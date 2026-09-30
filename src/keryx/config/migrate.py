"""`keryx migrate`: move an install to where Keryx keeps things, with the service stopped
while it does. Two kinds of install are moved, and a machine may have either:

- **Jarvis's XDG directories** (`~/.config/jarvis` and its three siblings), from before
  the service was renamed Keryx. Each moves whole to its Keryx twin — whole, because what
  is inside has grown past any list of names (`tools/`, the owner's own tools' data, the
  approval audit) — and the unit Jarvis ran as is retired for Keryx's.
- **`~/.jarvis`** (and a working-directory `.env`), from before the XDG layout. Everything
  lived in one directory — configuration, secrets, the PIN, data, logs and the approval
  socket — and on many machines the live configuration was still a `.env` in the checkout.
  Each known entry moves to where it belongs now (`keryx.config.files`).

In three phases:

1. **The plan** (`make_plan`) reads and never writes. It finds the sources — `~/.jarvis`, a
   `DATA_DIR` the legacy store or the `.env` recorded, the `.env` and `.secrets/` in the
   working directory — and maps every name Keryx wrote to its destination. Anything that
   would have to overwrite something is a *conflict*, and a plan with one is never run: the
   owner decides which copy is the real one, not this.
2. **The run** (`run`): stop the service, checkpoint `tasks.db`, move, rewrite the paths the
   database holds and the imports of the tools in `DATA_DIR/tools`, import the `.env`, unset
   a `DATA_DIR` that named the old directory, pin the Cloudflare tunnel the machine has been
   running, tighten modes, and rename `~/.jarvis` aside with whatever is left in it. Nothing
   is ever deleted but a stale socket and directories `ensure_dirs` made empty.
3. **Afterwards**: retire Jarvis's unit, re-render the service and the approval hook, whose
   files name the old paths, and start the service again.

Every step can be run twice. An entry already at its destination and gone from its source
is simply not in the next plan, so a migration that stopped half way finishes on a second
run, and one that finished has nothing left to do.

`Settings.storage_refusal` keeps `keryx serve` from starting until this has run: the
signal is the legacy data still being there, never the new directory existing, because
`ensure_dirs` creates that on the first command of any kind.
"""

import contextlib
import errno
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from keryx.config.files import (
    DATA_DIR_MODE,
    DATA_FILE_MODE,
    HOME_ENV,
    LEGACY_APP_NAME,
    LEGACY_HOME_ENV,
    LEGACY_NAMES,
    default_data_dir,
    default_state_dir,
    holds_files,
    keryx_home,
    legacy_home,
    read_toml,
    renamed_dirs,
    secure_dir,
    stray_legacy_home_env,
    write_private,
    xdg_home,
)
from keryx.config.pin import PIN_FILE_NAME, read_enrolled_pin
from keryx.config.settings import (
    GOOGLE_CLIENT_FILE,
    LEGACY_CLIENT_FILE,
    LEGACY_ENV_FILE,
    env_var_names,
)
from keryx.config.store import ConfigError, ConfigStore

#: Where each name in `LEGACY_NAMES` belongs now, by directory.
CONFIG = "config"
DATA = "data"
STATE = "state"
CATEGORIES: dict[str, str] = {
    **dict.fromkeys(("config.toml", "secrets.toml", PIN_FILE_NAME, GOOGLE_CLIENT_FILE), CONFIG),
    **dict.fromkeys(
        (
            "tasks.db",
            "tasks.db-wal",
            "tasks.db-shm",
            "tasks",
            "calls",
            "memory.md",
            "projects",
            "workspace",
            # A counter rewritten on every wrong guess is data, and it is kept with the data
            # rather than the state, which people treat as more disposable: losing it would
            # hand a PIN guesser a fresh budget.
            "pin-failures.json",
            "report_secret",
            "gmail_token.json",
            "gmail_signin.json",
            "google",
            "codex",
        ),
        DATA,
    ),
    **dict.fromkeys(
        ("logs", "restart.json", "running-version", "startup-log-marks.json", "approvals"),
        STATE,
    ),
}
#: Left behind by a broker that is no longer listening; removed, never moved.
SOCKET_NAME = "approvals.sock"
assert set(CATEGORIES) | {SOCKET_NAME} == LEGACY_NAMES

