"""A key or credential, checked before it is saved: `keryx config verify KEY`.

The Mac app asks this with the value the owner has just typed (on stdin, never saved here),
and saves it with `config set --stdin` only once it works — or once the owner says to keep
it anyway. Without a value, the one saved is checked. Each check is one the setup already
makes, reached through `Checks`, which the tests replace wholesale:

- `OPENAI_API_KEY` and `CODEX_API_KEY`: the wizard's `openai_key_problem`, OpenAI's list of
  models. A Codex API key is an OpenAI platform key, so the same read is the cheap check.
- `ANTHROPIC_API_KEY`: Anthropic's list of models, the way `config options` reads it — one
  `GET` that follows no redirect.
- `TWILIO_AUTH_TOKEN`: the account the saved `TWILIO_ACCOUNT_SID` names, read with the token
  (the admin client the phone section and `doctor` use); its friendly name is the `detail`.
  The SID is not checked on its own: it means nothing without its token.

A key that does not work is a result, not a failure. `problem` is one sentence of cause and
nothing else — a status code or an exception's type at most, never a key, a body or an
error's text, any of which can quote the key back. Nothing here writes anything.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keryx.config.options import (
    ANTHROPIC_MODELS_URL,
    ANTHROPIC_VERSION,
    TIMEOUT_S,
    Unavailable,
    _get_json,
    http_get,
)
from keryx.config.settings import Settings

#: Each key that can be checked, and the field its saved value is read from.
SUPPORTED = {
    "OPENAI_API_KEY": "openai_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "CODEX_API_KEY": "codex_api_key",
    "TWILIO_AUTH_TOKEN": "twilio_auth_token",
}
#: Twilio's answers that mean the SID and the token do not go together.
TWILIO_REFUSED = (401, 403, 404)


class Unsupported(ValueError):
    """A key there is no check for."""


@dataclass(frozen=True)
class Verified:
    """What `keryx config verify KEY --json` prints; the field names are a contract."""

    key: str
    ok: bool
    problem: str | None
    detail: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "ok": self.ok, "problem": self.problem, "detail": self.detail}


@dataclass(frozen=True)
class Checks:
    """Everything outside this process a key is checked against."""

    #: The wizard's probe: why OpenAI will not take a key, or None.
    openai_key_problem: Callable[[str], str | None]
    #: `httpx.get`'s shape: `get(url, *, headers, timeout, follow_redirects)`.
    get: Callable[..., Any]
    #: `(account_sid, auth_token)` → a `TwilioAdmin`.
    twilio: Callable[[str, str], Any]


def _twilio_admin(sid: str, token: str) -> Any:
    """The phone section's admin client, with a timeout: the owner is waiting on it."""
    from twilio.http.http_client import TwilioHttpClient
    from twilio.rest import Client

    from keryx.notify.twilio_out import RestTwilioAdmin

    client = Client(sid, token, http_client=TwilioHttpClient(timeout=TIMEOUT_S))
    return RestTwilioAdmin(sid, token, client=client)


def default_checks() -> Checks:
    """The real OpenAI, Anthropic and Twilio."""
    from keryx.setup.context import openai_key_problem

    return Checks(openai_key_problem=openai_key_problem, get=http_get, twilio=_twilio_admin)


def can_verify(key: str) -> bool:
    return key in SUPPORTED


def _sentence(text: str) -> str:
    """The wizard's fragment as the sentence `problem` is: capitalised, a full stop."""
    text = text.strip().rstrip(".")
    return f"{text[:1].upper()}{text[1:]}."


def _openai(key: str, checks: Checks) -> str | None:
    problem = checks.openai_key_problem(key)
    return None if problem is None else _sentence(problem)


def _anthropic(key: str, checks: Checks) -> str | None:
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    try:
        _get_json(checks.get, ANTHROPIC_MODELS_URL, headers, "Anthropic")
    except Unavailable as why:
        return str(why)
    return None


def _twilio(token: str, settings: Settings, checks: Checks) -> tuple[str | None, str | None]:
    """(problem, the account's name) for `token` with the saved account SID."""
    from keryx.notify.twilio_out import TwilioError

    sid = settings.twilio_account_sid
    if not sid:
        return "There's no TWILIO_ACCOUNT_SID saved to check the token with.", None
    try:
        return None, checks.twilio(sid, token).account_name()
    except TwilioError as error:
        if error.status in TWILIO_REFUSED:
            return "Twilio didn't accept the account SID and auth token.", None
        if error.status is not None:
            return f"Twilio answered with HTTP {error.status}.", None
        return f"Keryx couldn't reach Twilio ({error.cause or type(error).__name__}).", None


def verify(
    key: str, value: str | None, settings: Settings, checks: Checks | None = None
) -> Verified:
    """Whether `key` works: `value` when given, else the one saved. Never saves either."""
    if key not in SUPPORTED:
        raise Unsupported(key)
    secret = value if value is not None else getattr(settings, SUPPORTED[key])
    if not secret:
        return Verified(key, False, f"There's no {key} to check.", None)
    checks = checks or default_checks()
    detail = None
    try:
        if key == "ANTHROPIC_API_KEY":
            problem = _anthropic(secret, checks)
        elif key == "TWILIO_AUTH_TOKEN":
            problem, detail = _twilio(secret, settings, checks)
        else:
            problem = _openai(secret, checks)
    except Exception as exc:  # by its type only: its text may quote the key
        problem = f"Keryx couldn't check the key ({type(exc).__name__})."
    return Verified(key, problem is None, problem, detail)
