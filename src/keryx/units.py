"""`keryx service`: Keryx's four units — itself, its tunnel, the model server and the voice
server — as the service manager finds them, installing or removing them through the
checkout's own installer, and stopping or starting them without either (`control`), so the
Mac app needs no terminal for any of it.

A unit counts as installed when its manager has it loaded *or* its unit file is on disk:
`keryx service stop` boots a launchd job out and disables it, which leaves its plist where
it was and launchd knowing nothing of it, and that is a stopped unit, not a missing one.

The units are asked about through `restart.service`'s probes, which only ask; the installers
are `scripts/install-launchd.sh` and `install-systemd.sh`, found by `localmodels.voice`'s
`installer`, run as they would be at a terminal, their output captured so a failure can be
said in the script's own last line.
"""

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keryx.child_env import operational_env, redact_known
from keryx.config import Settings
from keryx.localmodels import voice as voice_setup
from keryx.localmodels.use import UNITS as SERVER_UNITS
from keryx.localmodels.voice import FAILED, OK, REFUSED, installer
from keryx.restart import service
from keryx.restart.service import LAUNCHD_LABEL, SYSTEMD_UNIT, ServiceTarget

#: How long an installer may run, and the exit code said for one stopped then: `voice`'s,
#: which gives its own installer runs the same deadline.
INSTALL_TIMEOUT_S = voice_setup.INSTALL_TIMEOUT_S
TIMED_OUT = voice_setup.TIMED_OUT
#: The units, in the order `keryx service` lists them.
KINDS = ("agent", "tunnel", "llm", "voice")
#: Each unit's name, by service manager: what `scripts/install-*.sh` installs (the agent's
#: is `SERVICE_UNIT` when that is set).
NAMES = {
    "launchd": {"agent": LAUNCHD_LABEL, "tunnel": "dev.keryx.tunnel",
                **SERVER_UNITS["launchd"]},
    "systemd": {"agent": SYSTEMD_UNIT, "tunnel": "cloudflared.service",
                **SERVER_UNITS["systemd"]},
}


def manager(settings: Settings, platform: str) -> str | None:
    """The service manager Keryx's units are under: `SERVICE_MANAGER`, or this platform's."""
    chosen = settings.service_manager
    if chosen != "auto":
        return None if chosen == "none" else chosen
    if platform == "darwin":
        return "launchd"
    return "systemd" if platform.startswith("linux") else None


def unit_name(settings: Settings, manager: str, kind: str) -> str:
    """The `kind` unit's name under `manager`: the installers' own, or `SERVICE_UNIT`'s."""
    if kind == "agent" and settings.service_unit:
        return settings.service_unit
    return NAMES[manager][kind]


def unit_on_disk(target: ServiceTarget) -> bool:
    """Whether `target`'s unit file is where its installer writes it."""
    return service.unit_file(target).is_file()


def status(
    settings: Settings,
    *,
    platform: str | None = None,
    installed: Callable[[ServiceTarget], bool] | None = None,
    active: Callable[[ServiceTarget], bool] | None = None,
) -> dict[str, Any]:
    """`keryx service --json`: each unit installed (loaded, or its file on disk), and
    running. Only asks."""
    found = manager(settings, platform or voice_setup.PLATFORM)
    # Late-bound through the module, as `keryx.status` binds them.
    is_installed = installed or service.is_installed
    is_active = active or service.is_active
    document: dict[str, dict[str, bool]] = {}
    for kind in KINDS:
        if found is None:
            document[kind] = {"installed": False, "active": False}
            continue
        target = ServiceTarget(found, unit_name(settings, found, kind))
        here = is_installed(target) or unit_on_disk(target)
        document[kind] = {"installed": here, "active": here and is_active(target)}
    return {"manager": found, "units": document}


# --- installing and removing them --------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """How an install or removal ended: its exit code, and what to say about it."""

    code: int
    message: str


