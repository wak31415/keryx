"""Which settings the running service may change, and which it never may.

Two actors write the configuration. The **owner** is somebody at a terminal: `jarvis config`,
`jarvis setup`. The **service** is Jarvis itself — the voice model's `set_config` tool, or a
subagent running `jarvis config set` inside a task (everything `jarvis serve` starts carries
`JARVIS_ACTOR=service` in its environment). The owner may write anything but the PIN, which
has a door of its own (`jarvis.config.pin`). The service may write a key only when it is
*service-writable*:

- each field declares a default (`service_writable=` in `jarvis.config.settings`) — the
  things you would reasonably say on a call: the voice, how long it waits, which model;
- the owner overrides one with `jarvis config lock KEY` / `unlock KEY`, recorded under
  `[service_writable]` in `config.toml`;
- and `PROTECTED_KEYS` can never be unlocked, because each one is either a credential or
  a line of defence — trust, approvals, spending, deletion, the network, the debug
  switches. A caller who has the PIN can already dispatch a subagent with a shell, so
  this is not a sandbox and does not pretend to be (SECURITY.md); it is what keeps Jarvis's
  *own* tools from lowering its own guard because somebody asked nicely on the phone.
"""

import fnmatch
import os
from collections.abc import Mapping

from jarvis.config.settings import (
    NOT_STORED,
    Settings,
    env_var_name,
    field_for,
    field_service_writable,
    is_secret,
)

#: Set in the environment of `jarvis serve`, and so inherited by every subagent it runs.
ACTOR_ENV = "JARVIS_ACTOR"
OWNER = "owner"
SERVICE = "service"

#: Keys the service may never write and the owner may never unlock, as `fnmatch` patterns.
#: Every secret is protected too (`is_protected`), whatever it is called.
PROTECTED_PATTERNS = (
    # Trust: who may call, which phone is theirs, and what a call hears before the PIN.
    "JARVIS_PIN",
    "PIN_*",
    "ALLOWED_CALLERS",
    "OWNER_NUMBER",
    "BRIEFING_BEFORE_PIN",
    # The approval bridge: the allowlist is the primary control over what a keypad runs.
    "APPROVALS_ENABLED",
    "APPROVAL_BASH_ALLOW",
    "APPROVAL_ROOTS",
    # Publishing: whether Jarvis files issues about itself, which are public, and where.
    # The owner turns it on by hand (`jarvis setup` or `jarvis config set`), never Jarvis.
    "ISSUE_*",
    # Spending and deletion.
    "SUBAGENT_MAX_BUDGET_USD",
    "DAILY_TASK_CAP",
    "*_RETENTION_DAYS",
    "SMS_ENABLED",
    # The network, the host and where things live.
    "PUBLIC_HOST",
    "HOST",
    "PORT",
    "CLOUDFLARE_TUNNEL",
    "DATA_DIR",
    "STATE_DIR",
    "CACHE_DIR",
    "SERVICE_*",
    "SKILLS_DIR",
    # Development switches, every one of which turns a check off.
    "DEBUG_*",
    "FAKE_AGENTS",
)


#: Service-writable limits whose 0 means "no limit": the service may change them, not lift them.
NEVER_OFF = frozenset({"SUBAGENT_TIMEOUT_S", "MAX_CALL_SECONDS", "LOCAL_SILENCE_TIMEOUT"})


def current_actor() -> str:
    """`service` inside anything `jarvis serve` started, `owner` everywhere else."""
    return SERVICE if os.environ.get(ACTOR_ENV) == SERVICE else OWNER


def is_protected(key: str) -> bool:
    """Never service-writable, whatever the owner says: a secret, or a line of defence."""
    field = field_for(key)
    if field is not None and is_secret(field):
        return True
    return any(fnmatch.fnmatchcase(key, pattern) for pattern in PROTECTED_PATTERNS)


#: Every setting `is_protected` covers, by name.
PROTECTED_KEYS = frozenset(
    key for key in map(env_var_name, Settings.model_fields) if is_protected(key)
)


def service_writable(key: str, overrides: Mapping[str, bool]) -> bool:
    """Whether the running service may write `key` right now.

    The owner's override when there is one, else the field's own default — and never a
    protected key, even if an override somehow says so (a hand edit of `config.toml`).
    """
    field = field_for(key)
    if field is None or key in NOT_STORED or is_protected(key):
        return False
    return bool(overrides.get(key, field_service_writable(field)))


def writable_keys(overrides: Mapping[str, bool]) -> list[str]:
    """Every key the service may write right now, in `Settings` order."""
    return [
        key for key in map(env_var_name, Settings.model_fields) if service_writable(key, overrides)
    ]


def refusal(
    key: str, value: object, *, actor: str, overrides: Mapping[str, bool], settings: Settings | None
) -> str | None:
    """Why `actor` may not set `key` to `value`, in one sentence; None when it may.

    `value` is the validated one (`store.validate`), or None for an unset. Two rules are
    about a value rather than a key. The service may move `AGENT_BACKEND` only among the
    agents already enabled, because enabling one is the owner's decision and a default
    outside that set is one `jarvis serve` refuses to start with. And it may tune a limit
    in `NEVER_OFF` but never switch it off: 0 is "no limit" for each, which is a spending
    decision (an unbounded subagent, an unbounded realtime call), and spending is the
    owner's.
    """
    if key in NOT_STORED:
        return (
            "the PIN is not a setting: it is set once, at the keyboard, with `jarvis setup` "
            "(or by the first call)"
        )
    if actor != SERVICE:
        return None
    if not service_writable(key, overrides):
        why = "it is protected" if is_protected(key) else "the owner has not unlocked it"
        return f"the running service may not change {key}: {why}"
    if key in NEVER_OFF and value is not None and not value:
        return f"the running service may not switch {key} off (0 means no limit)"
    if key == "AGENT_BACKEND" and settings is not None and value not in settings.enabled_agents:
        enabled = ", ".join(settings.enabled_agents)
        return f"AGENT_BACKEND may only be switched among the enabled agents ({enabled})"
    return None
