"""`keryx service install|uninstall`: what keeps Keryx and its tunnel running.

Two systemd user units on Linux (`keryx.service`, and `cloudflared.service` for the tunnel)
and two launchd agents on macOS (`dev.keryx.agent`, and `dev.keryx.tunnel` running ngrok: a
reserved ngrok domain needs no DNS zone, the right trade on a laptop). Linger keeps the
systemd units running with nobody logged in, so the machine answers after a reboot.

Each unit is written from this process's view of the machine, which is why it is written
here and not by hand:

- **its PATH**, so subagents find what a terminal finds (nvm, pixi, conda, cargo,
  ~/.local/bin). A user manager's own is bare: install again after it changes.
- **where Keryx keeps things**, `KERYX_HOME` and the four XDG base directories as resolved
  here, so the service reads exactly the configuration this command did.
- **how Keryx starts**: from a clone, `uv run --project <clone> keryx serve`, so a pull that
  moved the dependencies is synced on restart; otherwise this interpreter's
  `-m keryx serve`, which survives an upgrade and needs nothing on PATH.

Logs go to `STATE_DIR/logs`, where `keryx restart` reads them back. Values are escaped for
where they land: systemd unquotes `"…"` C-style, expands `%` specifiers everywhere and `$`
in an argument; plistlib writes the XML.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from keryx.config import Settings
from keryx.config.files import HOME_ENV, XDG_HOMES, keryx_home, xdg_home
from keryx.config.settings import SOURCE_ROOT
from keryx.install import Install, how_installed, keryx_command
from keryx.restart.logscan import log_dir
from keryx.restart.service import LAUNCHD_LABEL, SYSTEMD_UNIT

TUNNEL_UNIT = "cloudflared.service"
TUNNEL_LABEL = "dev.keryx.tunnel"
CLOUDFLARED_DOCS = "https://developers.cloudflare.com/cloudflare-one/"


class ServiceError(Exception):
    """Why the service could not be installed, said so the owner can fix it."""


Run = Callable[..., subprocess.CompletedProcess]
Echo = Callable[[str], None]


# --- escaping -------------------------------------------------------------------------


def systemd_path(value: str | Path) -> str:
    """A value for a setting that takes it unquoted (`WorkingDirectory=`, `append:`)."""
    return str(value).replace("%", "%%")


def systemd_quoted(value: str) -> str:
    """A value for inside `"…"`: a backslash or quote would escape or end it, `%` expand."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def exec_line(argv: list[str]) -> str:
    """An `Exec*=` command line: every word quoted, and `$` escaped in each argument (the
    executable is never expanded)."""
    words = [systemd_quoted(argv[0])]
    words += [systemd_quoted(arg).replace("$", "$$") for arg in argv[1:]]
    return " ".join(f'"{word}"' for word in words)


# --- what the units say ---------------------------------------------------------------


def locations() -> dict[str, str]:
    """`KERYX_HOME` and the XDG base directories, as this process resolves them."""
    return {
        HOME_ENV: str(keryx_home()),
        **{variable: str(xdg_home(kind)) for kind, (variable, _) in XDG_HOMES.items()},
    }


def serve_command(kind: Install, which: Callable[[str], str | None]) -> tuple[list[str], Path]:
    """How the service starts Keryx, and the directory it starts in."""
    if kind is not Install.CLONE:
        return [*keryx_command(), "serve"], Path.home()
    uv = which("uv")
    if uv is None:
        raise ServiceError("uv is not on PATH (https://docs.astral.sh/uv/), and this Keryx "
                           "runs from a clone with it")
    return [uv, "run", "--project", str(SOURCE_ROOT), "keryx", "serve"], SOURCE_ROOT


