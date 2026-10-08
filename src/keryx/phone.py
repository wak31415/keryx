"""The phone: the owner's Twilio numbers, where the chosen one points, and the tunnel.

One implementation behind both the wizard's phone section (`setup/phone.py`, which only asks
and tells) and `keryx phone`, whose `--json` the Mac app reads:

- `numbers_report` — the account's name and its numbers, each with where its calls go now
  (`config options TWILIO_NUMBER` lists the same numbers);
- `webhooks_report` — where `TWILIO_NUMBER` should point and where it does;
- `apply_webhooks` — the write. It is the one write either makes to anything outside the
  machine, and both make it only after the owner has seen both addresses and said yes, at
  the terminal or in the app; `keryx phone webhooks --apply` is the owner's alone;
- `tunnel_report` — which tunnel this machine has, its unit, whether Keryx answers
  through `PUBLIC_HOST`, and the public hostname the tunnel's own config already names
  (`tunnel_hostname`), for the app to offer as `PUBLIC_HOST`.

A phone number here is the owner's own, shown to the owner; in a log line it is only ever
`mask_number`'s shape. A failure is one sentence of cause — a status code or an exception's
type at most, never a token, a body or an error's text, any of which can quote a credential
or a number back. The credentials go in the client's auth and nowhere else.
"""

import logging
import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from keryx.config import Settings
from keryx.config.files import xdg_home
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
#: Where to get cloudflared, as a page the app can open: it never shows `brew install`.
CLOUDFLARED_DOWNLOADS = (
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
)
BAD_HOST = "PUBLIC_HOST isn't a bare hostname: just the name, no https:// and no path."
#: A bare public hostname: labels and a top-level domain, nothing else — no scheme, port,
#: path, credentials, fragment or control character, any of which would point a webhook
#: at another host or path.
HOSTNAME = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", re.I)

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


def refused_credentials(error: Exception) -> bool:
    """Whether `error` is Twilio answering that the SID and token do not go together — an
    answer about the setup, where anything else is Twilio not answering this time."""
    return isinstance(error, TwilioError) and error.status in TWILIO_REFUSED


def twilio_problem(error: Exception) -> str:
    """`error`, from asking Twilio, as one sentence of cause and nothing else."""
    if refused_credentials(error):
        return "Twilio didn't accept the account SID and auth token."
    if isinstance(error, TwilioError):
        if error.status is not None:
            return f"Twilio answered with HTTP {error.status}."
        return f"Keryx couldn't reach Twilio ({error.cause or type(error).__name__})."
    return f"Keryx couldn't reach Twilio ({type(error).__name__})."


# --- where a number points ---------------------------------------------------------------


def hostname_problem(value: str) -> str | None:
    """Why `value` is not a bare public hostname, or None. The wizard asks it of what is
    typed; every webhook written or compared here asks it of `PUBLIC_HOST` as saved."""
    if "://" in value or "/" in value:
        return "Just the hostname: no https:// and no path."
    return None if HOSTNAME.fullmatch(value) else "That is not a hostname."


def _host(settings: Settings) -> tuple[str | None, str | None]:
    """(`PUBLIC_HOST` when it is a bare hostname, why not), the reason None when unset."""
    host = settings.public_host
    if not host:
        return None, None
    return (None, BAD_HOST) if hostname_problem(host) else (host, None)


def webhook_urls(public_host: str) -> tuple[str, str]:
    """The voice webhook and the status callback, for a hostname."""
    return f"https://{public_host}{VOICE_PATH}", f"https://{public_host}{STATUS_PATH}"


def _same(url: str | None, want: str) -> bool:
    return (url or "").rstrip("/") == want


def matches(number: TwilioNumber, public_host: str) -> bool:
    """Whether both of `number`'s webhooks already point at `public_host`."""
    voice, status = webhook_urls(public_host)
    return _same(number.voice_url, voice) and _same(number.status_callback, status)


def point_here(admin: TwilioAdmin, number: TwilioNumber, public_host: str) -> None:
    """Point `number` at `public_host`: both webhooks, as POST. `Refused`, before either URL
    is built, for a host that is not a bare hostname — checked here, at the write, so no
    caller can point a number at `good.example@evil.example`; else raises `TwilioError`."""
    if hostname_problem(public_host):
        raise Refused(BAD_HOST)
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
    """`keryx phone numbers --json`: a report, so Twilio's refusal is its `error`. A
    number points here when both its webhooks do, as `webhooks_report` compares them."""
    host, _ = _host(settings)
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
                "points_here": host is not None and matches(number, host),
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
    host, bad = _host(settings)
    voice, status = webhook_urls(host) if host else (None, None)
    report: dict[str, Any] = {
        "number": settings.twilio_number or None,
        "expected": {"voice_url": voice, "status_url": status},
        "current": None,
        "matches": False,
        "error": None if host else bad or NO_HOST,
    }
    try:
        _needs(settings)
        number = _find(_admin(settings, admin_for), settings.twilio_number or "")
    except (Refused, Failed) as why:
        report["error"] = str(why)
        return report
    report["current"] = {"voice_url": number.voice_url, "status_url": number.status_callback}
    report["matches"] = host is not None and matches(number, host)
    return report


