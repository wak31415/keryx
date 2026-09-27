"""The phone: Twilio, a tunnel, and the number pointed at this machine.

Skippable — a Mac on the local channel alone needs none of it. When it is not skipped:

1. **Twilio.** The SID and token are checked with one read-only request, and the number is
   picked from the account's own list rather than typed, so it cannot be mistyped.
2. **The tunnel.** Cloudflare Tunnel on Linux, ngrok on macOS (`guides/tunnel.md`), and the
   hostname it gives is `PUBLIC_HOST`.
3. **The webhooks.** Shown next to where the number points now, and written only on a yes
   — the one write this section makes to anything outside the machine. On a no, nothing is
   touched and the console link is given instead.
4. **Texting** stays off unless asked for, with the reason.
"""

import re
import sys
from importlib import resources

from jarvis.doctor import STATUS_PATH, VOICE_PATH
from jarvis.notify.twilio_out import TwilioAdmin, TwilioError, TwilioNumber
from jarvis.setup.context import SetupContext
from jarvis.setup.ui import Choice

E164 = re.compile(r"\+[1-9]\d{6,14}")
HOSTNAME = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", re.I)
CONSOLE_NUMBERS = "https://console.twilio.com/us1/develop/phone-numbers/manage/incoming"


def guide(name: str) -> str:
    return resources.files("jarvis.setup.guides").joinpath(name).read_text(encoding="utf-8")


def webhook_urls(public_host: str) -> tuple[str, str]:
    """The voice webhook and the status callback, for a hostname."""
    return f"https://{public_host}{VOICE_PATH}", f"https://{public_host}{STATUS_PATH}"


def hostname_problem(value: str) -> str | None:
    """Why `value` is not a bare public hostname, or None."""
    if "://" in value or "/" in value:
        return "Just the hostname: no https:// and no path."
    return None if HOSTNAME.fullmatch(value) else "That is not a hostname."


def run_section(ctx: SetupContext) -> None:
    ui = ctx.ui
    ui.note("Call Jarvis from your phone, and have it call you back when work lands.")
    choice = ui.select(
        "Set up phone calls?",
        [Choice("setup", "Set up phone calls"), Choice("skip", "Skip — the Mac's microphone only")],
        default="setup",
    )
    if choice == "skip":
        return
    found = _twilio(ctx)
    if found is None:
        return
    if not _tunnel(ctx):
        return
    _webhooks(ctx, *found)
    _texting(ctx)


def _twilio(ctx: SetupContext) -> tuple[TwilioAdmin, TwilioNumber] | None:
    ui, settings = ctx.ui, ctx.settings
    if not ctx.review and settings.twilio_account_sid and settings.twilio_auth_token:
        sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    else:
        ui.markdown(guide("twilio.md"))
        sid = ui.text(
            "Account SID",
            default=settings.twilio_account_sid or "",
            validate=lambda v: None if v.strip().startswith("AC") else "It starts with AC.",
        )
        token = ui.secret("Auth token", validate=lambda v: None if v.strip() else "Required.")
    admin = ctx.probes.twilio(sid, token)
    try:
        with ui.spinner("Checking with Twilio…"):
            account = admin.account_name()
            numbers = admin.numbers()
    except TwilioError as exc:
        ui.error(f"Twilio refused: {exc}")
        return None
    ui.success(f"Twilio account: {account}")
    if not numbers:
        ui.error("That account has no phone number yet.")
        ui.note("Buy one with voice: https://console.twilio.com/us1/develop/phone-numbers/manage/search")
        return None
    current = settings.twilio_number
    chosen = ui.select(
        "Which number should Jarvis answer?",
        [Choice(number.phone_number, number.phone_number) for number in numbers],
        default=current if any(n.phone_number == current for n in numbers)
        else numbers[0].phone_number,
    )
    number = next(n for n in numbers if n.phone_number == chosen)
    ctx.save({"TWILIO_ACCOUNT_SID": sid, "TWILIO_AUTH_TOKEN": token, "TWILIO_NUMBER": chosen})
    return admin, number


def _tunnel(ctx: SetupContext) -> bool:
    ui, settings = ctx.ui, ctx.settings
    if settings.public_host and not ctx.review:
        ui.success(f"PUBLIC_HOST: {settings.public_host}")
        return True
    ui.markdown(guide("tunnel.md"))
    host = ui.text(
        "Public hostname (blank to stop here)",
        default=settings.public_host or "",
        validate=lambda value: None if not value else hostname_problem(value.strip()),
    )
    if not host:
        return False
    values: dict[str, str] = {"PUBLIC_HOST": host.strip().lower()}
    if sys.platform != "darwin":
        values["CLOUDFLARE_TUNNEL"] = ui.text(
            "Cloudflare tunnel name", default=settings.cloudflare_tunnel
        ) or settings.cloudflare_tunnel
    return ctx.save(values)


def _webhooks(ctx: SetupContext, admin: TwilioAdmin, number: TwilioNumber) -> None:
    """Point the number at this machine — after showing both addresses, and a yes."""
    ui, settings = ctx.ui, ctx.settings
    assert settings.public_host is not None
    voice, status = webhook_urls(settings.public_host)
    if (number.voice_url or "").rstrip("/") == voice and (
        number.status_callback or ""
    ).rstrip("/") == status:
        ui.success(f"{number.phone_number} already calls {voice}")
        return
    ui.table(
        ("Webhook", "Now", "Would become"),
        [
            ("Incoming call", number.voice_url or "—", voice),
            ("Call status", number.status_callback or "—", status),
        ],
    )
    if not ui.confirm(f"Point {number.phone_number} at this machine?", default=True):
        ui.note(f"Left as it is. To set it yourself: {CONSOLE_NUMBERS} (HTTP POST, both).")
        return
    try:
        admin.set_webhooks(number.sid, voice_url=voice, status_url=status)
    except TwilioError as exc:
        ui.error(f"Twilio refused the change: {exc}")
        return
    ui.success(f"{number.phone_number} now rings Jarvis")


def _texting(ctx: SetupContext) -> None:
    ui = ctx.ui
    ui.note(
        "Texting is off by default: many Twilio accounts cannot send SMS in their region, "
        "and Slack is the written channel. Calls work either way."
    )
    enable = ui.confirm("Let Jarvis send you texts?", default=ctx.settings.sms_enabled)
    if enable != ctx.settings.sms_enabled:
        ctx.save({"SMS_ENABLED": enable})


def numbers_problem(value: str) -> str | None:
    """Why a comma list of phone numbers is not all E.164, or None (blank is fine)."""
    bad = [part.strip() for part in value.split(",") if part.strip() and not E164.fullmatch(
        part.strip().replace(" ", ""))]
    return f"Not in E.164 (+15551234567): {', '.join(bad)}" if bad else None
