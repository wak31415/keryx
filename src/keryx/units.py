"""`keryx service`: Keryx's five units — itself, its tunnel, the model server, the voice
server and the voice LLM's own server — as the service manager finds them, installing or
removing them through the checkout's own installer, and stopping or starting them without
either (`control`), so the Mac app needs no terminal for any of it.

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
#: The units, in the order `keryx service` lists them: the one list of them, which
#: anything that walks every unit reads.
KINDS = ("agent", "tunnel", "llm", "voice", "voice_llm")
#: The installers' flag for each model server's unit, and the unit it is.
FLAGS = {"--llm": "llm", "--voice": "voice", "--voice-llm": "voice_llm"}
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


def owner_mismatch(
    settings: Settings, kinds: Sequence[str], *, service_manager: str | None = None,
    platform: str | None = None,
) -> service.ForeignUnits | None:
    """Whether any of the `kinds` units was rendered for another `KERYX_HOME` than this
    process's (`service.foreign_units`): asked before anything drives `launchctl` or
    `systemctl`, by every command that does, under `service_manager` (this machine's,
    `manager`, by default); None with no manager. The tunnel renders no Keryx environment
    of its own, and belongs to whichever Keryx its agent unit does: it is judged by that
    unit, or as the default home's when it is installed alone."""
    found = service_manager or manager(settings, platform or voice_setup.PLATFORM)
    if found is None:
        return None
    agent = ServiceTarget(found, unit_name(settings, found, "agent"))
    targets = []
    for kind in kinds:
        if kind == "tunnel" and service.unit_owners(agent):
            targets.append(agent)
        else:
            targets.append(ServiceTarget(found, unit_name(settings, found, kind)))
    return service.foreign_units(targets)


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
    """How an install or removal ended: its exit code, and what to say about it — with a
    `detail` only on a refusal of units another `KERYX_HOME` rendered (`owner_mismatch`)."""

    code: int
    message: str
    detail: str | None = None


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
    voice_llm: bool = False,
    uninstall: bool = False,
    root: Path,
    platform: str | None = None,
    run: Callable[[Sequence[str]], tuple[int, str, str]] = run_captured,
) -> Outcome:
    """Install (or, with `uninstall`, remove) Keryx and its tunnel, or with `llm`/`voice`/
    `voice_llm` the model servers' units, through the checkout at `root`. 2 for no
    installer here — no checkout, no service manager — or units another `KERYX_HOME`
    rendered (`owner_mismatch`, asked before the installer runs, or the installer's own
    check refusing them: exit 2 saying `KERYX_HOME`), 1 with the script's last error line, 0
    when done."""
    chosen = {"llm": llm, "voice": voice, "voice_llm": voice_llm}
    flags = [flag for flag, kind in FLAGS.items() if chosen[kind]]
    script, problem = installer(" ".join(flags), root=root, platform=platform)
    if script is None:
        return Outcome(REFUSED, problem or "no installer")
    kinds = [FLAGS[flag] for flag in flags] or list(MAIN_KINDS)
    found = "launchd" if script.name == "install-launchd.sh" else "systemd"
    settings = settings or Settings()
    if foreign := owner_mismatch(settings, kinds, service_manager=found):
        return Outcome(REFUSED, foreign.message, foreign.detail)
    argv = [str(script), *flags, *(["--uninstall"] if uninstall else [])]
    code, out, err = run(argv)
    if code == REFUSED and service.HOME_ENV in err:
        # The installer's own check (`scripts/lib.sh::require_own_units`) refused: said as
        # this one says it, the path-free sentence first and the installer's lines after.
        lines = [line.strip() for line in err.splitlines() if line.strip()]
        return Outcome(REFUSED, service.FOREIGN_UNITS, " ".join(lines))
    if code != 0:
        return Outcome(FAILED, _last_line(err) or f"{script.name} {' '.join(argv[1:])} exited "
                                                  f"with {code}")
    names = ", ".join(unit_name(settings, found, kind) for kind in kinds)
    return Outcome(OK, f"{'removed' if uninstall else 'installed'} {names}")


# --- stopping and starting them ----------------------------------------------------------

#: What `keryx service stop|start` acts on without `--all`: Keryx, and its tunnel, which
#: serves nothing else and without which nobody can reach Keryx. `--all` adds every other
#: unit in `NAMES` — the model servers, and whatever that table gains later.
MAIN_KINDS = ("agent", "tunnel")
#: The model servers' units: every other one in `KINDS`.
SERVER_KINDS = tuple(kind for kind in KINDS if kind not in MAIN_KINDS)
#: The server units that read from another, and the ones they read from: the voice's words
#: come from the voice LLM's own server, or the model server's.
STARTS_AFTER = {"voice": ("llm", "voice_llm")}
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
NOT_SETTLED = ("Keryx is stopped, but its restart record couldn't be updated: if a restart "
               "was under way, you may still get a call saying Keryx didn't come back.")
#: What a `settle` callback answers when it could not write: the record stayed locked.
SETTLE_LOCKED = "locked"

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


def start_order(kinds: list[str]) -> list[str]:
    """`kinds` in the order a start takes them: the model servers first, each after the
    ones it reads from (`STARTS_AFTER`), then the tunnel, then Keryx. A stop is the
    reverse: Keryx first, then what it uses."""
    servers = [kind for kind in kinds if kind not in MAIN_KINDS]
    ordered: list[str] = []

    def place(kind: str) -> None:
        if kind in ordered or kind not in servers:
            return
        for first in STARTS_AFTER.get(kind, ()):
            place(first)
        ordered.append(kind)

    for kind in servers:
        place(kind)
    return [*ordered, *(kind for kind in MAIN_KINDS[::-1] if kind in kinds)]


