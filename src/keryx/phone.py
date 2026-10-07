"""The phone: the owner's Twilio numbers, where the chosen one points, and the tunnel.

One implementation behind both the wizard's phone section (`setup/phone.py`, which only asks
and tells) and `keryx phone`, whose `--json` the Mac app reads:

- `numbers_report` — the account's name and its numbers, each with where its calls go now
  (`config options TWILIO_NUMBER` lists the same numbers);
- `webhooks_report` — where `TWILIO_NUMBER` should point and where it does;
- `apply_webhooks` — the write. It is the one write either makes to anything outside the
  machine, and both make it only after the owner has seen both addresses and said yes, at
  the terminal or in the app; `keryx phone webhooks --apply` is the owner's alone;
- `tunnel_report` — which tunnel this machine has, its unit, and whether Keryx answers
  through `PUBLIC_HOST`.

A phone number here is the owner's own, shown to the owner; in a log line it is only ever
`mask_number`'s shape. A failure is one sentence of cause — a status code or an exception's
type at most, never a token, a body or an error's text, any of which can quote a credential
or a number back. The credentials go in the client's auth and nowhere else.
"""

import logging
import shutil
from collections.abc import Callable
from typing import Any

import httpx

from keryx.config import Settings
from keryx.config.options import TIMEOUT_S
from keryx.doctor import STATUS_PATH, TUNNEL_BINARIES, VOICE_PATH
from keryx.logging_util import mask_number
from keryx.notify.twilio_out import TwilioAdmin, TwilioError, TwilioNumber

log = logging.getLogger("keryx.phone")

#: How long the tunnel has to answer for Keryx. (One Twilio request may take `TIMEOUT_S`,
#: every check's: the owner is waiting on it.)
TUNNEL_TIMEOUT_S = 3.0
#: Twilio's answers that mean the SID and the token do not go together.
TWILIO_REFUSED = (401, 403, 404)
NOT_ON_ACCOUNT = "TWILIO_NUMBER isn't one of this Twilio account's numbers."
NO_NUMBER = "There's no TWILIO_NUMBER saved."
NO_HOST = "There's no PUBLIC_HOST saved, so there's nowhere to point it."

#: `(account_sid, auth_token)` → a `TwilioAdmin`.
AdminFor = Callable[[str, str], TwilioAdmin]


class Refused(Exception):
    """Nothing to apply yet: something the write needs is not saved. Twilio was not asked."""


class Failed(Exception):
    """Twilio was asked and did not give what was wanted: one sentence of cause."""


def rest_admin(sid: str, token: str) -> TwilioAdmin:
    """The admin client over Twilio's REST API, with a timeout. The SID and token are the
    client's basic auth, never part of a URL."""
    from twilio.http.http_client import TwilioHttpClient
    from twilio.rest import Client

    from keryx.notify.twilio_out import RestTwilioAdmin

    client = Client(sid, token, http_client=TwilioHttpClient(timeout=TIMEOUT_S))
    return RestTwilioAdmin(sid, token, client=client)


def twilio_problem(error: Exception) -> str:
    """`error`, from asking Twilio, as one sentence of cause and nothing else."""
    if isinstance(error, TwilioError):
        if error.status in TWILIO_REFUSED:
            return "Twilio didn't accept the account SID and auth token."
        if error.status is not None:
            return f"Twilio answered with HTTP {error.status}."
        return f"Keryx couldn't reach Twilio ({error.cause or type(error).__name__})."
    return f"Keryx couldn't reach Twilio ({type(error).__name__})."


# --- where a number points ---------------------------------------------------------------


def webhook_urls(public_host: str) -> tuple[str, str]:
    """The voice webhook and the status callback, for a hostname."""
    return f"https://{public_host}{VOICE_PATH}", f"https://{public_host}{STATUS_PATH}"


def _same(url: str | None, want: str) -> bool:
    return (url or "").rstrip("/") == want


def matches(number: TwilioNumber, public_host: str) -> bool:
    """Whether both of `number`'s webhooks already point at `public_host`."""
    voice, status = webhook_urls(public_host)
    return _same(number.voice_url, voice) and _same(number.status_callback, status)


def rings_here(number: TwilioNumber, public_host: str | None) -> bool:
    """Whether `number`'s calls come here: its voice webhook, whatever its status callback."""
    return bool(public_host) and _same(number.voice_url, webhook_urls(public_host or "")[0])


def point_here(admin: TwilioAdmin, number: TwilioNumber, public_host: str) -> None:
    """Point `number` at `public_host`: both webhooks, as POST. Raises `TwilioError`."""
    voice, status = webhook_urls(public_host)
    admin.set_webhooks(number.sid, voice_url=voice, status_url=status)
    log.info("pointed %s at %s", mask_number(number.phone_number), public_host)


# --- the reports -------------------------------------------------------------------------


def _missing_credentials(settings: Settings) -> str | None:
    """Why Twilio cannot be asked: the credentials not saved, said by name; or None."""
    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
        )
        if not value
    ]
    return f"There's no {' or '.join(missing)} saved." if missing else None


