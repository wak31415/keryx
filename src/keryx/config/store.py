"""The configuration store: `KERYX_HOME/config.toml` and `KERYX_HOME/secrets.toml`.

The only code that writes either file. Plain settings go in `config.toml`; every secret
(a field declared `repr=False`) goes in `secrets.toml`; both are 0600 in an 0700
directory, and both are replaced whole and atomically (`files.write_private`). That split
is the one Claude Code, Codex and their peers make, and for the same reason: the plain
file can be read, diffed and pasted into a bug report, and the other never is.

Every write is validated against `Settings` first, the same validators a load runs, so
the store never holds a value that would stop `keryx serve` from starting — and every
write names its *actor*, which `keryx.config.permissions` checks.

Reading is `Settings`' job (its sources are these two files); this module reads them only
to rewrite them, and to say where a value came from.
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from keryx.config import permissions
from keryx.config.files import (
    config_file,
    dump_toml,
    keryx_home,
    read_toml,
    secrets_file,
    secure_file,
    write_private,
)
from keryx.config.pin import PIN_PATTERN, PIN_RULE, pin_file, read_enrolled_pin, write_enrolled_pin
from keryx.config.settings import (
    GOOGLE_CLIENT_FILE,
    LEGACY_CLIENT_FILE,
    META_TABLES,
    NOT_STORED,
    PLACEHOLDER_KEY,
    Settings,
    canonical_key,
    env_var_name,
    env_var_names,
    field_for,
    field_group,
    is_secret,
    parse_google_client,
)

CONFIG_HEADER = (
    "Keryx's settings. `keryx config set KEY VALUE` changes one, and `keryx config list`\n"
    "shows every one there is; editing this file by hand works too. Credentials are not\n"
    "here: they are in secrets.toml beside it."
)
SECRETS_HEADER = (
    "Keryx's credentials, readable by you alone. Set one with\n"
    "`keryx config set KEY --stdin`, which keeps it out of your shell history."
)

#: What `source_of` answers.
FROM_INIT = "command line"
FROM_ENV = "environment"
FROM_SECRETS = "secrets.toml"
FROM_CONFIG = "config.toml"
FROM_PIN_FILE = "KERYX_HOME/pin"
FROM_DEFAULT = "default"

#: Settings whose value is a path: a legacy `.env` meant them relative to the directory
#: the service ran in, which is the `.env`'s own, so an import resolves them there.
PATH_KEYS = frozenset(
    {"DATA_DIR", "STATE_DIR", "CACHE_DIR", "PROJECTS_ROOT", "SKILLS_DIR"}
)


class ConfigError(ValueError):
    """A write that was refused or could not be validated, in a sentence to print."""


class _Probe(Settings):
    """`Settings` fed from its arguments alone: what a value would validate to."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def validate(values: Mapping[str, Any]) -> dict[str, Any]:
    """`{KEY: raw}` as the values the store would keep, or a `ConfigError` naming each problem.

    A raw value is what somebody typed — `"true"`, `"3"`, `"a,b"`, `'{"x": "/y"}'` — or
    already the right type. What comes back is its JSON shape (a `Path` as its string), and
    None for "unset".
    """
    fields = {}
    for key in values:
        name = field_for(key)
        if name is None:
            raise ConfigError(f"there is no setting called {key} (`keryx config list`)")
        fields[name] = key
    arguments = {name: values[key] for name, key in fields.items()}
    arguments.setdefault("openai_api_key", PLACEHOLDER_KEY)
    try:
        probe = _Probe(**arguments)
    except ValidationError as error:
        problems = []
        for detail in error.errors():
            name = str(detail["loc"][0]) if detail.get("loc") else ""
            message = str(detail.get("msg", "is not valid")).removeprefix("Value error, ")
            problems.append(f"{env_var_name(name) if name else 'the value'} {message}")
        raise ConfigError("; ".join(problems)) from None
    dumped = probe.model_dump(mode="json", include=set(fields))
    return {key: dumped[name] for name, key in fields.items()}


def default_value(key: str) -> Any:
    """What `key` is when nothing sets it, in the same JSON shape the store keeps."""
    name = field_for(key)
    assert name is not None, key
    info = Settings.model_fields[name]
    if info.is_required():
        return None
    value = info.get_default(call_default_factory=True)
    return validate({key: value})[key] if value is not None else None


