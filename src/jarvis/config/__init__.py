"""Jarvis's configuration: the settings, where they are stored, and who may change them.

- `settings` — `Settings`, every field with its description, group and default permission;
- `store` — `JARVIS_HOME/config.toml` and `secrets.toml`, the only code that writes them;
- `permissions` — which keys the running service may change (`PROTECTED_KEYS` never);
- `pin` — `DATA_DIR/pin`, the PIN's own store, written once;
- `files` — the directory, the modes and the atomic writes under all of them.

Everything the rest of Jarvis imported from the old `jarvis.config` module is re-exported
here, so `from jarvis.config import Settings, secure_dir` still works.
"""

from jarvis.config.files import (
    DATA_DIR_MODE,
    DATA_FILE_MODE,
    HOME_ENV,
    config_file,
    jarvis_home,
    secrets_file,
    secure_dir,
    secure_file,
    write_private,
)
from jarvis.config.pin import (
    PIN_FILE_NAME,
    PIN_FROM_ENV,
    PIN_FROM_FILE,
    PIN_MAX_DIGITS,
    PIN_PATTERN,
    PIN_RULE,
    is_trivial_pin,
    pin_file,
    read_enrolled_pin,
    replace_pin_at_keyboard,
    write_enrolled_pin,
)
from jarvis.config.settings import (
    CLUSTER_WORD,
    GOOGLE_CLIENT_FILE,
    GROUPS,
    OPTIONAL_STR_FIELDS,
    OWNER_FALLBACK,
    PLACEHOLDER_KEY,
    AgentName,
    Settings,
    env_var_name,
    field_for,
    field_group,
    field_service_writable,
    is_secret,
    load_settings,
    parse_google_client,
)

__all__ = [
    "CLUSTER_WORD",
    "DATA_DIR_MODE",
    "DATA_FILE_MODE",
    "GOOGLE_CLIENT_FILE",
    "GROUPS",
    "HOME_ENV",
    "OPTIONAL_STR_FIELDS",
    "OWNER_FALLBACK",
    "PIN_FILE_NAME",
    "PIN_FROM_ENV",
    "PIN_FROM_FILE",
    "PIN_MAX_DIGITS",
    "PIN_PATTERN",
    "PIN_RULE",
    "PLACEHOLDER_KEY",
    "AgentName",
    "Settings",
    "config_file",
    "env_var_name",
    "field_for",
    "field_group",
    "field_service_writable",
    "is_secret",
    "is_trivial_pin",
    "jarvis_home",
    "load_settings",
    "parse_google_client",
    "pin_file",
    "read_enrolled_pin",
    "replace_pin_at_keyboard",
    "secrets_file",
    "secure_dir",
    "secure_file",
    "write_enrolled_pin",
    "write_private",
]
