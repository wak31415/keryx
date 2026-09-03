"""Talking to the service manager: what to restart, and what to leave watching.

Split out of `restart.py` (2026-09-02). Both halves are the same conversation — one hands
the service manager a restart, the other hands it a watchdog — and both are the reason
`ServiceTarget` and `watch_command` live together rather than a module apart.

Nothing supervising this process means `resolve_target` returns `None`, and a restart is
then refused rather than attempted: stopping would leave nothing to start it again.
"""

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis.config import Settings

log = logging.getLogger("jarvis.restart")

#: Default unit/label names, matching `ops/systemd/jarvis.service` and
#: `ops/launchd/dev.jarvis.agent.plist`.
SYSTEMD_UNIT = "jarvis.service"
LAUNCHD_LABEL = "dev.jarvis.agent"

#: The transient unit the watchdog runs as, suffixed with the pid that armed it so a
#: second restart during a crash loop does not collide with the watch still running.
WATCH_UNIT_PREFIX = "jarvis-restart-watch"
#: Where the watchdog's own output goes, under `data_dir/logs`.
WATCH_LOG_NAME = "restart-watch.log"

#: What `jarvis restart` prints when there is no service manager to ask.
UNSUPPORTED_HINT = """no service manager to restart through on this machine.

Install the service first (it is what starts Jarvis again after it stops):

  scripts/install-systemd.sh    # Linux
  scripts/install-launchd.sh    # macOS

Or set SERVICE_MANAGER / SERVICE_UNIT in .env if the unit is named something else. To
restart by hand instead, stop the process and start `jarvis serve` again — nothing else
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


def resolve_target(
    settings: Settings,
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> ServiceTarget | None:
    """The service manager to restart through, or None when nothing supervises us.

    `SERVICE_MANAGER=auto` (the default) picks systemd on Linux and launchd on macOS, and
    only when the tool is actually on PATH — a Jarvis started by hand from a terminal has
    nothing that would bring it back, and must refuse to stop rather than take itself off
    the air. `none` refuses outright.
    """
    manager = settings.service_manager
    if manager == "auto":
        if platform.startswith("linux") and which("systemctl"):
            manager = "systemd"
        elif platform == "darwin" and which("launchctl"):
            manager = "launchd"
        else:
            manager = "none"
    if manager == "none":
        return None
    if not which("systemctl" if manager == "systemd" else "launchctl"):
        log.warning("SERVICE_MANAGER=%s but its command is not on PATH", manager)
        return None
    default = SYSTEMD_UNIT if manager == "systemd" else LAUNCHD_LABEL
    return ServiceTarget(manager, settings.service_unit or default)


# --- the watchdog that outlives the restart ---------------------------------


@dataclass(frozen=True)
class WatchPlan:
    """How to start the watchdog: the command, what to call it, and who redirects it."""

    argv: list[str]
    label: str
    #: True when we have to point its output at a file ourselves; systemd does it for us.
    redirect: bool


def watch_command(
    settings: Settings,
    target: ServiceTarget,
    *,
    which: Callable[[str], str | None] = shutil.which,
    pid: int | None = None,
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
    inner = [sys.executable, "-m", "jarvis", "restart-watch"]
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
            "--",
            *inner,
        ],
        unit,
        redirect=False,
    )


def watch_log_path(settings: Settings) -> Path:
    """Where the watchdog writes; it has no other way to be heard if it fails itself."""
    return settings.data_dir / "logs" / WATCH_LOG_NAME


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