#: The working-directory `.env` that was configuration (`LEGACY_CLIENT_FILE` the other).
ENV_NAME = LEGACY_ENV_FILE.name
TASK_DB = "tasks.db"
#: How long the service may take to stop before the migration gives up, untouched.
STOP_TIMEOUT_S = 30.0
STOP_POLL_S = 0.5
#: The tunnel `CLOUDFLARE_TUNNEL` meant when it was left unset on a Jarvis install.
TUNNEL_KEY = "CLOUDFLARE_TUNNEL"
#: Where the owner's own voice tools live in `DATA_DIR`, and an import of the old package
#: in one of them (`from jarvis.tools.custom import custom_tool`), which no longer loads.
TOOLS_DIR = "tools"
OLD_IMPORT = re.compile(rf"^(\s*(?:from|import)\s+){LEGACY_APP_NAME}(?=[.\s]|$)", re.MULTILINE)
#: What the service was installed as before it was Keryx, by manager: stopped before
#: anything moves, and removed once everything has. The first is the one to stop.
OLD_UNITS = {
    "systemd": ("jarvis.service",),
    "launchd": ("dev.jarvis.agent", "dev.jarvis.tunnel"),
}
#: What an entry is going to have done to it.
MOVE = "move"
REMOVE = "remove"
DUPLICATE = "duplicate"  # already at the destination, byte for byte: the source goes


class MigrationError(RuntimeError):
    """The migration stopped, in a sentence to print; what was done before it stands."""


@dataclass(frozen=True)
class Step:
    """One entry: where it is, where it goes, and what happens to it."""

    source: Path
    destination: Path | None
    action: str = MOVE

    def describe(self) -> str:
        if self.action == REMOVE:
            return f"{_tilde(self.source)}: removed (a socket nothing listens on)"
        assert self.destination is not None
        if self.action == DUPLICATE:
            return f"{_tilde(self.source)}: already at {_tilde(self.destination)}, removed"
        return f"{_tilde(self.source)}  →  {_tilde(self.destination)}"


@dataclass
class Plan:
    """What `run` would do, worked out without touching anything."""

    legacy: Path
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    steps: list[Step] = field(default_factory=list)
    #: Entries that would overwrite something, each as a sentence naming it.
    conflicts: list[str] = field(default_factory=list)
    #: The data directories whose paths `tasks.db` holds, and that are moving away.
    old_data_dirs: list[Path] = field(default_factory=list)
    #: Working-directory `.env` files to import into the store.
    env_files: list[Path] = field(default_factory=list)
    #: Whether the store's `DATA_DIR` names the legacy directory, and is to be unset.
    unset_data_dir: bool = False
    #: Whether `~/.jarvis` is renamed aside at the end: not while it is still in use.
    retire: bool = False
    #: Directories whose leftovers are worth listing afterwards (`.secrets/`).
    working_dirs: list[Path] = field(default_factory=list)
    #: The tools in `DATA_DIR/tools` whose imports name the old package, by file name.
    old_imports: list[str] = field(default_factory=list)
    #: Whether `CLOUDFLARE_TUNNEL` is pinned to the tunnel a Jarvis install ran by default.
    pin_tunnel: bool = False

    @property
    def empty(self) -> bool:
        return not (self.steps or self.env_files or self.unset_data_dir or self.retire)

    def describe(self) -> list[str]:
        """The plan as the lines `keryx migrate` prints before it asks."""
        lines = [
            "Where Keryx keeps things from now on:",
            f"  config  {_tilde(self.config_dir)}  (KERYX_HOME: settings, secrets, the PIN)",
            f"  data    {_tilde(self.data_dir)}  (DATA_DIR)",
            f"  state   {_tilde(self.state_dir)}  (STATE_DIR: logs, the approval socket)",
            f"  cache   {_tilde(self.cache_dir)}  (CACHE_DIR)",
            "",
        ]
        lines += [f"  {step.describe()}" for step in self.steps]
        for env in self.env_files:
            lines.append(
                f"  {_tilde(env)}: imported into {_tilde(self.config_dir)}, then renamed aside"
            )
        if self.old_data_dirs:
            olds = ", ".join(_tilde(path) for path in self.old_data_dirs)
            lines.append(f"  tasks.db: paths under {olds} rewritten to {_tilde(self.data_dir)}")
        for name in self.old_imports:
            lines.append(f"  {TOOLS_DIR}/{name}: its `{LEGACY_APP_NAME}` imports say `keryx`")
        if self.unset_data_dir:
            lines.append(f"  DATA_DIR in the store names {_tilde(self.legacy)}: unset")
        if self.pin_tunnel:
            lines.append(
                f"  {TUNNEL_KEY}: set to {LEGACY_APP_NAME}, the tunnel this machine has been "
                "running (the default is keryx now)"
            )
        if self.retire:
            lines.append(
                f"  {_tilde(self.legacy)}: renamed {_tilde(self.legacy)}.migrated-<date>, "
                "with whatever is left in it"
            )
        return lines


