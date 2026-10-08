"""Talking to the service manager: what to restart, and what to leave watching.

Split out of `restart.py` (2026-09-02). Both halves are the same conversation — one hands
the service manager a restart, the other hands it a watchdog — and both are the reason
`ServiceTarget` and `watch_command` live together rather than a module apart.

Nothing supervising this process means `resolve_target` returns `None`, and a restart is
then refused rather than attempted: stopping would leave nothing to start it again.

"Supervising" is a fact about this process, not about the machine. `systemctl` is on PATH
on every Linux desktop, and that alone once resolved a `keryx serve` started in a terminal
to `systemctl --user restart keryx.service`: a unit that did not exist (the failure went
nowhere anyone would hear it), or worse, the installed copy — restarted in place of the one
that was asked. So `auto` asks whether this process runs *as* the unit: its cgroup under
systemd, the job label launchd hands it on macOS.
"""

import logging
import os
import plistlib
import pwd
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from keryx.config import Settings
from keryx.config.files import APP_NAME, HOME_ENV, XDG_HOMES, keryx_home, xdg_home

log = logging.getLogger("keryx.restart")

#: Default unit/label names, matching `ops/systemd/keryx.service` and
#: `ops/launchd/dev.keryx.agent.plist`.
SYSTEMD_UNIT = "keryx.service"
LAUNCHD_LABEL = "dev.keryx.agent"
#: What puts each one there, for the messages that say it is missing.
INSTALLERS = {"systemd": "scripts/install-systemd.sh", "launchd": "scripts/install-launchd.sh"}

#: Where Linux says which cgroup this process is in. Under systemd the cgroup is the unit.
PROC_SELF_CGROUP = Path("/proc/self/cgroup")
#: The user manager's own cgroup; `systemctl --user` reaches only the units beneath it.
USER_MANAGER_CGROUP = re.compile(r"user@\d+\.service")
#: The environment variable launchd sets to the label of the job it started.
LAUNCHD_JOB_VAR = "XPC_SERVICE_NAME"
#: How long a question to the service manager may take before the answer is "no".
PROBE_TIMEOUT_S = 5.0

#: The transient unit the watchdog runs as, suffixed with the pid that armed it so a
#: second restart during a crash loop does not collide with the watch still running.
WATCH_UNIT_PREFIX = "keryx-restart-watch"
#: Where the watchdog's own output goes, under `state_dir/logs`.
WATCH_LOG_NAME = "restart-watch.log"

#: What `keryx restart` prints when there is no service manager to ask.
UNSUPPORTED_HINT = """no installed Keryx service to restart on this machine.

Install the service first (it is what starts Keryx again after it stops):

  scripts/install-systemd.sh    # Linux
  scripts/install-launchd.sh    # macOS

Or `keryx config set SERVICE_MANAGER … SERVICE_UNIT …` if the unit is named something else. To
restart by hand instead, stop the process and start `keryx serve` again — nothing else
will do it for you."""


# --- the service manager ----------------------------------------------------


@dataclass(frozen=True)
class ServiceTarget:
    """The unit this process runs as, and how to ask its manager to restart it."""

    manager: str  # "systemd" | "launchd"
    unit: str

    def command(self) -> list[str]:
        """The restart command, as a list (never a shell string)."""
        if self.manager == "systemd":
            return ["systemctl", "--user", "restart", self.unit]
        # `kickstart -k` kills the job and starts it again; `launchctl restart` is gone.
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{self.unit}"]

    def describe(self) -> str:
        return f"{self.manager} {self.unit}"

    def stop_command(self) -> list[str]:
        """Stop it and keep it stopped: `bootout` on launchd, where `KeepAlive` would
        start a merely killed job again."""
        if self.manager == "systemd":
            return ["systemctl", "--user", "stop", self.unit]
        return ["launchctl", "bootout", f"gui/{os.getuid()}/{self.unit}"]

    def start_command(self) -> list[str]:
        """Start it again after `stop_command`."""
        if self.manager == "systemd":
            return ["systemctl", "--user", "start", self.unit]
        return ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(unit_file(self))]

    def kickstart_command(self) -> list[str]:
        """Start it if it is loaded and not running; nothing if it runs (no `-k`). systemd
        has no loaded-but-stopped of its own, so there it is `start_command`."""
        if self.manager == "systemd":
            return self.start_command()
        return ["launchctl", "kickstart", f"gui/{os.getuid()}/{self.unit}"]

    def disable_command(self) -> list[str]:
        """Keep it from starting at login — and, on launchd, from being bootstrapped at all
        — until `enable_command`: what `keryx service stop` leaves behind it."""
        if self.manager == "systemd":
            return ["systemctl", "--user", "disable", self.unit]
        return ["launchctl", "disable", f"gui/{os.getuid()}/{self.unit}"]

    def enable_command(self) -> list[str]:
        """Undo `disable_command`. A disabled launchd job refuses `bootstrap`, so this comes
        first, before any start."""
        if self.manager == "systemd":
            return ["systemctl", "--user", "enable", self.unit]
        return ["launchctl", "enable", f"gui/{os.getuid()}/{self.unit}"]


