"""How a coding agent authenticates: three tiers, one precedence, for every agent.

| tier                         | claude                    | codex                |
|------------------------------|---------------------------|----------------------|
| API key (pay per token)      | `ANTHROPIC_API_KEY`       | `CODEX_API_KEY`      |
| headless subscription token  | `CLAUDE_CODE_OAUTH_TOKEN` | `CODEX_ACCESS_TOKEN` |
| stored subscription login    | `claude` → `/login`       | `codex login`        |

The first one configured wins. A backend supplies only data — its two setting names, the
variables they travel under, and a probe for the stored login (`AuthSource`) — and
`resolve_auth` applies the precedence for all of them, so `doctor`, `jarvis setup` and the
runners can never disagree about which credential a subagent is on.

A credential reaches the subagent in its *environment* and nowhere else: never argv, which
any user on the machine can read in `ps`, and never a log line. What does get logged or
spoken is passed through `redact` first, because a provider's 401 can quote the key back.

Nothing here falls back from one agent's key to another's. `OPENAI_API_KEY` is the voice
model's key, and quietly handing it to Codex would move a subscription user onto per-token
billing without anybody having asked for that.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from jarvis.config import Settings

#: What a provider's error makes of a key it refuses: `sk-bogus*********robe`. Masked, but
#: still the key's first and last characters, which is more than a spoken error needs.
_MASKED_KEY_RE = re.compile(r"\b[A-Za-z0-9][\w-]{2,}\*{3,}[\w-]*")
REDACTED = "[redacted]"


class AuthMode(StrEnum):
    """Which of the three tiers a backend resolved to, or none of them."""

    API_KEY = "api_key"
    TOKEN = "token"
    SUBSCRIPTION = "subscription"
    NONE = "none"


@dataclass(frozen=True)
class AuthSource:
    """Where one backend's credentials come from — data, not behaviour."""

    #: The `Settings` field and the child's environment variable for the API-key tier.
    api_key_setting: str
    api_key_env: str
    #: The same pair for the headless subscription token.
    token_setting: str
    token_env: str
    #: Best effort: is there a stored subscription login on this machine?
    stored_login: Callable[[], bool]
    #: What to run for each tier, for the sentences `doctor` and `jarvis setup` print.
    login_hint: str


@dataclass(frozen=True)
class AuthStatus:
    """The credential a backend will run on, and a sentence saying so."""

    mode: AuthMode
    #: The environment variable the credential goes under; None for a stored login.
    variable: str | None = None
    secret: str | None = field(default=None, repr=False)
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.mode is not AuthMode.NONE


def resolve_auth(source: AuthSource, settings: Settings, *, probe: bool = True) -> AuthStatus:
    """API key, else headless token, else the stored login, else nothing.

    `probe=False` skips looking for the stored login and calls it one: a runner does not
    need to know, because the agent's own CLI finds that login by itself, and a runner
    opens a session per task. `doctor` and `jarvis auth status` probe.
    """
    key = getattr(settings, source.api_key_setting)
    if key:
        return AuthStatus(
            AuthMode.API_KEY,
            source.api_key_env,
            key,
            f"{source.api_key_env} set (pay per token)",
        )
    token = getattr(settings, source.token_setting)
    if token:
        return AuthStatus(
            AuthMode.TOKEN, source.token_env, token, f"{source.token_env} set (subscription)"
        )
    if not probe or source.stored_login():
        return AuthStatus(AuthMode.SUBSCRIPTION, detail="stored subscription login")
    return AuthStatus(AuthMode.NONE, detail=f"no login found — {source.login_hint}")


def child_env(status: AuthStatus) -> dict[str, str]:
    """The variables to add to the subagent's environment: the credential, or nothing.

    Nothing is the stored login, which the agent's own CLI finds by itself.
    """
    if status.variable and status.secret:
        return {status.variable: status.secret}
    return {}


def redact(text: str, secrets: Iterable[str | None] = ()) -> str:
    """`text` with every secret in `secrets`, and anything shaped like a masked key, removed."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return _MASKED_KEY_RE.sub(REDACTED, text)