# --- the plan ------------------------------------------------------------------------------


def make_plan(
    working_dirs: Sequence[Path], environ: dict[str, str] | None = None
) -> Plan:
    """What a migration would do on this machine. Reads; never writes.

    `working_dirs` are where a legacy `.env` and `.secrets/` may be: the directory the
    command runs in, and the checkout the service runs in.
    """
    env = dict(os.environ if environ is None else environ)
    legacy = legacy_home()
    config_dir = keryx_home()
    new_store = ConfigStore(config_dir)
    renamed = renamed_dirs()
    # Jarvis's store, still in its old directory: until it has moved, it is the settings.
    old_config = _read_quietly(renamed["config"] / "config.toml")
    legacy_config = _read_quietly(legacy / "config.toml")
    working = list(dict.fromkeys(path.resolve() for path in working_dirs))
    env_files = [path / ENV_NAME for path in working if (path / ENV_NAME).is_file()]

    # DATA_DIR as it will be once the migration is done: the environment first, then the
    # store (the new one, then the legacy one about to become it), then the `.env`. One in
    # a file that names the legacy directory is about to be unset, so it means the default;
    # one in the environment cannot be, and keeps the legacy directory in use.
    from_env = bool(env.get("DATA_DIR", "").strip())
    stores = (_stored_quietly(new_store), old_config, legacy_config)
    configured = _recorded_data_dir(env, stores, env_files, working)
    unset = configured == legacy and not from_env
    data_dir = default_data_dir() if configured is None or unset else configured
    state_dir = _setting_dir("STATE_DIR", env, stores) or default_state_dir()
    cache_dir = _setting_dir("CACHE_DIR", env, stores) or xdg_home("cache") / "keryx"
    plan = Plan(legacy, config_dir, data_dir, state_dir, cache_dir, env_files=env_files,
                working_dirs=working, unset_data_dir=unset)

    targets = {CONFIG: config_dir, DATA: data_dir, STATE: state_dir}
    twins = {"config": config_dir, "data": data_dir, "state": state_dir, "cache": cache_dir}
    claimed: dict[Path, Path] = {}
    moving = [kind for kind, old in renamed.items() if old != twins[kind] and holds_files(old)]
    for kind in moving:
        old = renamed[kind]
        if (old / SOCKET_NAME).exists() or (old / SOCKET_NAME).is_symlink():
            plan.steps.append(Step(old / SOCKET_NAME, None, REMOVE))
        _add_tree(plan, claimed, old, twins[kind])
        if kind == "data":
            plan.old_data_dirs.append(old)
            plan.old_imports = _old_imports(old / TOOLS_DIR)
    if moving and _entries(legacy):
        plan.conflicts.append(
            f"both {_tilde(legacy)} and {_tilde(renamed[moving[0]])} hold Jarvis's files; "
            "keep one and move the other out of the way"
        )
    if stray := stray_legacy_home_env():
        plan.conflicts.append(
            f"{LEGACY_HOME_ENV}={stray} is set: rename it to {HOME_ENV} (or unset it) first"
        )
    # The legacy directory, and a DATA_DIR kept elsewhere: its data stays where it is, and
    # the PIN, the logs and the restart record in it move out.
    sources = list(dict.fromkeys([legacy, data_dir]))
    for source in dict.fromkeys(sources):
        moved_data = False
        for name in _entries(source):
            path = source / name
            if name == SOCKET_NAME:
                plan.steps.append(Step(path, None, REMOVE))
                continue
            destination = targets[CATEGORIES[name]] / name
            if destination == path:
                continue
            moved_data = moved_data or CATEGORIES[name] == DATA
            _add(plan, claimed, path, destination)
        if moved_data and source != data_dir:
            plan.old_data_dirs.append(source)
    for directory in working:
        client = directory / LEGACY_CLIENT_FILE
        if client.is_file():
            _add(plan, claimed, client, config_dir / GOOGLE_CLIENT_FILE)
    _check_pins(plan, env_files, sources)
    for env_file in env_files:
        try:
            ConfigStore.check_import(env_file)
        except ConfigError as error:
            plan.conflicts.append(f"{env_file}: {error}")
    in_use = legacy in (config_dir, data_dir, state_dir)
    plan.retire = legacy.is_dir() and not in_use
    tunnel_named = env.get(TUNNEL_KEY, "").strip() or any(
        str(store.get(TUNNEL_KEY, "")).strip() for store in stores
    ) or any(dotenv_values(path).get(TUNNEL_KEY) for path in env_files)
    plan.pin_tunnel = bool(plan.steps) and not tunnel_named
    if in_use and legacy.is_dir():
        plan.conflicts.append(
            f"{_tilde(legacy)} is still where {_naming(legacy, config_dir, data_dir, state_dir)}"
            f" points; unset it in the environment ({HOME_ENV}, DATA_DIR, STATE_DIR) first"
        )
    return plan