def apply_webhooks(settings: Settings, admin_for: AdminFor | None = None) -> dict[str, Any]:
    """Point `TWILIO_NUMBER` at `PUBLIC_HOST`: `keryx phone webhooks --apply`. The caller
    has had the owner's yes. `Refused` before Twilio is asked; `Failed` once it was."""
    _needs(settings)
    host, bad = _host(settings)
    if host is None:
        raise Refused(bad or NO_HOST)
    admin = _admin(settings, admin_for)
    number = _find(admin, settings.twilio_number or "")
    try:
        point_here(admin, number, host)
    except Refused:
        raise
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
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
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
    unit, the hostname, and whether Keryx answers there — null with no hostname to ask, and
    false, with `error` saying why, for one that is not a bare hostname. `install_url` is
    cloudflared's download page while there is no tunnel program, else null."""
    from keryx import units

    find = which or shutil.which
    kind, binary = None, None
    for name in TUNNEL_BINARIES:
        if found := find(name):
            kind, binary = name, found
            break
    tunnel = (unit_status or units.status)(settings)["units"].get("tunnel", {})
    host, bad = _host(settings)
    if host is not None:
        reachable: bool | None = _answers_as_keryx(get or httpx.get, host)
    else:
        reachable = False if bad else None
    return {
        "kind": kind,
        "binary": binary,
        "public_host": settings.public_host or None,
        "unit_installed": bool(tunnel.get("installed")),
        "reachable": reachable,
        "error": bad,
        "install_url": CLOUDFLARED_DOWNLOADS if binary is None else None,
        "hostname": tunnel_hostname(settings, kind),
    }


# --- the hostname the tunnel already knows -----------------------------------------------

#: Where cloudflared looks for its config outside `HOME`, as cloudflared itself does.
CLOUDFLARED_SYSTEM_DIRS: tuple[Path, ...] = (
    Path("/etc/cloudflared"), Path("/usr/local/etc/cloudflared"),
)
#: A tunnel's config is a few lines: anything larger is not one, and is not read.
MAX_TUNNEL_CONFIG_BYTES = 1 << 20
#: One `key: value` line of a YAML block, an item's `- ` before it or not.
_YAML_LINE = re.compile(r"^\s*(-\s+)?([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def tunnel_hostname(settings: Settings, kind: str | None) -> str | None:
    """The public hostname the tunnel's own config names, or None: cloudflared's ingress
    rule for Keryx's port (else its first hostname), or ngrok's reserved domain. The tunnel
    found (`kind`) is asked first, then the other. Only reads, never the network, and never
    raises: a config that will not read, or holds no bare hostname, is None. Only a
    hostname ever leaves — these files can hold a tunnel's token."""
    order = sorted(TUNNEL_BINARIES, key=lambda name: name != kind)
    for name in order:
        read = _cloudflared_hostname if name == "cloudflared" else _ngrok_hostname
        for path in _tunnel_configs(name):
            try:
                text = _config_text(path)
                found = read(text, settings.port) if text is not None else None
            except Exception:  # never the report's failure: a hostname is only a suggestion
                found = None
            if found:
                return found
    return None


def _tunnel_configs(name: str) -> list[Path]:
    """Where `name` keeps its config, in the order it looks."""
    home = Path.home()
    if name == "cloudflared":
        dirs = [home / ".cloudflared", home / ".cloudflare-warp", home / "cloudflare-warp",
                *CLOUDFLARED_SYSTEM_DIRS]
        return [folder / file for folder in dirs for file in ("config.yml", "config.yaml")]
    return [xdg_home("config") / "ngrok" / "ngrok.yml",
            home / "Library" / "Application Support" / "ngrok" / "ngrok.yml",
            home / ".ngrok2" / "ngrok.yml"]


def _config_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_TUNNEL_CONFIG_BYTES:
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _yaml_lines(text: str) -> Iterator[tuple[bool, str, str]]:
    """Each `key: value` line of a block-style YAML file: whether it starts a list item,
    the key, and the value unquoted and without its comment. Enough for a tunnel's config,
    which is all keys, lists of rules and scalars."""
    for line in text.splitlines():
        found = _YAML_LINE.match(line)
        if found is None:
            continue
        item, key, value = bool(found.group(1)), found.group(2), found.group(3)
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else ""
        else:
            value = value.split(" #", 1)[0].strip()
        yield item, key, value


def _bare(value: str) -> str | None:
    """`value` as a bare public hostname, lower-cased, or None when it is not one."""
    host = value.strip().lower()
    return host if host and hostname_problem(host) is None else None


def _cloudflared_hostname(text: str, port: int) -> str | None:
    rules: list[dict[str, str]] = []
    for item, key, value in _yaml_lines(text):
        if item:
            rules.append({})
        if rules and key in ("hostname", "service"):
            rules[-1].setdefault(key, value)
    named = [(host, rule.get("service", "")) for rule in rules
             if (host := _bare(rule.get("hostname", "")))]
    for host, service in named:
        try:
            target = urlparse(service)
            if target.hostname in _LOOPBACK and target.port == port:
                return host
        except ValueError:
            continue
    return named[0][0] if named else None


def _ngrok_hostname(text: str, port: int) -> str | None:
    """A reserved domain: `domain:` (or the older `hostname:`) on a tunnel, or an endpoint's
    `url:` — an upstream's loopback url is no hostname, so it is never taken."""
    for _item, key, value in _yaml_lines(text):
        if key == "url":
            value = value.removeprefix("https://")
        elif key not in ("domain", "hostname"):
            continue
        if host := _bare(value):
            return host
    return None