def run_captured(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run an installer, nothing read from its stdin; its exit code, stdout and stderr. One
    that has not finished within `INSTALL_TIMEOUT_S` is stopped, and says so on stderr. None
    of Keryx's secrets is in its environment (`child_env.operational_env`)."""
    try:
        done = subprocess.run(list(argv), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, check=False, timeout=INSTALL_TIMEOUT_S,
                              env=operational_env())
    except subprocess.TimeoutExpired:
        name = Path(argv[0]).name
        return TIMED_OUT, "", f"{name} did not finish within {INSTALL_TIMEOUT_S:g} s"
    except OSError as error:
        return 127, "", str(error)
    return done.returncode, done.stdout, done.stderr


def _last_line(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else None


def install(
    *,
    settings: Settings | None = None,
    llm: bool = False,
    voice: bool = False,
    uninstall: bool = False,
    root: Path,
    platform: str | None = None,
    run: Callable[[Sequence[str]], tuple[int, str, str]] = run_captured,
) -> Outcome:
    """Install (or, with `uninstall`, remove) Keryx and its tunnel, or with `llm`/`voice`
    the model servers' units, through the checkout at `root`. 2 for no installer here —
    no checkout, no service manager — 1 with the script's last error line, 0 when done."""
    flags = [flag for flag, on in (("--llm", llm), ("--voice", voice)) if on]
    script, problem = installer(" ".join(flags), root=root, platform=platform)
    if script is None:
        return Outcome(REFUSED, problem or "no installer")
    argv = [str(script), *flags, *(["--uninstall"] if uninstall else [])]
    code, out, err = run(argv)
    if code != 0:
        return Outcome(FAILED, _last_line(err) or f"{script.name} {' '.join(argv[1:])} exited "
                                                  f"with {code}")
    kinds = [flag.removeprefix("--") for flag in flags] or ["agent", "tunnel"]
    found = "launchd" if script.name == "install-launchd.sh" else "systemd"
    settings = settings or Settings()
    names = ", ".join(unit_name(settings, found, kind) for kind in kinds)
    return Outcome(OK, f"{'removed' if uninstall else 'installed'} {names}")


# --- stopping and starting them ----------------------------------------------------------

#: What `keryx service stop|start` acts on without `--all`: Keryx, and its tunnel, which
#: serves nothing else and without which nobody can reach Keryx. `--all` adds every other
#: unit in `NAMES` — the model servers, and whatever that table gains later.
MAIN_KINDS = ("agent", "tunnel")
#: A unit's state once a stop or start has been through it.
RUNNING, STOPPED, NOT_INSTALLED, UNIT_FAILED = "running", "stopped", "not-installed", "failed"

STOPPED_MAIN = "Keryx is stopped, and won't start at login until it's started again."
STOPPED_ALL = ("Keryx and its model servers are stopped, and won't start at login until "
               "they're started again.")
RUNNING_MAIN = "Keryx is running."
RUNNING_ALL = "Keryx and its model servers are running."
SERVERS_STOPPED = ("Keryx isn't installed as a background process; its model servers are "
                   "stopped, and won't start at login until they're started again.")
NOTHING_TO_STOP = "Keryx isn't installed as a background process, so there's nothing to stop."
NOT_INSTALLED_YET = "Keryx isn't installed as a background process yet."
COULD_NOT = {"stop": "Not everything could be stopped.",
             "start": "Not everything could be started."}
NO_MANAGER = "There's no service manager on this machine to run Keryx with."

#: `(argv)` → `(exit code, stdout, stderr)`: `run_captured`'s shape.
Runner = Callable[[Sequence[str]], tuple[int, str, str]]


@dataclass(frozen=True)
class Control:
    """How a `keryx service stop|start` ended: its exit code, and what it says."""

    ok: bool
    code: int
    message: str
    detail: str | None
    units: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "detail": self.detail,
                "units": self.units}


def _steps(target: ServiceTarget, action: str, *, loaded: bool) -> list[list[str]]:
    """The commands for one unit. A stop disables first, so a stop cut short (the app's
    quit deadline, a shutdown) still leaves nothing to start at login; a start enables
    first, because launchd refuses to bootstrap a disabled job."""
    if action == "stop":
        if target.manager == "systemd":
            return [target.disable_command(), target.stop_command()]
        return [target.disable_command(), *([target.stop_command()] if loaded else [])]
    if target.manager == "systemd":
        return [target.enable_command(), target.start_command()]
    return [target.enable_command(),
            target.kickstart_command() if loaded else target.start_command()]


def control(
    settings: Settings,
    action: str,
    *,
    every: bool = False,
    settle: Callable[[], object] | None = None,
    platform: str | None = None,
    run: Runner | None = None,
    loaded: Callable[[ServiceTarget], bool] | None = None,
    on_disk: Callable[[ServiceTarget], bool] | None = None,
    active: Callable[[ServiceTarget], bool] | None = None,
) -> Control:
    """`keryx service stop|start` (`action`): Keryx and its tunnel, or with `every` every
    unit there is, stopped and kept from starting at login, or let start and started —
    without removing any unit file, so no installer runs and macOS asks nothing.

    A unit not installed is left alone and said so; starting Keryx itself when it is not
    is refused (2) before anything runs. `settle` is called once before a stop touches
    anything — the restart in flight, told it was the owner. Each unit's verdict is what
    its manager then holds (launchd: loaded or not), not only what a command exited with,
    since launchd can refuse a bootout of a job that is going anyway. 1 when any unit
    failed, every failure in `detail`, redacted.
    """
    found = manager(settings, platform or voice_setup.PLATFORM)
    if found is None:
        return Control(False, REFUSED, NO_MANAGER, None, [])
    run = run or run_captured
    is_loaded = loaded or service.is_installed
    has_file = on_disk or unit_on_disk
    is_running = active or service.is_active
    kinds = [kind for kind in NAMES[found] if every or kind in MAIN_KINDS]
    targets = {kind: ServiceTarget(found, unit_name(settings, found, kind)) for kind in kinds}
    present = {kind: is_loaded(target) or has_file(target) for kind, target in targets.items()}
    rows = {kind: {"kind": kind, "unit": target.unit, "state": NOT_INSTALLED}
            for kind, target in targets.items()}
    if not present["agent"]:
        if action == "start":
            for kind in kinds:  # untouched: each as it is
                if present[kind]:
                    rows[kind]["state"] = RUNNING if is_running(targets[kind]) else STOPPED
            return Control(False, REFUSED, NOT_INSTALLED_YET, None, list(rows.values()))
        if not any(present.values()):
            return Control(True, OK, NOTHING_TO_STOP, None, list(rows.values()))
    if action == "stop" and settle is not None:
        settle()
    order = kinds if action == "stop" else kinds[::-1]  # the servers up before Keryx
    problems: list[str] = []
    for kind in order:
        if not present[kind]:
            continue
        target = targets[kind]
        failure = None
        for argv in _steps(target, action, loaded=is_loaded(target)):
            code, out, err = run(argv)
            if code != 0 and failure is None:
                said = _last_line(err) or _last_line(out) or f"exited with {code}"
                failure = f"{' '.join(argv)}: {said}"
        if failure is not None and target.manager == "launchd" \
                and is_loaded(target) == (action == "start"):
            failure = None  # what launchd now holds is what was asked for
        if failure is not None:
            problems.append(failure)
        rows[kind]["state"] = UNIT_FAILED if failure else STOPPED if action == "stop" else RUNNING
    acted = [kind for kind in kinds if present[kind]]
    if problems:
        detail = redact_known("; ".join(problems), settings)
        return Control(False, FAILED, COULD_NOT[action], detail, list(rows.values()))
    servers = any(kind not in MAIN_KINDS for kind in acted)
    if not present["agent"]:
        message = SERVERS_STOPPED if servers else NOTHING_TO_STOP
    elif action == "stop":
        message = STOPPED_ALL if servers else STOPPED_MAIN
    else:
        message = RUNNING_ALL if servers else RUNNING_MAIN
    return Control(True, OK, message, None, list(rows.values()))