def _add(plan: Plan, claimed: dict[Path, Path], source: Path, destination: Path) -> None:
    """One move, or the conflict it would be."""
    if destination in claimed:
        plan.conflicts.append(
            f"{_tilde(source)} and {_tilde(claimed[destination])} would both become "
            f"{_tilde(destination)}; keep one"
        )
        return
    claimed[destination] = source
    if _occupied(destination):
        if source.is_file() and destination.is_file() and _same_bytes(source, destination):
            plan.steps.append(Step(source, destination, DUPLICATE))
            return
        plan.conflicts.append(
            f"{_tilde(source)} and {_tilde(destination)} both exist; keep one and "
            "move the other out of the way"
        )
        return
    plan.steps.append(Step(source, destination))


def _add_tree(plan: Plan, claimed: dict[Path, Path], source: Path, destination: Path) -> None:
    """One directory moved whole, or the conflict it would be: anything in `destination`
    but the empty directories `ensure_dirs` makes would be overwritten or mixed in."""
    claimed[destination] = source
    if holds_files(destination):
        plan.conflicts.append(
            f"{_tilde(source)} and {_tilde(destination)} both hold files; keep one and move "
            "the other out of the way"
        )
        return
    plan.steps.append(Step(source, destination))


def _old_imports(tools: Path) -> list[str]:
    """The tools in `tools` that import the old package by name, by file name."""
    try:
        files = sorted(path for path in tools.glob("*.py") if path.is_file())
    except OSError:
        return []
    return [path.name for path in files if OLD_IMPORT.search(_text_quietly(path))]