def unit_file(target: ServiceTarget) -> Path:
    """Where the installers write `target`'s unit: its plist in `~/Library/LaunchAgents`, or
    its unit file under the XDG configuration directory's `systemd/user`."""
    if target.manager == "systemd":
        return xdg_home("config") / "systemd" / "user" / target.unit
    return Path.home() / "Library" / "LaunchAgents" / f"{target.unit}.plist"


# --- whose units they are ----------------------------------------------------------------

#: What every command that drives Keryx's units says when they were rendered for another
#: configuration directory: a sentence for a person, with no path in it (the Mac app leads
#: with it), and the paths in `FOREIGN_UNITS_DETAIL` — only `KERYX_HOME`, never another
#: variable — which a terminal prints beneath it.
FOREIGN_UNITS = ("Keryx's background processes here belong to another Keryx settings folder, "
                 "so this command leaves them alone.")
FOREIGN_UNITS_DETAIL = ("The units were rendered for KERYX_HOME={theirs} (in {unit_file}); "
                        "this command runs with KERYX_HOME={ours}.")
#: The detail when whose they are cannot be told: a unit file that cannot be read or parsed,
#: or a service manager that cannot be asked. Refused all the same — the guard fails closed.
UNREADABLE_DETAIL = "could not read {unit_file}"
#: What `launchctl print` exits with for a job it does not have loaded.
LAUNCHD_NOT_LOADED = 113


class UnreadableUnit(Exception):
    """A unit file, or a service manager's answer, that does not say whose the unit is."""


@dataclass(frozen=True)
class Owner:
    """Whose one unit is: the `KERYX_HOME` it runs with — None when that cannot be told —
    and where that was read (a unit file, or the service manager's own answer)."""

    home: Path | None
    where: str


@dataclass(frozen=True)
class ForeignUnits:
    """A unit rendered for a `KERYX_HOME` (`theirs`) that is not this process's (`ours`), or
    one whose `KERYX_HOME` cannot be told (`theirs` None): the refusal every command that
    drives the service manager makes. `unit_file` is where it was read."""

    theirs: Path | None
    ours: Path
    unit_file: str

    @property
    def message(self) -> str:
        return FOREIGN_UNITS

    @property
    def detail(self) -> str:
        if self.theirs is None:
            return UNREADABLE_DETAIL.format(unit_file=self.unit_file)
        return FOREIGN_UNITS_DETAIL.format(theirs=self.theirs, ours=self.ours,
                                           unit_file=self.unit_file)


