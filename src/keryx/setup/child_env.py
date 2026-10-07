"""The environment a sign-in's child runs in: Keryx's own, with no secret of Keryx's in it.

A login child — `claude setup-token`, `codex login --device-auth`, the workspace-mcp server
a Google sign-in starts — needs `PATH`, `HOME` and the like, and none of Keryx's
credentials: CLAUDE.md's "a credential goes in the child's environment and nowhere else"
read from the other side. So every secret setting's variable is dropped, under every name
`Settings` reads it by and in any case (it reads them case-insensitively, so `keryx_pin`
is a PIN to it), with the Anthropic credentials that are not settings; and Codex's three
credential variables are set blank, as `agents/codex.py` sets them for every other Codex
child, so nothing inherited is lent.
"""

import os

from keryx.agents.codex import CREDENTIAL_VARIABLES
from keryx.config import Settings
from keryx.config.settings import env_var_names

#: Not a setting, but a credential all the same.
ALSO_SECRET = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR")


def secret_variables() -> frozenset[str]:
    """Every variable a secret may come in under, upper-cased: each `repr=False` setting's
    names (the PIN's both), Codex's credential variables, and `ALSO_SECRET`."""
    names = {name for field, info in Settings.model_fields.items() if not info.repr
             for name in env_var_names(field)}
    names |= {*env_var_names("pin"), *CREDENTIAL_VARIABLES, *ALSO_SECRET}
    return frozenset(name.upper() for name in names)


def login_env(**extra: str) -> dict[str, str]:
    """Keryx's environment with every secret variable gone, whatever its case, Codex's
    credential variables blank, and `extra` on top."""
    secret = secret_variables()
    env = {key: value for key, value in os.environ.items() if key.upper() not in secret}
    return {**env, **dict.fromkeys(CREDENTIAL_VARIABLES, ""), **extra}