def _text_quietly(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _check_pins(plan: Plan, env_files: list[Path], sources: Iterable[Path]) -> None:
    """A `.env` PIN that differs from an enrolled one would lock the owner out: refused here,
    before anything moves, rather than half way through (`ConfigStore._import_pin`)."""
    enrolled = {
        path: read_enrolled_pin(path) for path in (plan.config_dir, *sources)
        if (path / PIN_FILE_NAME).exists()
    }
    for env_file in env_files:
        values = dotenv_values(env_file)
        pin = next((v.strip() for n in env_var_names("pin") if (v := values.get(n))), "")
        for directory, value in enrolled.items():
            if pin and value != pin:
                plan.conflicts.append(
                    f"the PIN in {env_file} is not the PIN in "
                    f"{_tilde(directory / PIN_FILE_NAME)}. The .env one has been the PIN in "
                    "use; to keep it, delete that file"
                )


def _recorded_data_dir(
    env: dict[str, str],
    stores: Sequence[dict[str, Any]],
    env_files: list[Path],
    working: list[Path],
) -> Path | None:
    """The `DATA_DIR` the service has been using, absolute; None for the default.

    A relative one meant the service's working directory: the `.env`'s own for a `.env`,
    the checkout for the stores (`Settings` refuses one now, so none is written again).
    """
    base = working[0] if working else Path.cwd()
    candidates = [
        (env.get("DATA_DIR", ""), base),
        *[(store.get("DATA_DIR", ""), base) for store in stores],
        *[(dotenv_values(path).get("DATA_DIR") or "", path.parent) for path in env_files],
    ]
    for value, relative_to in candidates:
        if text := str(value).strip():
            path = Path(text).expanduser()
            return path if path.is_absolute() else relative_to / path
    return None


def _setting_dir(
    key: str, env: dict[str, str], stores: Sequence[dict[str, Any]]
) -> Path | None:
    values = [env.get(key, ""), *(str(store.get(key, "")) for store in stores)]
    value = next((text.strip() for text in values if text.strip()), "")
    return Path(value).expanduser() if value else None


def _stored_quietly(store: ConfigStore) -> dict[str, Any]:
    try:
        return store.stored()
    except Exception:  # a store that does not parse: `keryx doctor` says so
        return {}


def _read_quietly(path: Path) -> dict[str, Any]:
    try:
        return read_toml(path)
    except Exception:
        return {}


def _entries(directory: Path) -> list[str]:
    try:
        present = {entry.name for entry in directory.iterdir()}
    except OSError:
        return []
    return sorted(present & LEGACY_NAMES)


def _occupied(path: Path) -> bool:
    """There, and not an empty directory — which `ensure_dirs` makes on any command."""
    if path.is_symlink() or path.is_file():
        return True
    if path.is_dir():
        return any(path.iterdir())
    return path.exists()


def _same_bytes(one: Path, other: Path) -> bool:
    try:
        return one.read_bytes() == other.read_bytes()
    except OSError:  # pragma: no cover - both were listed a moment ago
        return False


def _naming(legacy: Path, config_dir: Path, data_dir: Path, state_dir: Path) -> str:
    names = [name for name, path in (("KERYX_HOME", config_dir), ("DATA_DIR", data_dir),
                                     ("STATE_DIR", state_dir)) if path == legacy]
    return " and ".join(names)


def _tilde(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


# --- the service ---------------------------------------------------------------------------


@dataclass
class Service:
    """The installed service, stopped for the move and started again after it.

    `run` is `subprocess.run`, injectable: a test never reaches a real service manager.
    """

    target: Any  # restart.service.ServiceTarget
    run: Callable[..., Any] = subprocess.run
    sleep: Callable[[float], None] = time.sleep

    def describe(self) -> str:
        return str(self.target.describe())

    def active(self) -> bool:
        from keryx.restart.service import is_active

        return is_active(self.target, run=self.run)

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Stop it and wait until it has; a `MigrationError` if it will not."""
        self.run(self.target.stop_command(), capture_output=True, text=True, check=False)
        waited = 0.0
        while self.active():
            if waited >= timeout:
                raise MigrationError(
                    f"{self.describe()} did not stop within {timeout:g}s; nothing was moved"
                )
            self.sleep(STOP_POLL_S)
            waited += STOP_POLL_S

    def start(self) -> bool:
        """Start it unless it already runs (the installer may have started it). True if up."""
        if not self.active():
            self.run(self.target.start_command(), capture_output=True, text=True, check=False)
        return self.active()


# --- the run -------------------------------------------------------------------------------


@dataclass
class Report:
    """What `run` did, for `keryx migrate` to print."""

    moved: list[str] = field(default_factory=list)
    imported: list[str] = field(default_factory=list)
    rewritten: int = 0
    #: Claude tasks that ran in the old workspace, `(id, status, description)`, unfinished
    #: first: their sessions are gone, so a follow-up to one starts afresh.
    lost_sessions: list[tuple[int, str, str]] = field(default_factory=list)
    tightened: list[str] = field(default_factory=list)
    retired_to: Path | None = None
    leftovers: list[str] = field(default_factory=list)
    rerendered: list[str] = field(default_factory=list)
    started: bool | None = None
    notes: list[str] = field(default_factory=list)


def run(
    plan: Plan,
    *,
    service: Service | None,
    fix_permissions: Callable[[], list[str]],
    rerender: Callable[[], list[str]] = lambda: [],
    successor: Service | None = None,
    retire: Callable[[], list[str]] = lambda: [],
    today: date | None = None,
    echo: Callable[[str], None] = lambda line: None,
) -> Report:
    """Carry `plan` out. Raises `MigrationError` (with the service left stopped) on failure.

    `fix_permissions` is doctor's, over the settings as they are once everything has
    moved; `rerender` re-installs the service and the approval hook and names what it did.
    `service` is what is stopped first and started last — unless it is Jarvis's unit, in
    which case `retire` removes it once everything has moved, before `rerender` installs
    `successor`, Keryx's, which is the one started.
    """
    if plan.conflicts:
        raise MigrationError("the plan has conflicts; nothing was done")
    report = Report()
    today = today or date.today()
    was_active = service is not None and service.active()
    if service is not None and was_active:
        echo(f"stopping {service.describe()}")
        service.stop()
    for directory in (plan.config_dir, plan.data_dir, plan.state_dir):
        secure_dir(directory)
    for source in dict.fromkeys(step.source.parent for step in plan.steps):
        _checkpoint(source / TASK_DB)
    try:
        for step in plan.steps:
            _carry_out(step)
            report.moved.append(step.describe())
    except OSError as error:
        raise MigrationError(
            f"stopped at {error.filename or 'a move'}: {error.strerror or error}. What moved "
            "stays moved; run `keryx migrate` again to finish"
        ) from None
    report.rewritten, report.lost_sessions = rewrite_task_paths(
        plan.data_dir / TASK_DB, plan.old_data_dirs
    )
    for name in plan.old_imports:
        if rewrite_imports(plan.data_dir / TOOLS_DIR / name):
            report.notes.append(f"{TOOLS_DIR}/{name}: imports keryx now")
    store = ConfigStore(plan.config_dir)
    for env_file in plan.env_files:
        try:
            imported = store.import_env(env_file, today=today)
        except ConfigError as error:
            raise MigrationError(f"{env_file} was not imported: {error}") from None
        report.imported.append(f"{_tilde(env_file)} → {_tilde(imported.renamed_to)}"
                               if imported.renamed_to else str(env_file))
    if _names_legacy(store, plan.legacy):
        try:
            store.unset(["DATA_DIR"])
        except ConfigError as error:
            raise MigrationError(f"DATA_DIR could not be unset: {error}") from None
        report.notes.append(f"DATA_DIR no longer names {_tilde(plan.legacy)}")
    if plan.pin_tunnel and TUNNEL_KEY not in _stored_quietly(store):
        try:
            store.set({TUNNEL_KEY: LEGACY_APP_NAME})
        except ConfigError as error:
            raise MigrationError(f"{TUNNEL_KEY} could not be set: {error}") from None
        report.notes.append(f"{TUNNEL_KEY} = {LEGACY_APP_NAME}, the tunnel it has been running")
    report.tightened = _tighten(plan) + fix_permissions()
    if plan.retire:
        report.retired_to, report.leftovers = _retire(plan.legacy, today)
    report.leftovers += _working_leftovers(plan.working_dirs)
    report.rerendered = retire() + rerender()
    starting = successor or service
    if starting is not None and (was_active or starting.active()):
        report.started = starting.start()
    return report


def _names_legacy(store: ConfigStore, legacy: Path) -> bool:
    value = str(_stored_quietly(store).get("DATA_DIR", "")).strip()
    return bool(value) and Path(value).expanduser() == legacy


def _checkpoint(database: Path) -> None:
    """Fold the write-ahead log into `tasks.db`, so the file that moves is the whole of it."""
    if not database.is_file():
        return
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _carry_out(step: Step) -> None:
    if not step.source.exists() and not step.source.is_symlink():
        return  # `tasks.db-wal`, which the checkpoint's last close may have taken away
    if step.action == REMOVE or step.action == DUPLICATE:
        step.source.unlink(missing_ok=True)
        return
    assert step.destination is not None
    move(step.source, step.destination)


def move(source: Path, destination: Path) -> None:
    """`source` to `destination`: a rename, else a copy that is fsynced before the original
    goes, for a destination on another file system."""
    secure_dir(destination.parent)
    if destination.is_dir() and not destination.is_symlink() and not holds_files(destination):
        shutil.rmtree(destination)  # empty directories `ensure_dirs` made, before this ran
    try:
        os.rename(source, destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    partial = destination.with_name(f".{destination.name}.migrating")
    _remove(partial)  # an earlier copy that never finished: ours, and incomplete
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, partial, symlinks=True)
    else:
        shutil.copy2(source, partial, follow_symlinks=False)
    _fsync_tree(partial)
    os.replace(partial, destination)
    _remove(source)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _fsync_tree(path: Path) -> None:
    files = [path] if not path.is_dir() else [p for p in path.rglob("*") if p.is_file()]
    for file in files:
        if file.is_symlink():
            continue
        with file.open("rb") as handle:
            os.fsync(handle.fileno())


def rewrite_task_paths(
    database: Path, old_dirs: Iterable[Path]
) -> tuple[int, list[tuple[int, str, str]]]:
    """Point `report_path` and `cwd` at the new data directory, in one transaction.

    Compared with `substr`, never `LIKE`: a `_` or `%` in a home directory's name must not
    match anything but itself. Rows already rewritten no longer match, so a second run
    changes nothing. Claude tasks that ran in an old directory have their session id
    cleared in the same transaction — Claude keeps a session under the directory it ran
    in, which is gone, so a follow-up has to start afresh (the manager does, for a task
    with no session). Returns how many rows changed, and those tasks.
    """
    olds = [str(path) for path in old_dirs]
    if not olds or not database.is_file():
        return 0, []
    new = str(database.parent)
    changed = 0
    lost: list[tuple[int, str, str]] = []
    with contextlib.closing(sqlite3.connect(database)) as connection, connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        if not {"cwd", "report_path"} <= columns:
            return 0, []
        claude = "agent = 'claude'" if "agent" in columns else "1"
        for old in olds:
            under = "({col} = :old OR substr({col}, 1, length(:old) + 1) = :old || '/')"
            params = {"old": old, "new": new}
            rows = connection.execute(
                f"SELECT id, status, description FROM tasks WHERE {claude} "  # noqa: S608
                f"AND claude_session_id IS NOT NULL "
                f"AND (cwd IS NULL OR {under.format(col='cwd')}) ORDER BY id",
                params,
            ).fetchall()
            lost += [(row[0], str(row[1]), str(row[2])) for row in rows]
            connection.executemany(
                "UPDATE tasks SET claude_session_id = NULL WHERE id = ?",
                [(row[0],) for row in rows],
            )
            for column in ("cwd", "report_path"):
                cursor = connection.execute(
                    f"UPDATE tasks SET {column} = :new || substr({column}, length(:old) + 1) "  # noqa: S608
                    f"WHERE {under.format(col=column)}",
                    params,
                )
                changed += cursor.rowcount
    unfinished = {"queued", "running"}
    lost.sort(key=lambda row: (row[1] not in unfinished, row[0]))
    return changed, lost


def rewrite_imports(path: Path) -> bool:
    """`path`'s imports of the old package, made imports of `keryx`. True when it changed.

    Only the import lines: the rest of the file is the owner's, and a comment that still
    says Jarvis is history rather than a fault.
    """
    text = _text_quietly(path)
    fixed = OLD_IMPORT.sub(r"\1keryx", text)
    if fixed == text:
        return False
    write_private(path, fixed)
    return True


def old_target(target: Any) -> Any:
    """Jarvis's unit on `target`'s manager, when it is installed and `target` is the default.

    A unit named in `SERVICE_UNIT` is the owner's own, whatever it is called, and is left.
    """
    from keryx.restart.service import LAUNCHD_LABEL, SYSTEMD_UNIT, ServiceTarget, is_installed

    if target is None or target.unit not in (SYSTEMD_UNIT, LAUNCHD_LABEL):
        return None
    old = ServiceTarget(target.manager, OLD_UNITS[target.manager][0])
    return old if is_installed(old) else None


def retire_old_units(manager: str, *, run: Callable[..., Any] = subprocess.run) -> list[str]:
    """Jarvis's units disabled and their files removed, now that Keryx's replace them.

    Before Keryx's are installed, so two tunnels never run at once.
    """
    done = []
    if manager == "systemd":
        directory = xdg_home("config") / "systemd" / "user"
        commands = [
            ["systemctl", "--user", "disable", "--now", unit] for unit in OLD_UNITS[manager]
        ]
        files = [directory / unit for unit in OLD_UNITS[manager]]
    else:
        directory = Path.home() / "Library" / "LaunchAgents"
        commands = [
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"] for label in OLD_UNITS[manager]
        ]
        files = [directory / f"{label}.plist" for label in OLD_UNITS[manager]]
    for command, path in zip(commands, files, strict=True):
        run(command, capture_output=True, text=True, check=False)
        path.unlink(missing_ok=True)
        done.append(f"{path.name}: retired")
    if manager == "systemd":
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, check=False)
    return done


def _tighten(plan: Plan) -> list[str]:
    """Owner-only for what moved: each entry itself, and the log files, whose modes were
    the umask's. Not recursively — `workspace/` is an agent's working tree."""
    done = []
    paths = [step.destination for step in plan.steps if step.action == MOVE and step.destination]
    logs = plan.state_dir / "logs"
    if logs.is_dir():
        paths += [path for path in logs.iterdir() if path.is_file()]
    for path in paths:
        if path.is_symlink() or not path.exists():
            continue
        wanted = DATA_DIR_MODE if path.is_dir() else DATA_FILE_MODE
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            with contextlib.suppress(OSError):
                os.chmod(path, wanted)
                done.append(f"{_tilde(path)}: {mode:04o} → {wanted:04o}")
    return done


def _retire(legacy: Path, today: date) -> tuple[Path | None, list[str]]:
    """`~/.jarvis` renamed aside with whatever is left in it; removed only when empty."""
    if not legacy.is_dir():
        return None, []
    left = sorted(entry.name for entry in legacy.iterdir())
    if not left:
        legacy.rmdir()
        return None, []
    target = legacy.with_name(f"{legacy.name}.migrated-{today.isoformat()}")
    count = 1
    while target.exists():
        count += 1
        target = legacy.with_name(f"{legacy.name}.migrated-{today.isoformat()}-{count}")
    legacy.rename(target)
    return target, [f"{_tilde(target / name)}" for name in left]


def _working_leftovers(working_dirs: Iterable[Path]) -> list[str]:
    """What nothing reads any more in a working directory: the rest of `.secrets/`, and the
    renamed `.env`, which still holds every secret the store now holds."""
    found = []
    for directory in working_dirs:
        secrets = directory / ".secrets"
        if secrets.is_dir():
            found += [str(path) for path in sorted(secrets.iterdir())]
        found += [str(path) for path in sorted(directory.glob(f"{ENV_NAME}.imported-*"))]
    return found