def systemd_unit(description: str, argv: list[str], *, environment: dict[str, str],
                 logs: Path, log_name: str, working_dir: Path | None = None) -> str:
    lines = [
        "# Written by `keryx service install`: run it again rather than editing this file.",
        "",
        "[Unit]",
        f"Description={description}",
        "",
        "[Service]",
        "Type=simple",
        *([f"WorkingDirectory={systemd_path(working_dir)}"] if working_dir else []),
        f"ExecStart={exec_line(argv)}",
        *(f'Environment="{systemd_quoted(f"{name}={value}")}"'
          for name, value in environment.items()),
        # A crash, and the "started before the network was up" case at boot.
        "Restart=always",
        "RestartSec=5",
        f"StandardOutput=append:{systemd_path(logs / f'{log_name}.out.log')}",
        f"StandardError=append:{systemd_path(logs / f'{log_name}.err.log')}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def launchd_agent(label: str, argv: list[str], *, environment: dict[str, str], logs: Path,
                  log_name: str, working_dir: Path | None = None) -> bytes:
    agent = {
        "Label": label,
        "ProgramArguments": argv,
        **({"WorkingDirectory": str(working_dir)} if working_dir else {}),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs / f"{log_name}.out.log"),
        "StandardErrorPath": str(logs / f"{log_name}.err.log"),
    }
    return plistlib.dumps(agent, sort_keys=False)


# --- installing -------------------------------------------------------------------------


def systemd_dir() -> Path:
    return xdg_home("config") / "systemd" / "user"


def launchd_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def install(
    settings: Settings,
    *,
    echo: Echo = print,
    run: Run = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
    kind: Install | None = None,
) -> None:
    """Write the units, start them, and say how to look at them. Re-runnable."""
    if not settings.public_host:
        hint = ("a reserved ngrok domain" if platform == "darwin"
                else "the hostname routed to the Cloudflare tunnel")
        raise ServiceError(f"PUBLIC_HOST is not set ({hint}): run `keryx setup`, or\n"
                           "  keryx config set PUBLIC_HOST keryx.example.com")
    argv, working_dir = serve_command(kind or how_installed(), which)
    settings.ensure_dirs()
    logs = log_dir(settings.state_dir)
    path = os.environ.get("PATH", "")
    if platform.startswith("linux"):
        _install_systemd(settings, argv, working_dir, logs, path, echo, run, which)
    elif platform == "darwin":
        _install_launchd(settings, argv, working_dir, logs, path, echo, run, which)
    else:
        raise ServiceError("the service runs on Linux (systemd) and macOS (launchd)")
    echo(f"\nPoint the Twilio number's voice webhook at https://{settings.public_host}"
         "/twilio/voice (HTTP POST)\nand the status callback at "
         f"https://{settings.public_host}/twilio/status.")


def _checked(run: Run, command: list[str]) -> None:
    """Run `command` where its output shows, and stop at its first failure."""
    try:
        code = run(command, check=False).returncode
    except OSError as error:
        raise ServiceError(f"{command[0]}: {error.strerror}") from None
    if code != 0:
        raise ServiceError(f"{' '.join(command)} exited {code}")


def _quiet(run: Run, command: list[str]) -> int:
    """Run `command` for its exit code alone; one that cannot start at all failed."""
    try:
        return run(command, capture_output=True, check=False).returncode
    except OSError:
        return 127