@dataclass
class ImportReport:
    """What `import_env` did, for `keryx config import-env` to print."""

    imported: list[str] = field(default_factory=list)
    defaults: list[str] = field(default_factory=list)
    #: In the `.env` and already in the store, whose value was the one in use: left alone.
    kept: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    pin: str | None = None
    client_file: Path | None = None
    renamed_to: Path | None = None


class ConfigStore:
    """The two files in one `KERYX_HOME` (default: `keryx_home()` when asked)."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home

    @property
    def home(self) -> Path:
        return self._home or keryx_home()

    @property
    def config_path(self) -> Path:
        return config_file(self.home)

    @property
    def secrets_path(self) -> Path:
        return secrets_file(self.home)

    # --- reading ----------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        return read_toml(self.config_path)

    def _secrets(self) -> dict[str, Any]:
        return read_toml(self.secrets_path)

    def stored(self) -> dict[str, Any]:
        """Every setting either file holds, by key. Not the meta tables."""
        values = {
            key: value
            for key, value in self._config().items()
            if key not in META_TABLES
        }
        values.update(self._secrets())
        return values

    def overrides(self) -> dict[str, bool]:
        """The owner's `lock`/`unlock` decisions: `{KEY: may the service write it}`."""
        table = self._config().get("service_writable", {})
        return {str(key): bool(value) for key, value in table.items()} if isinstance(
            table, dict
        ) else {}

    def secrets_in_config(self) -> list[str]:
        """Secrets somebody put in `config.toml` by hand, where a bug report would paste them."""
        return sorted(
            key
            for key in self._config()
            if (name := field_for(key)) is not None and is_secret(name)
        )

    def walked_sections(self) -> list[str]:
        """The `keryx setup` sections already walked: not asked again unless reviewing.

        What `doctor` cannot judge — the settings kept at their defaults, Google left for
        later — is only known to be settled because somebody went through it and said so.
        """
        table = self._config().get("setup", {})
        walked = table.get("walked", []) if isinstance(table, dict) else []
        return [str(name) for name in walked]

    def source_of(self, key: str, settings: Settings | None = None) -> str:
        """Where `key`'s value comes from, in the order `Settings` looks (see its docstring)."""
        name = field_for(key)
        if name is None:
            raise ConfigError(f"there is no setting called {key}")
        key = env_var_name(name)
        if any(os.environ.get(each, "").strip() for each in env_var_names(name)):
            return FROM_ENV
        if key not in NOT_STORED:
            if key in self._secrets():
                return FROM_SECRETS
            if key in self._config():
                return FROM_CONFIG
        if key == "KERYX_PIN" and settings is not None and pin_file(settings.config_dir).exists():
            return FROM_PIN_FILE
        return FROM_DEFAULT

    def describe(self, settings: Settings) -> list[dict[str, Any]]:
        """Every setting as `keryx config list` shows it. A secret's value is never here."""
        overrides = self.overrides()
        dumped = settings.model_dump(mode="json")
        rows = []
        for name, info in Settings.model_fields.items():
            key = env_var_name(name)
            secret = is_secret(name)
            source = self.source_of(key, settings)
            rows.append(
                {
                    "key": key,
                    "group": field_group(name),
                    "description": info.description or "",
                    "required": info.is_required(),
                    "set": source != FROM_DEFAULT,
                    "source": source,
                    "secret": secret,
                    "service_writable": permissions.service_writable(key, overrides),
                    "protected": permissions.is_protected(key),
                    "value": None if secret else dumped[name],
                }
            )
        return rows

    # --- writing ----------------------------------------------------------------------

    def set(
        self,
        values: Mapping[str, Any],
        *,
        actor: str = permissions.OWNER,
        settings: Settings | None = None,
    ) -> dict[str, Any]:
        """Validate and store `{KEY: raw}`; a raw None unsets. Returns what was stored.

        All or nothing: one refused or invalid value and neither file is touched.
        """
        keys = {canonical_key(key): value for key, value in values.items()}
        for key in keys:
            if field_for(key) is None:
                raise ConfigError(f"there is no setting called {key} (`keryx config list`)")
            if key in NOT_STORED:  # before validating: a refusal must not echo a PIN's rule
                raise ConfigError(permissions.refusal(key, None, actor=actor, overrides={},
                                                      settings=settings) or key)
        cleaned = validate({key: value for key, value in keys.items() if value is not None})
        cleaned.update({key: None for key, value in keys.items() if value is None})
        overrides = self.overrides()
        for key, value in cleaned.items():
            why = permissions.refusal(
                key, value, actor=actor, overrides=overrides, settings=settings
            )
            if why is not None:
                raise ConfigError(why)
        self._check_whole(cleaned)
        self._write(cleaned)
        return cleaned

    def unset(self, keys: Iterable[str], *, actor: str = permissions.OWNER) -> list[str]:
        """Remove `keys` from the store, so the next source down (or the default) applies.

        Returns the ones that were actually there.
        """
        wanted = [canonical_key(key) for key in keys]
        present = set(self.stored())
        self.set({key: None for key in wanted}, actor=actor)
        return [key for key in wanted if key in present]

    def lock(self, key: str) -> None:
        """Stop the running service writing `key`."""
        self._override(key, False)

    def unlock(self, key: str) -> None:
        """Let the running service write `key`; refused for `permissions.PROTECTED_KEYS`."""
        key = canonical_key(key)
        if permissions.is_protected(key):
            raise ConfigError(f"{key} is protected: the running service may never change it")
        self._override(key, True)

    def _override(self, key: str, writable: bool) -> None:
        key = canonical_key(key)
        if field_for(key) is None or key in NOT_STORED:
            raise ConfigError(f"there is no setting called {key}")
        config = self._config()
        table = config.setdefault("service_writable", {})
        table[key] = writable
        write_private(self.config_path, dump_toml(config, CONFIG_HEADER))

    def mark_walked(self, section: str, walked: bool = True) -> None:
        """Remember that `keryx setup` has been through `section` (or forget that it has)."""
        config = self._config()
        table = config.setdefault("setup", {})
        names = [name for name in table.get("walked", []) if name != section]
        if walked:
            names.append(section)
        table["walked"] = names
        write_private(self.config_path, dump_toml(config, CONFIG_HEADER))

    def drop_retired(self, keys: Iterable[str]) -> list[str]:
        """Remove settings that are no longer settings — `keys`, whichever file holds them.

        No validation, because there is nothing left to validate them against: this is how a
        setting a plugin replaced (`keryx.plugins.RETIRED_KEYS`) leaves the store once its
        value has moved into the plugin's own file. A key that is still a setting is refused.
        Returns the ones that were there.
        """
        wanted = {key.strip().upper() for key in keys}
        if live := sorted(key for key in wanted if field_for(key) is not None):
            raise ConfigError(f"{', '.join(live)} are still settings: `keryx config unset`")
        dropped = []
        files = ((self.config_path, CONFIG_HEADER), (self.secrets_path, SECRETS_HEADER))
        for path, header in files:
            data = read_toml(path)
            gone = [key for key in data if key in wanted]
            if gone:
                write_private(path, dump_toml(
                    {key: value for key, value in data.items() if key not in wanted}, header
                ))
                dropped += gone
        return sorted(dropped)

    def _check_whole(self, cleaned: Mapping[str, Any]) -> None:
        """Refuse a write after which the store as a whole would not load.

        Each value was validated on its own; this is the same load `keryx serve` does, over
        everything the store would hold, so a combination no single value shows is caught
        here rather than at the next start.
        """
        merged = {key: value for key, value in self.stored().items() if key not in NOT_STORED}
        merged.update(cleaned)
        validate({key: value for key, value in merged.items()
                  if value is not None and field_for(key) is not None})

    def _write(self, cleaned: Mapping[str, Any]) -> None:
        """Put each key in the file its kind belongs in, and out of the other one."""
        config, secrets = self._config(), self._secrets()
        before = (dict(config), dict(secrets))
        for key, value in cleaned.items():
            name = field_for(key)
            assert name is not None
            home, other = (secrets, config) if is_secret(name) else (config, secrets)
            other.pop(key, None)
            if value is None:
                home.pop(key, None)
            else:
                home[key] = value
        if secrets != before[1]:
            write_private(self.secrets_path, dump_toml(secrets, SECRETS_HEADER))
        if config != before[0]:
            write_private(self.config_path, dump_toml(config, CONFIG_HEADER))

    # --- a legacy .env ------------------------------------------------------------------

    def import_env(
        self, path: Path, *, today: date | None = None, actor: str = permissions.OWNER
    ) -> ImportReport:
        """Move a legacy `.env` into the store, then rename it out of the way.

        Settings still at their defaults are not copied, so a `.env` made from the old
        example file does not pin sixty defaults for ever. `KERYX_PIN` goes to
        `KERYX_HOME/pin`, the PIN's own store — unless a *different* PIN is already there,
        in which case nothing at all is written: until now the `.env` one was in use, and
        silently switching to the other would lock the owner out of their own phone. The
        Google client file (`GOOGLE_CLIENT_SECRETS_FILE`, or `.secrets/client_secret.json`
        beside the `.env`) is copied to `KERYX_HOME/google_client_secret.json`.
        """
        if actor != permissions.OWNER:
            # It writes protected settings and the PIN by its nature: the owner's alone.
            raise ConfigError("only the owner may import settings, at their own terminal")
        path = path.resolve()
        parsed = _read_env(path)
        report = ImportReport(unknown=parsed.unknown)
        stored = self.stored()
        for key, value in parsed.cleaned.items():
            if key in stored:
                report.kept.append(key)
            elif value == default_value(key):
                report.defaults.append(key)
            else:
                report.imported.append(key)
        keep = {key: parsed.cleaned[key] for key in report.imported}
        pin, client_text = parsed.pin, parsed.client_text
        # Everything that can refuse has been asked; from here on it is only writing.
        if pin is not None:
            report.pin = self._import_pin(self.home, pin)
        if client_text is not None:
            report.client_file = write_private(self.home / GOOGLE_CLIENT_FILE, client_text)
        self._write(keep)
        report.renamed_to = _rename_aside(path, today or date.today())
        return report

    @staticmethod
    def check_import(path: Path) -> str | None:
        """Everything `import_env` could refuse about `path`, asked without writing a thing.

        Returns the `.env`'s PIN, if it has one, for a caller that has PINs of its own to
        compare it with (`keryx migrate`, before it moves anything).
        """
        return _read_env(path.resolve()).pin

    @staticmethod
    def _import_pin(home: Path, pin: str) -> str:
        existing = read_enrolled_pin(home)
        if pin_file(home).exists() and existing != pin:
            raise ConfigError(
                f"the .env's KERYX_PIN is not the PIN in {pin_file(home)}. The .env one "
                "has been the PIN in use; to keep it, delete that file and import again. "
                "Nothing was imported."
            )
        if existing == pin:
            return "already there"
        if not write_enrolled_pin(home, pin):
            raise ConfigError(f"could not write the PIN to {pin_file(home)}")
        return "moved to KERYX_HOME/pin"


