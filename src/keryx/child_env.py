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
"""

import os

from keryx.agents.codex import CREDENTIAL_VARIABLES
from keryx.config import Settings
from keryx.config.settings import env_var_names

#: Not a setting, but a credential all the same.
ALSO_SECRET = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR")
#: Credentials an operational child is owed: a Hugging Face download may be gated.
OPERATIONAL_KEEPS = frozenset({"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"})


def secret_variables() -> frozenset[str]:
    """Every variable a secret may come in under, upper-cased: each `repr=False` setting's
    names (the PIN's both), Codex's credential variables, and `ALSO_SECRET`."""
    names = {name for field, info in Settings.model_fields.items() if not info.repr
             for name in env_var_names(field)}
    names |= {*env_var_names("pin"), *CREDENTIAL_VARIABLES, *ALSO_SECRET}
    return frozenset(name.upper() for name in names)


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