def _admin(settings: Settings, admin_for: AdminFor | None) -> TwilioAdmin:
    # Late-bound through the module, so a test can hand in its own.
    make = admin_for or rest_admin
    return make(settings.twilio_account_sid or "", settings.twilio_auth_token or "")


def account_numbers(
    settings: Settings, admin_for: AdminFor | None = None
) -> tuple[str, list[TwilioNumber]]:
    """The account's name and its numbers. Raises `Refused` without credentials, and
    `Failed` with Twilio's answer as one sentence."""
    if why := _missing_credentials(settings):
        raise Refused(why)
    admin = _admin(settings, admin_for)
    try:
        return admin.account_name(), admin.numbers()
    except Exception as error:  # by its cause only: its text may quote the token
        raise Failed(twilio_problem(error)) from None


def numbers_report(settings: Settings, admin_for: AdminFor | None = None) -> dict[str, Any]:
    """`keryx phone numbers --json`: a report, so Twilio's refusal is its `error`."""
    try:
        account, numbers = account_numbers(settings, admin_for)
    except (Refused, Failed) as why:
        return {"account": None, "numbers": [], "error": str(why)}
    return {
        "account": account,
        "numbers": [
            {
                "number": number.phone_number,
                "friendly_name": number.friendly_name,
                "voice_url": number.voice_url,
                "points_here": rings_here(number, settings.public_host),
            }
            for number in numbers
        ],
        "error": None,
    }


def _needs(settings: Settings) -> None:
    """`Refused` unless `TWILIO_NUMBER` and the credentials to look it up are saved."""
    if not settings.twilio_number:
        raise Refused(NO_NUMBER)
    if why := _missing_credentials(settings):
        raise Refused(why)


def _find(admin: TwilioAdmin, wanted: str) -> TwilioNumber:
    """`wanted` as the account has it, or `Failed`."""
    try:
        listed = admin.numbers()
    except Exception as error:  # by its cause only: its text may quote the token
        raise Failed(twilio_problem(error)) from None
    for number in listed:
        if number.phone_number == wanted:
            return number
    raise Failed(NOT_ON_ACCOUNT)


def webhooks_report(settings: Settings, admin_for: AdminFor | None = None) -> dict[str, Any]:
    """`keryx phone webhooks --json`: where the number should point, and where it does."""
    host = settings.public_host
    voice, status = webhook_urls(host) if host else (None, None)
    report: dict[str, Any] = {
        "number": settings.twilio_number or None,
        "expected": {"voice_url": voice, "status_url": status},
        "current": None,
        "matches": False,
        "error": None if host else NO_HOST,
    }
    try:
        _needs(settings)
        number = _find(_admin(settings, admin_for), settings.twilio_number or "")
    except (Refused, Failed) as why:
        report["error"] = str(why)
        return report
    report["current"] = {"voice_url": number.voice_url, "status_url": number.status_callback}
    report["matches"] = bool(host) and matches(number, host or "")
    return report


def apply_webhooks(settings: Settings, admin_for: AdminFor | None = None) -> dict[str, Any]:
    """Point `TWILIO_NUMBER` at `PUBLIC_HOST`: `keryx phone webhooks --apply`. The caller
    has had the owner's yes. `Refused` before Twilio is asked; `Failed` once it was."""
    _needs(settings)
    if not settings.public_host:
        raise Refused(NO_HOST)
    admin = _admin(settings, admin_for)
    number = _find(admin, settings.twilio_number or "")
    try:
        point_here(admin, number, settings.public_host)
    except Exception as error:  # by its cause only: its text may quote the token
        raise Failed(twilio_problem(error)) from None
    return {"applied": True, "number": number.phone_number}


# --- the tunnel --------------------------------------------------------------------------


def _answers_as_keryx(get: Callable[..., Any], public_host: str) -> bool:
    """Whether `https://PUBLIC_HOST/health` is Keryx answering, through whatever tunnel."""
    try:
        response = get(
            f"https://{public_host}/health", timeout=TUNNEL_TIMEOUT_S, follow_redirects=False
        )
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(body, dict) and body.get("ok") is True


def tunnel_report(
    settings: Settings,
    *,
    which: Callable[[str], str | None] | None = None,
    get: Callable[..., Any] | None = None,
    unit_status: Callable[[Settings], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`keryx phone tunnel --json`: the tunnel binary on `PATH` (as `doctor` finds it), its
    unit, the hostname, and whether Keryx answers there — null with no hostname to ask."""
    from keryx import units

    find = which or shutil.which
    kind, binary = None, None
    for name in TUNNEL_BINARIES:
        if found := find(name):
            kind, binary = name, found
            break
    tunnel = (unit_status or units.status)(settings)["units"].get("tunnel", {})
    host = settings.public_host or None
    return {
        "kind": kind,
        "binary": binary,
        "public_host": host,
        "unit_installed": bool(tunnel.get("installed")),
        "reachable": None if host is None else _answers_as_keryx(get or httpx.get, host),
    }
