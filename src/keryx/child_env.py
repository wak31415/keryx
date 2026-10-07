"""The environments Keryx's children run in: Keryx's own, with no secret of Keryx's in it.

Two kinds of child need `PATH`, `HOME` and the like, and none of Keryx's credentials —
CLAUDE.md's "a credential goes in the child's environment and nowhere else" read from the
other side:

- **A sign-in's child** (`login_env`) — `claude setup-token`, `codex login --device-auth`,
  the workspace-mcp server a Google sign-in starts. Codex's three credential variables are
  set blank for it, as `agents/codex.py` sets them for every other Codex child, so nothing
  inherited is lent.
- **An operational child** (`operational_env`) — speech-to-speech's model download, the
  package installers (`uv`, `pip`, Homebrew, Ollama) and the checkout's unit installer. It
  keeps what such work needs and nothing else of Keryx's: the Hugging Face tokens
  (`HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN`) stay, since a gated download needs one and they are
  the owner's for exactly that.

Either way every secret setting's variable is dropped, under every name `Settings` reads it
by and in any case (it reads them case-insensitively, so `keryx_pin` is a PIN to it), with
the Anthropic credentials that are not settings.

And the same list read the other way: `redact_known` takes every one of those values out of
text Keryx is about to show — an error's `detail`, a check's `hint`, a server's problem —
since what a tool or a server says can quote the credential it was handed.
"""

import os
from collections.abc import Iterable

from keryx.agents.auth import redact
from keryx.agents.codex import CREDENTIAL_VARIABLES, LOCAL_KEY_ENV
from keryx.config import PLACEHOLDER_KEY, Settings
from keryx.config.settings import env_var_names

#: Not settings, but credentials all the same (the last is a local server's key, lent to Codex).
ALSO_SECRET = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", LOCAL_KEY_ENV)
#: Credentials an operational child is owed: a Hugging Face download may be gated.
OPERATIONAL_KEEPS = frozenset({"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"})


def secret_variables() -> frozenset[str]:
    """Every variable a secret may come in under, upper-cased: each `repr=False` setting's
    names (the PIN's both), Codex's credential variables, and `ALSO_SECRET`."""
    names = {name for field, info in Settings.model_fields.items() if not info.repr
             for name in env_var_names(field)}
    names |= {*env_var_names("pin"), *CREDENTIAL_VARIABLES, *ALSO_SECRET}
    return frozenset(name.upper() for name in names)


def redact_known(
    text: str, settings: Settings | None = None, extra: Iterable[str | None] = ()
) -> str:
    """`text` with every credential Keryx knows of replaced by `[redacted]`: each secret
    setting's value (in `settings`, else the settings as the store holds them now, when they
    load), each `secret_variables()` and Hugging Face token (`OPERATIONAL_KEEPS`) in this
    process's environment, and `extra` — a key in hand, like a server's. Longest first, so
    one that contains another goes whole; anything shaped like a masked key goes too
    (`agents.auth.redact`). Never raises."""
    found = [*extra]
    known = settings if settings is not None else _settings_now()
    if known is not None:
        found += [getattr(known, name) for name, info in Settings.model_fields.items()
                  if not info.repr]
    names = secret_variables() | OPERATIONAL_KEEPS
    found += [value for key, value in os.environ.items() if key.upper() in names]
    secrets = {str(value) for value in found if value and str(value) != PLACEHOLDER_KEY}
    return redact(text, sorted(secrets, key=len, reverse=True))


def _settings_now() -> Settings | None:
    """The settings as the store holds them now, or None when they will not load."""
    from keryx.config import load_settings

    try:
        return load_settings()
    except Exception:  # what is being redacted may be exactly why they will not load
        return None


def _without_secrets(keep: frozenset[str] = frozenset()) -> dict[str, str]:
    secret = secret_variables() - keep
    return {key: value for key, value in os.environ.items() if key.upper() not in secret}


def login_env(**extra: str) -> dict[str, str]:
    """Keryx's environment with every secret variable gone, whatever its case, Codex's
    credential variables blank, and `extra` on top."""
    return {**_without_secrets(), **dict.fromkeys(CREDENTIAL_VARIABLES, ""), **extra}


def operational_env(**extra: str) -> dict[str, str]:
    """Keryx's environment with every secret variable gone, whatever its case, but the
    Hugging Face tokens (`OPERATIONAL_KEEPS`); `extra` on top. `PATH`, `HOME`, `KERYX_HOME`,
    the `XDG_*` directories and the proxies are no secrets, and stay as they are."""
    return {**_without_secrets(OPERATIONAL_KEEPS), **extra}
