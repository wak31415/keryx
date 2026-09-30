"""Where Keryx keeps its own files, and the modes they are kept at.

The filesystem half of `keryx.config`, with nothing here that knows what a setting is, so
`settings`, `store` and `pin` can all build on it without importing one another.

Four directories, on the XDG base-directory layout on Linux and macOS alike (the one uv, gh,
git and neovim use; `~/Library` is deliberately not it), each honouring its `XDG_*_HOME`:

- `~/.config/keryx` — `KERYX_HOME`: `config.toml` for the plain settings, `secrets.toml`
  for the credentials, the PIN and the Google client file. It is an environment variable
  and never a setting, because it is what says where the settings are;
- `~/.local/share/keryx` — `DATA_DIR`: tasks, transcripts, memory, sign-in tokens;
- `~/.local/state/keryx` — `STATE_DIR`: logs, the restart record, the approval socket;
- `~/.cache/keryx` — `CACHE_DIR`: what can be downloaded again (the wake-word models).

Every file written here goes through `write_private`: a temporary file created 0600 next
to the target, filled, flushed to disk and renamed over it. The rename is the only moment
the target changes, so a crash half way through leaves the old file whole rather than half
of a new one, and there is never an instant at which a secret sits in a file with the
umask's mode.
"""

import contextlib
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

#: The environment variable that moves the configuration out of `~/.config/keryx`.
HOME_ENV = "KERYX_HOME"
#: Its name before the service was Keryx. Never followed: set without `HOME_ENV` it names
#: a directory the owner meant, and quietly using the default instead would start a
#: service with none of their settings — so `serve` refuses until it is renamed.
LEGACY_HOME_ENV = "JARVIS_HOME"
#: The environment variable that moves the Claude CLI's configuration out of `~/.claude`.
CLAUDE_CONFIG_ENV = "CLAUDE_CONFIG_DIR"
#: What each directory is called inside its XDG base directory.
APP_NAME = "keryx"
#: What each was called before the service was Keryx: `keryx migrate` moves them, and
#: `serve` refuses to start while one still holds anything.
LEGACY_APP_NAME = "jarvis"
CONFIG_NAME = "config.toml"
SECRETS_NAME = "secrets.toml"

#: The XDG base directories, by kind: the variable that moves each, and where it is without.
XDG_HOMES: dict[str, tuple[str, str]] = {
    "config": ("XDG_CONFIG_HOME", "~/.config"),
    "data": ("XDG_DATA_HOME", "~/.local/share"),
    "state": ("XDG_STATE_HOME", "~/.local/state"),
    "cache": ("XDG_CACHE_HOME", "~/.cache"),
}

#: Where everything lived before the XDG layout: configuration, data, logs and socket in
#: one directory. Read by `keryx migrate` alone, and looked at by `Settings` only to refuse
#: to start (or to enrol a PIN) while it still holds anything.
LEGACY_HOME = Path("~/.jarvis")
#: Every name Keryx ever wrote into `LEGACY_HOME`. Anything else found there is somebody
#: else's, and is left where it is.
LEGACY_NAMES = frozenset(
    {
        "config.toml",
        "secrets.toml",
        "pin",
        "google_client_secret.json",
        "tasks.db",
        "tasks.db-wal",
        "tasks.db-shm",
        "tasks",
        "calls",
        "memory.md",
        "projects",
        "workspace",
        "pin-failures.json",
        "report_secret",
        "gmail_token.json",
        "gmail_signin.json",
        "google",
        "codex",
        "logs",
        "restart.json",
        "running-version",
        "startup-log-marks.json",
        "approvals",
        "approvals.sock",
    }
)

#: Modes for everything in all four directories. Owner-only, every one of them,
#: because of what is actually in there: `calls/*.log` is every word of every call,
#: `tasks.db` and `tasks/*.md` are what was asked for and what came back, `memory.md` is
#: what Keryx knows about its owner between calls, and `secrets.toml` is every key. The
#: default umask on most machines is 022, which makes all of that world-readable to anyone
#: else with an account.
DATA_DIR_MODE = 0o700
DATA_FILE_MODE = 0o600


def xdg_home(kind: str) -> Path:
    """The XDG base directory of `kind` (`config`, `data`, `state`, `cache`).

    `$XDG_<KIND>_HOME` when it is set to an absolute path; otherwise the specification's
    default. An empty or relative value is ignored, as the specification requires, so a
    stray `XDG_DATA_HOME=.` cannot scatter the data into whatever directory a process
    happened to start in.
    """
    variable, default = XDG_HOMES[kind]
    raw = os.environ.get(variable, "").strip()
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path(default).expanduser()


def keryx_home() -> Path:
    """The configuration directory: `KERYX_HOME`, else `$XDG_CONFIG_HOME/keryx`."""
    raw = os.environ.get(HOME_ENV, "").strip()
    return Path(raw).expanduser() if raw else xdg_home("config") / APP_NAME