def account_home() -> Path:
    """The account's own home directory, from the user database rather than `$HOME`: where
    the user's service manager finds its units, whatever a shell has moved `HOME` to."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:  # an account the user database does not list: `$HOME` is all there is
        return Path.home()


def unit_files(target: ServiceTarget) -> list[Path]:
    """The files `target`'s name could be loaded from when its manager has none loaded:
    where this process's installer writes it (`unit_file`, which follows `HOME` and
    `XDG_CONFIG_HOME`), and the account's default, which the manager reads whatever this
    process's environment says. A label (or a user unit's name) is one per account, so a
    sandboxed `HOME` or `XDG_CONFIG_HOME` moves the file this process would write, never the
    unit `launchctl` or `systemctl` reaches; what the manager has loaded is asked of it
    (`manager_owners`)."""
    if target.manager == "systemd":
        default = account_home() / ".config" / "systemd" / "user" / target.unit
    else:
        default = account_home() / "Library" / "LaunchAgents" / f"{target.unit}.plist"
    return list(dict.fromkeys([unit_file(target), default]))


def rendered_env(path: Path, manager: str) -> dict[str, str] | None:
    """The environment a unit file was rendered with: a plist's `EnvironmentVariables`, a
    systemd unit's `Environment=` lines (systemd's quoting and `%%` undone). None when there
    is no file; empty for one that renders none (a template from before they were rendered).
    `UnreadableUnit` for one that cannot be read or parsed, or names `KERYX_HOME` with no
    value to be read: whose that is cannot be told."""
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError as error:
        raise UnreadableUnit(str(path)) from error
    if manager != "systemd":
        try:
            document = plistlib.loads(data)
        except Exception as error:  # not a plist launchd could load either
            raise UnreadableUnit(str(path)) from error
        variables = document.get("EnvironmentVariables", {}) if isinstance(document, dict) \
            else None
        if not isinstance(variables, dict) or not isinstance(
                variables.get(HOME_ENV, ""), str):
            raise UnreadableUnit(str(path))
        return {key: value for key, value in variables.items() if isinstance(value, str)}
    env: dict[str, str] = {}
    mentioned = False
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.lstrip().startswith(("#", ";")):
            continue
        mentioned = mentioned or HOME_ENV in line
        setting, _, assignments = line.partition("=")
        if setting.strip() != "Environment":
            continue
        try:
            env.update(_assignments(assignments))
        except ValueError:
            continue
    if mentioned and HOME_ENV not in env:
        raise UnreadableUnit(str(path))
    return env


def _assignments(text: str) -> dict[str, str]:
    """`NAME=value` words, as systemd quotes them (`%%` undone); ValueError when the quoting
    does not close."""
    env = {}
    for word in shlex.split(text):
        name, equals, value = word.partition("=")
        if equals:
            env[name] = value.replace("%%", "%")
    return env


def rendered_home(env: Mapping[str, str]) -> Path:
    """The `KERYX_HOME` a unit rendered with `env` runs with: the one it names, a leading
    `~` read against the account's home (a value that is still not absolute is never this
    process's); else the one its `XDG_CONFIG_HOME` gives; else the default, under the
    account's home. A unit with none of them — a template from before they were rendered —
    belongs to the default. `UnreadableUnit` for a `KERYX_HOME` that is there and blank."""
    if HOME_ENV in env:
        home = env[HOME_ENV].strip()
        if not home:
            raise UnreadableUnit(HOME_ENV)
        if home == "~" or home.startswith("~/"):
            return account_home() / home[2:]
        return Path(home)
    config = env.get(XDG_HOMES["config"][0], "").strip()
    if config and Path(config).is_absolute():
        return Path(config) / APP_NAME
    return account_home() / ".config" / APP_NAME


def manager_owners(
    target: ServiceTarget, *, run: Callable[..., object] | None = None
) -> list[Owner]:
    """Whose `target` is, as its service manager has it loaded — read-only, and wherever
    its file is: `systemctl --user show` (its `Environment`, drop-ins included) or
    `launchctl print` (its `environment`, else the file at its `path`). Empty when the
    manager has nothing loaded by that name, or there is no such manager here; an `Owner`
    with no home when it cannot be asked or does not say — a loaded job whose file is
    gone, a probe that timed out."""
    run = run or subprocess.run  # late-bound, so a test's replacement is the one used
    if target.manager == "systemd":
        command = ["systemctl", "--user", "show", "-p", "LoadState", "-p", "FragmentPath",
                   "-p", "Environment", target.unit]
    else:
        command = ["launchctl", "print", f"gui/{os.getuid()}/{target.unit}"]
    asked = f"{target.manager}'s answer about {target.unit}"
    try:
        result = run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except FileNotFoundError:
        return []  # no such manager on this machine: nothing it could have loaded
    except (OSError, subprocess.SubprocessError):
        return [Owner(None, asked)]
    said = result.stdout or ""
    if target.manager == "systemd":
        if result.returncode != 0:
            return [Owner(None, asked)]
        fields = dict(line.partition("=")[::2] for line in said.splitlines())
        if fields.get("LoadState", "").strip() != "loaded":
            return []
        where = fields.get("FragmentPath", "").strip() or asked
        try:
            return [Owner(rendered_home(_assignments(fields.get("Environment", ""))), where)]
        except (ValueError, UnreadableUnit):
            return [Owner(None, where)]
    if result.returncode == LAUNCHD_NOT_LOADED:
        return []
    if result.returncode != 0:
        return [Owner(None, asked)]
    path, env = _launchd_job(said)
    try:
        if HOME_ENV in env:
            return [Owner(rendered_home(env), path or asked)]
        if path is None:
            return [Owner(None, asked)]
        rendered = rendered_env(Path(path), "launchd")
        if rendered is None:  # loaded from a file that is gone: whose it is, unsaid
            return [Owner(None, path)]
        return [Owner(rendered_home(rendered), path)]
    except UnreadableUnit:
        return [Owner(None, path or asked)]


def _launchd_job(said: str) -> tuple[str | None, dict[str, str]]:
    """From `launchctl print`: the job's `path`, and its own `environment` block (not the
    `inherited` or `default` one)."""
    path, env, inside = None, {}, False
    for line in said.splitlines():
        stripped = line.strip()
        if inside:
            if stripped == "}":
                inside = False
            elif " => " in stripped:
                name, _, value = stripped.partition(" => ")
                env[name] = value
        elif stripped == "environment = {":
            inside = True
        elif path is None and stripped.startswith("path = "):
            path = stripped.removeprefix("path = ")
    return path, env


def unit_owners(
    target: ServiceTarget, *, run: Callable[..., object] | None = None
) -> list[Owner]:
    """Whose `target` is, everywhere that can be told: as its manager has it loaded
    (`manager_owners`), and each of its unit files on disk (`unit_files`). Empty when it is
    neither loaded nor on disk."""
    owners = list(manager_owners(target, run=run))
    for path in unit_files(target):
        try:
            env = rendered_env(path, target.manager)
            if env is not None:
                owners.append(Owner(rendered_home(env), str(path)))
        except UnreadableUnit:
            owners.append(Owner(None, str(path)))
    return owners


def _same_directory(one: Path, other: Path) -> bool:
    return os.path.realpath(one) == os.path.realpath(other)


def foreign_units(
    targets: Sequence[ServiceTarget], *, home: Path | None = None,
    run: Callable[..., object] | None = None,
) -> ForeignUnits | None:
    """The first of `targets` that runs, or was rendered, with another `KERYX_HOME` than
    `home` (this process's own, `keryx_home`), compared as real paths — or whose
    `KERYX_HOME` cannot be told, which is refused the same; None when every one is `home`'s,
    or not installed. The one guard every command that drives the service manager asks
    before running anything: unit names are global to the account, so a command run with
    another `KERYX_HOME` — a sandbox, a test — would otherwise stop, start or replace the
    owner's own Keryx. It only reads, and never raises for a file or a manager it cannot
    read: that is a refusal."""
    ours = home or keryx_home()
    for target in targets:
        for owner in unit_owners(target, run=run):
            theirs = owner.home
            if theirs is None or not theirs.is_absolute() or not _same_directory(theirs, ours):
                return ForeignUnits(theirs, ours, owner.where)
    return None


def resolve_target(
    settings: Settings,
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] | None = None,
    from_outside: bool = False,
    supervising: Callable[[ServiceTarget], bool] | None = None,
    installed: Callable[[ServiceTarget], bool] | None = None,
) -> ServiceTarget | None:
    """The service manager to restart through, or None when nothing supervises us.

    `SERVICE_MANAGER=auto` (the default) picks systemd on Linux and launchd on macOS, and
    only when this process actually runs as the unit (`runs_under`). A Keryx started by
    hand from a terminal has nothing that would bring it back, and must refuse to stop
    rather than take itself off the air — or restart an installed copy it is not.

    `from_outside` is for the commands a person runs in a terminal (`keryx restart`,
    `keryx doctor`), which are never inside the unit: for them an installed unit
    (`is_installed`) is the answer. `systemd`/`launchd` are taken at their word, and `none`
    refuses outright.
    """
    target = candidate_target(settings, platform=platform, which=which)
    if target is None or settings.service_manager != "auto":
        return target
    # Late-bound, like `which` below, so a test can reach the real probes' replacements.
    if (supervising or runs_under)(target):
        return target
    if from_outside and (installed or is_installed)(target):
        return target
    return None


def candidate_target(
    settings: Settings,
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] | None = None,
) -> ServiceTarget | None:
    """The unit Keryx would run as on this machine, whether or not it does.

    The manager `SERVICE_MANAGER` names (this platform's, for `auto`) and the unit
    `SERVICE_UNIT` names (the installers' default otherwise); None when there is no such
    manager, or its command is not on PATH.
    """
    # Resolved here rather than as a default argument: a default is bound once, at import,
    # so `monkeypatch.setattr(shutil, "which", ...)` could never reach it — which is how a
    # suite that assumes systemd ends up failing two dozen tests on a Mac.
    which = which or shutil.which
    manager = settings.service_manager
    if manager == "auto":
        if platform.startswith("linux"):
            manager = "systemd"
        elif platform == "darwin":
            manager = "launchd"
        else:
            manager = "none"
    if manager == "none":
        return None
    if not which("systemctl" if manager == "systemd" else "launchctl"):
        if settings.service_manager != "auto":
            log.warning("SERVICE_MANAGER=%s but its command is not on PATH", manager)
        return None
    default = SYSTEMD_UNIT if manager == "systemd" else LAUNCHD_LABEL
    return ServiceTarget(manager, settings.service_unit or default)


def runs_under(
    target: ServiceTarget,
    *,
    cgroup: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True when this process is the service `target` names (or a child of it).

    systemd: the unit is a directory in this process's cgroup path, beneath the user
    manager — a terminal's shell is in a `.scope`, and a system unit of the same name is
    out of `systemctl --user`'s reach. launchd: the job label it sets in the environment.
    """
    if target.manager == "systemd":
        text = read_cgroup() if cgroup is None else cgroup
        return any(target.unit in _user_units(line) for line in text.splitlines())
    env = os.environ if environ is None else environ
    return env.get(LAUNCHD_JOB_VAR) == target.unit


def _user_units(line: str) -> list[str]:
    """The cgroup path components beneath the user manager, from one `/proc/*/cgroup` line."""
    parts = line.split(":", 2)[-1].strip().split("/")
    for index, part in enumerate(parts):
        if USER_MANAGER_CGROUP.fullmatch(part):
            return parts[index + 1 :]
    return []


def read_cgroup(path: Path = PROC_SELF_CGROUP) -> str:
    """This process's cgroup membership; empty where there is none to read (macOS)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def is_installed(
    target: ServiceTarget, *, run: Callable[..., object] = subprocess.run
) -> bool:
    """Whether the service manager knows the unit at all. Asks; never changes anything.

    A probe that cannot answer — no user bus over ssh, a hung manager — is a "no": this
    decides whether a restart is attempted, and one that is not attempted is announced.
    """
    if target.manager == "systemd":
        command = ["systemctl", "--user", "show", "--property=LoadState", "--value", target.unit]
    else:
        command = ["launchctl", "print", f"gui/{os.getuid()}/{target.unit}"]
    try:
        result = run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return target.manager != "systemd" or result.stdout.strip() == "loaded"


def is_stopped(
    target: ServiceTarget, *, run: Callable[..., object] = subprocess.run
) -> bool:
    """Installed, but stopped and kept from starting — what `keryx service stop` leaves:
    its unit file on disk while launchd has it unloaded, or systemd has it disabled and
    not running. Asks; changes nothing. A probe that cannot answer is "not stopped", so a
    restart goes on being attempted as before."""
    if not unit_file(target).is_file():
        return False
    if target.manager != "systemd":
        return not is_installed(target, run=run)
    return _enablement(target, run) is False and not is_active(target, run=run)


def is_present(target: ServiceTarget) -> bool:
    """Installed, as `doctor`, `keryx status` and the setup plan mean it: the manager has
    it loaded, or its unit file is where its installer put it — a job `keryx service stop`
    booted out is still installed, only stopped. `is_installed` keeps meaning loaded, for
    `keryx restart` and `keryx migrate`, which act on the job itself."""
    return is_installed(target) or unit_file(target).is_file()


def is_enabled(
    target: ServiceTarget, *, run: Callable[..., object] = subprocess.run
) -> bool | None:
    """Whether the unit may start at login (and, on launchd, be bootstrapped): launchd's
    `print-disabled` list, systemd's `is-enabled`. None when it cannot be told. Asks;
    changes nothing."""
    return _enablement(target, run)


#: One line of `launchctl print-disabled`: `"dev.keryx.agent" => disabled` (older macOS
#: says `=> true` for disabled, `=> false` for enabled).
_DISABLED_LINE = re.compile(r'^\s*"(?P<label>[^"]+)"\s*=>\s*(?P<state>\w+)', re.M)


def _enablement(target: ServiceTarget, run: Callable[..., object]) -> bool | None:
    if target.manager == "systemd":
        command = ["systemctl", "--user", "is-enabled", target.unit]
    else:
        command = ["launchctl", "print-disabled", f"gui/{os.getuid()}"]
    try:
        result = run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    said = result.stdout or ""
    if target.manager == "systemd":
        state = said.strip()
        return True if state.startswith("enabled") else False if state == "disabled" else None
    if result.returncode != 0:
        return None
    for line in _DISABLED_LINE.finditer(said):
        if line.group("label") == target.unit:
            return line.group("state") not in ("disabled", "true")
    return True  # not listed: launchd's default, and the installers set no `Disabled`


def is_active(
    target: ServiceTarget, *, run: Callable[..., object] = subprocess.run
) -> bool:
    """Whether the service is running, or on its way up or down. Asks; changes nothing.

    Anything short of a clear "stopped" counts as running — `activating`, `deactivating`,
    a probe that timed out — because the one caller (`keryx migrate`) must not move a
    database out from under a process that still has it open.
    """
    if target.manager == "systemd":
        command = ["systemctl", "--user", "is-active", target.unit]
    else:
        command = ["launchctl", "print", f"gui/{os.getuid()}/{target.unit}"]
    try:
        result = run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return True
    if target.manager == "systemd":
        return result.stdout.strip() not in ("inactive", "failed", "unknown")
    # launchd: not loaded at all is stopped; loaded is running unless it says otherwise.
    return result.returncode == 0 and "state = not running" not in result.stdout


# --- the watchdog that outlives the restart ---------------------------------


@dataclass(frozen=True)
class WatchPlan:
    """How to start the watchdog: the command, what to call it, and who redirects it."""

    argv: list[str]
    label: str
    #: True when we have to point its output at a file ourselves; systemd does it for us.
    redirect: bool


def location_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The variables that say where Keryx keeps things, as this process has them.

    A transient unit starts with the *user manager's* environment, not ours, so without
    these the watchdog could resolve a different `KERYX_HOME` or XDG directory from the
    service it watches — read another restart record, or none, and ring nobody. The
    directory settings go too, for a service that was handed one in its environment.
    """
    env = os.environ if environ is None else environ
    names = (
        HOME_ENV,
        *(variable for variable, _ in XDG_HOMES.values()),
        "DATA_DIR",
        "STATE_DIR",
        "CACHE_DIR",
    )
    return {name: env[name] for name in names if env.get(name, "").strip()}


def watch_command(
    settings: Settings,
    target: ServiceTarget,
    *,
    which: Callable[[str], str | None] | None = None,
    pid: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> WatchPlan | None:
    """How to start the watchdog *outside* this service, or None when nothing can be.

    Outside is the whole point. A restart signals the service's entire cgroup, and a
    process we merely fork is in it — it would be killed by the very restart it exists to
    watch. `systemd-run --user` hands the job to the service manager instead, which starts
    it in a transient unit of its own; launchd has no cgroup of its own to escape, so a
    new session is enough there.

    None means the watch cannot be armed at all (systemd with no `systemd-run`). Saying so
    on the record is better than starting something that will quietly die with us.
    """
    which = which or shutil.which  # late-bound, for the reason in `resolve_target`
    inner = [sys.executable, "-m", "keryx", "restart-watch"]
    if target.manager != "systemd":
        return WatchPlan(inner, "detached", redirect=True)
    runner = which("systemd-run")
    if runner is None:
        return None
    unit = f"{WATCH_UNIT_PREFIX}-{pid or os.getpid()}"
    log_path = watch_log_path(settings)
    return WatchPlan(
        [
            runner,
            "--user",
            "--quiet",
            "--collect",  # take the unit away once it exits, so the next one is free to run
            f"--unit={unit}",
            f"--property=WorkingDirectory={Path.cwd()}",
            f"--property=StandardOutput=append:{log_path}",
            f"--property=StandardError=append:{log_path}",
            *(f"--setenv={name}={value}" for name, value in location_env(environ).items()),
            "--",
            *inner,
        ],
        unit,
        redirect=False,
    )


def watch_log_path(settings: Settings) -> Path:
    """Where the watchdog writes; it has no other way to be heard if it fails itself."""
    return settings.state_dir / "logs" / WATCH_LOG_NAME


def spawn_watchdog(plan: WatchPlan, settings: Settings) -> int:
    """Start the watchdog and return its pid, without ever waiting for it.

    Blocking, and deliberately `Popen`: we are about to be killed, so there must be no
    child watcher attached to an event loop that is going away, and nothing to reap.
    """
    output = subprocess.DEVNULL
    if plan.redirect:
        path = watch_log_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        output = path.open("a", encoding="utf-8")  # noqa: SIM115 - the child owns it now
    try:
        process = subprocess.Popen(  # noqa: S603 - a fixed argv, never a shell string
            plan.argv,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT if plan.redirect else subprocess.DEVNULL,
            start_new_session=True,
            cwd=Path.cwd(),
        )
    finally:
        if plan.redirect:
            output.close()  # the child holds its own duplicate
    return process.pid