@dataclass
class _EnvFile:
    """A legacy `.env` read and validated: what `import_env` would write."""

    cleaned: dict[str, Any]
    unknown: list[str]
    pin: str | None
    client_text: str | None


def _read_env(path: Path) -> _EnvFile:
    """`path` parsed and checked, raising `ConfigError` for anything an import would refuse."""
    unknown: list[str] = []
    raw: dict[str, str] = {}
    for key, value in dotenv_values(path).items():
        key = key.strip().upper()
        if value is None or not value.strip():
            continue
        name = field_for(key)
        if name is None:
            unknown.append(key)
            continue
        raw[env_var_name(name)] = value.strip()
    pin = raw.pop("KERYX_PIN", None)
    if pin is not None and not PIN_PATTERN.fullmatch(pin):
        raise ConfigError(f"KERYX_PIN {PIN_RULE}")
    client_raw = raw.pop("GOOGLE_CLIENT_SECRETS_FILE", None)
    for key in PATH_KEYS & raw.keys():
        candidate = Path(raw[key]).expanduser()
        raw[key] = str(candidate if candidate.is_absolute() else path.parent / candidate)
    cleaned = validate(raw)
    client = Path(client_raw).expanduser() if client_raw else LEGACY_CLIENT_FILE
    client = client if client.is_absolute() else path.parent / client
    client_text = client.read_text(encoding="utf-8") if client.is_file() else None
    if client_text is not None:
        try:
            parse_google_client(client_text)
        except ValueError as error:
            raise ConfigError(f"{client} is not a Google OAuth client file: {error}") from None
    return _EnvFile(cleaned, unknown, pin, client_text)


def _rename_aside(path: Path, today: date) -> Path:
    """`path` renamed to `<name>.imported-<date>`, never over an earlier one."""
    target = path.with_name(f"{path.name}.imported-{today.isoformat()}")
    count = 1
    while target.exists():
        count += 1
        target = path.with_name(f"{path.name}.imported-{today.isoformat()}-{count}")
    path.rename(target)
    # It still holds every secret the store now holds; no looser than the store.
    secure_file(target)
    return target