def claude_config_dir() -> Path:
    """Where the Claude CLI keeps its own configuration: `$CLAUDE_CONFIG_DIR`, else `~/.claude`.

    Not one of Keryx's directories, but read from several places (the skills, the stored
    login, `CLAUDE.md`, the approval hook), and the CLI moves all of it together.
    """
    raw = os.environ.get(CLAUDE_CONFIG_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path("~/.claude").expanduser()


def claude_user_config() -> Path:
    """The Claude CLI's user-scope config, where its MCP servers are: `~/.claude.json`.

    With `CLAUDE_CONFIG_DIR` set, the CLI keeps it inside that directory instead.
    """
    raw = os.environ.get(CLAUDE_CONFIG_ENV, "").strip()
    return Path(raw).expanduser() / ".claude.json" if raw else Path("~/.claude.json").expanduser()


def default_data_dir() -> Path:
    """`DATA_DIR` when nothing sets it: `$XDG_DATA_HOME/keryx`."""
    return xdg_home("data") / APP_NAME


def default_state_dir() -> Path:
    """`STATE_DIR` when nothing sets it: `$XDG_STATE_HOME/keryx`."""
    return xdg_home("state") / APP_NAME


def default_cache_dir() -> Path:
    """`CACHE_DIR` when nothing sets it: `$XDG_CACHE_HOME/keryx`."""
    return xdg_home("cache") / APP_NAME


def renamed_dirs() -> dict[str, Path]:
    """Where each XDG directory was when the service was Jarvis, by kind."""
    return {kind: xdg_home(kind) / LEGACY_APP_NAME for kind in XDG_HOMES}


def holds_files(path: Path) -> bool:
    """Anything in `path` but directories: what `ensure_dirs` makes is empty directories.

    Stops at the first file, so a data directory full of repositories costs one look.
    """
    if not path.is_dir() or path.is_symlink():
        return path.exists() or path.is_symlink()
    return any(entry.is_symlink() or not entry.is_dir() for entry in path.rglob("*"))


def stray_legacy_home_env() -> str | None:
    """`LEGACY_HOME_ENV`'s value, when it is set and `HOME_ENV` is not; else None."""
    if os.environ.get(HOME_ENV, "").strip():
        return None
    return os.environ.get(LEGACY_HOME_ENV, "").strip() or None


def legacy_home() -> Path:
    """`LEGACY_HOME`, expanded against this process's home directory."""
    return LEGACY_HOME.expanduser()


def legacy_entries(path: Path | None = None) -> list[str]:
    """The names in `path` (default `~/.jarvis`) that Keryx put there, sorted.

    Empty when the directory is not there at all. Only `LEGACY_NAMES` count: an old restart
    script or a hand-made copy of `memory.md` is the owner's, not a sign of an install.
    """
    directory = legacy_home() if path is None else path
    try:
        present = {entry.name for entry in directory.iterdir()}
    except OSError:
        return []
    return sorted(present & LEGACY_NAMES)


def config_file(home: Path | None = None) -> Path:
    return (home or keryx_home()) / CONFIG_NAME


def secrets_file(home: Path | None = None) -> Path:
    return (home or keryx_home()) / SECRETS_NAME


def secure_dir(path: Path) -> Path:
    """Create one of Keryx's directories, owner-only, tightening one that already exists."""
    path.mkdir(parents=True, exist_ok=True)
    # Best effort: a mode that cannot be set (a mounted share, another owner) is not a
    # reason to refuse to run, and `keryx doctor` reports the result either way.
    with contextlib.suppress(OSError):
        os.chmod(path, DATA_DIR_MODE)
    return path


def secure_file(path: Path) -> Path:
    """Tighten one of Keryx's files to owner-only. A file that is not there is fine."""
    with contextlib.suppress(OSError):
        os.chmod(path, DATA_FILE_MODE)
    return path


def write_private(path: Path, text: str) -> Path:
    """Replace `path` with `text`, atomically, at 0600, in an 0700 directory."""
    secure_dir(path.parent)
    # mkstemp creates the file 0600 whatever the umask says: the secret is never readable
    # by anybody else, not even for the moment before a chmod.
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return secure_file(path)


def read_toml(path: Path) -> dict[str, Any]:
    """A TOML file as a dict; a file that is not there is an empty one.

    A file that is there and does not parse raises: silently running on the defaults
    because a hand edit broke the syntax would be a much worse surprise than the error.
    """
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        return {}


def dump_toml(data: dict[str, Any], header: str = "") -> str:
    """`data` as TOML: scalars and arrays first, then one table per nested dict.

    Only the shapes settings take — strings, numbers, booleans, arrays of those, and one
    level of tables — which is why this is twenty lines rather than a dependency.
    """
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    if lines:
        lines.append("")
    tables = {key: value for key, value in data.items() if isinstance(value, dict)}
    for key, value in data.items():
        if key not in tables:
            lines.append(f"{toml_key(key)} = {toml_value(value)}")
    for name, table in tables.items():
        lines += ["", f"[{toml_key(name)}]"]
        lines += [f"{toml_key(key)} = {toml_value(value)}" for key, value in table.items()]
    return "\n".join(lines).strip("\n") + "\n"


def toml_key(key: str) -> str:
    bare = key and all(char.isalnum() or char in "_-" for char in key) and key.isascii()
    return key if bare else _toml_string(key)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for char in map(chr, [*range(0x20), 0x7F]):
        escaped = escaped.replace(char, f"\\u{ord(char):04x}")
    return f'"{escaped}"'


def toml_value(value: Any) -> str:
    """One value as TOML: what `dump_toml` writes, and what a plugin's template is filled with.

    Every string is quoted and escaped here, so nothing a value holds can end its line.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return _toml_string(str(value))