def _enablement_command(target: ServiceTarget, action: str) -> list[str]:
    return target.disable_command() if action == "stop" else target.enable_command()


def _run_command(target: ServiceTarget, action: str, *, loaded: bool) -> list[str] | None:
    """The command that stops or starts one unit, once its enablement is set; None for a
    launchd job already unloaded, which has nothing to stop."""
    if action == "stop":
        return target.stop_command() if loaded or target.manager == "systemd" else None
    if target.manager == "systemd":
        return target.start_command()
    return target.kickstart_command() if loaded else target.start_command()


def control(
    settings: Settings,
    action: str,
    *,
    every: bool = False,
    settle: Callable[[], str] | None = None,
    platform: str | None = None,
    run: Runner | None = None,
    loaded: Callable[[ServiceTarget], bool] | None = None,
    on_disk: Callable[[ServiceTarget], bool] | None = None,
    active: Callable[[ServiceTarget], bool] | None = None,
    enabled: Callable[[ServiceTarget], bool | None] | None = None,
) -> Control:
    """`keryx service stop|start` (`action`): Keryx and its tunnel, or with `every` every
    unit there is, stopped and kept from starting at login, or let start and started —
    without removing any unit file, so no installer runs and macOS asks nothing.

    Units another `KERYX_HOME` rendered (`owner_mismatch`) are refused (2) before anything
    else, `settle` included: nothing is stopped, so no restart is settled. Otherwise a stop
    calls `settle` first of all — the restart in flight, told it was the owner — before any
    probe or other early return; when it answers `SETTLE_LOCKED` the units are
    stopped all the same, and the stop is reported failed (`NOT_SETTLED`). Then two passes
    over the units installed: every one disabled (a start: enabled), and only then each
    stopped (bootout) or started
    (bootstrap, kickstart). So a stop cut short in its slow half, a bootout waiting on
    Keryx, has already kept every unit from starting at login.

    A unit not installed is left alone and said so; starting Keryx itself when it is not
    is refused (2) before anything runs. A failed bootout, bootstrap or kickstart counts
    only while launchd does not hold what was asked (it can refuse a bootout of a job that
    is going anyway); a failed disable or enable only while the manager's own list
    (`launchctl print-disabled`, `systemctl is-enabled`) does not show it. 1 when any unit
    failed, every failure in `detail`, redacted.
    """
    found = manager(settings, platform or voice_setup.PLATFORM)
    kinds = [kind for kind in NAMES[found] if every or kind in MAIN_KINDS] if found else []
    if found is not None and (foreign := owner_mismatch(settings, kinds,
                                                        service_manager=found)):
        return Control(False, REFUSED, foreign.message, foreign.detail, [])
    settled = settle() if action == "stop" and settle is not None else None
    if found is None:
        return Control(False, REFUSED, NO_MANAGER, None, [])
    run = run or run_captured
    is_loaded = loaded or service.is_installed
    has_file = on_disk or unit_on_disk
    is_running = active or service.is_active
    is_enabled = enabled or service.is_enabled
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
    # Started what is used first and Keryx last; stopped the other way round.
    order = start_order(kinds)
    acting = [kind for kind in (order[::-1] if action == "stop" else order) if present[kind]]
    failures: dict[str, str] = {}

    def attempt(kind: str, argv: list[str], *, holds: Callable[[], bool]) -> None:
        code, out, err = run(argv)
        if code != 0 and not holds():
            said = _last_line(err) or _last_line(out) or f"exited with {code}"
            failures.setdefault(kind, f"{' '.join(argv)}: {said}")

    wanted = action == "start"
    for kind in acting:
        target = targets[kind]
        attempt(kind, _enablement_command(target, action),
                holds=lambda target=target: is_enabled(target) is wanted)
    for kind in acting:
        target = targets[kind]
        argv = _run_command(target, action, loaded=is_loaded(target))
        if argv is None or (wanted and kind in failures):
            continue  # nothing loaded to stop; or a job launchd will not let start
        attempt(kind, argv, holds=lambda target=target: target.manager == "launchd"
                and is_loaded(target) is wanted)
    for kind in acting:
        rows[kind]["state"] = (UNIT_FAILED if kind in failures
                               else STOPPED if action == "stop" else RUNNING)
    if failures:
        detail = redact_known("; ".join(failures[kind] for kind in acting
                                        if kind in failures), settings)
        return Control(False, FAILED, COULD_NOT[action], detail, list(rows.values()))
    if settled == SETTLE_LOCKED:
        # Stopped all the same — disabled and booted out — but said: the watchdog of a
        # restart in flight still reads `pending`.
        return Control(False, FAILED, NOT_SETTLED, "the restart record stayed locked",
                       list(rows.values()))
    servers = any(kind not in MAIN_KINDS for kind in acting)
    if not present["agent"]:
        message = SERVERS_STOPPED if servers else NOTHING_TO_STOP
    elif action == "stop":
        message = STOPPED_ALL if servers else STOPPED_MAIN
    else:
        message = RUNNING_ALL if servers else RUNNING_MAIN
    return Control(True, OK, message, None, list(rows.values()))
