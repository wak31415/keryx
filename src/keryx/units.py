"""`keryx service`: Keryx's four units — itself, its tunnel, the model server and the voice
server — as the service manager finds them, and installing or removing them through the
checkout's own installer, so the Mac app needs no terminal for either.

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

from keryx.child_env import operational_env
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


def status(
    settings: Settings,
    *,
    platform: str | None = None,
    installed: Callable[[ServiceTarget], bool] | None = None,
    active: Callable[[ServiceTarget], bool] | None = None,
) -> dict[str, Any]:
    """`keryx service --json`: each unit installed, and running. Only asks."""
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
        here = is_installed(target)
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
