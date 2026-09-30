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
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from keryx.config import Settings
from keryx.config.files import HOME_ENV, XDG_HOMES

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
        plist = Path.home() / "Library" / "LaunchAgents" / f"{self.unit}.plist"
        return ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]


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