def _install_systemd(settings, argv, working_dir, logs, path, echo, run, which) -> None:
    tunnel = settings.cloudflare_tunnel or "keryx"
    cloudflared = which("cloudflared")
    if cloudflared is None:
        raise ServiceError(f"cloudflared is not on PATH ({CLOUDFLARED_DOCS})")
    if _quiet(run, [cloudflared, "tunnel", "info", tunnel]):
        raise ServiceError(
            f'no Cloudflare tunnel named "{tunnel}" on this machine. Create it once (opens a '
            f"browser):\n\n  cloudflared tunnel login\n  cloudflared tunnel create {tunnel}\n"
            f"  cloudflared tunnel route dns {tunnel} {settings.public_host}\n\n"
            "To use a different name: keryx config set CLOUDFLARE_TUNNEL <name>"
        )
    units = {
        SYSTEMD_UNIT: systemd_unit(
            "Keryx voice agent (phone channel)", argv, working_dir=working_dir,
            environment={"PATH": path, **locations()}, logs=logs, log_name="keryx"),
        TUNNEL_UNIT: systemd_unit(
            "Cloudflare tunnel for Keryx",
            [cloudflared, "tunnel", "--no-autoupdate", "--protocol", "http2", "run",
             "--url", f"http://localhost:{settings.port}", tunnel],
            environment={"PATH": path}, logs=logs, log_name="cloudflared"),
    }
    directory = systemd_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (directory / name).write_text(text, encoding="utf-8")
    _checked(run, ["systemctl", "--user", "daemon-reload"])
    for name in units:
        _checked(run, ["systemctl", "--user", "enable", "--now", name])
        echo(f"started {name} ({directory / name})")
    # Without linger, the user manager stops at logout and takes both units with it.
    user = os.environ.get("USER") or Path.home().name
    if _quiet(run, ["loginctl", "enable-linger", user]):
        echo(f"could not enable linger: run  sudo loginctl enable-linger {user}")
    echo(f"\nKeryx is running under systemd.\n\n"
         f"  status:  systemctl --user status keryx cloudflared\n"
         f"  logs:    tail -f {logs}/keryx.err.log {logs}/keryx.log\n"
         f"  stop:    keryx service uninstall")


def _install_launchd(settings, argv, working_dir, logs, path, echo, run, which) -> None:
    ngrok = which("ngrok")
    if ngrok is None:
        raise ServiceError("ngrok is not on PATH (brew install ngrok)")
    home = {"PATH": path, "HOME": str(Path.home())}
    agents = {
        LAUNCHD_LABEL: launchd_agent(
            LAUNCHD_LABEL, argv, working_dir=working_dir, environment={**home, **locations()},
            logs=logs, log_name="keryx"),
        TUNNEL_LABEL: launchd_agent(
            TUNNEL_LABEL,
            [ngrok, "http", f"--domain={settings.public_host}", str(settings.port)],
            environment=home, logs=logs, log_name="ngrok"),
    }
    directory = launchd_dir()
    directory.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for label, plist in agents.items():
        file = directory / f"{label}.plist"
        file.write_bytes(plist)
        # An older version may still be loaded; booting it out first makes this re-runnable.
        _quiet(run, ["launchctl", "bootout", f"{domain}/{label}"])
        _checked(run, ["launchctl", "bootstrap", domain, str(file)])
        echo(f"loaded {label} ({file})")
    echo(f"\nKeryx is running under launchd.\n\n"
         f"  status:  launchctl print {domain}/{LAUNCHD_LABEL} | head -20\n"
         f"  logs:    tail -f {logs}/keryx.err.log {logs}/keryx.log\n"
         f"  stop:    keryx service uninstall")


def uninstall(*, echo: Echo = print, run: Run = subprocess.run,
              platform: str = sys.platform) -> None:
    """Stop the units and delete them. Leaves the logs and everything Keryx keeps."""
    if platform == "darwin":
        domain = f"gui/{os.getuid()}"
        for label in (LAUNCHD_LABEL, TUNNEL_LABEL):
            plist = launchd_dir() / f"{label}.plist"
            if plist.is_file():
                _quiet(run, ["launchctl", "bootout", f"{domain}/{label}"])
                plist.unlink()
                echo(f"removed {label}")
        return
    for name in (SYSTEMD_UNIT, TUNNEL_UNIT):
        _quiet(run, ["systemctl", "--user", "disable", "--now", name])
        (systemd_dir() / name).unlink(missing_ok=True)
        echo(f"removed {name}")
    _quiet(run, ["systemctl", "--user", "daemon-reload"])
