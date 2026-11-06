"""`keryx service install|uninstall`, against a scratch HOME with every command faked.

Nothing reaches the machine's own service manager: `run` records the commands and `which`
answers from a table. What is checked is the unit files written, read back the way systemd
and launchd read them. The paths are awkward on purpose: a space, a `%` (a systemd
specifier), a `$` (expanded in an `Exec*=` argument), and `&<>` (markup in a plist).
"""

import os
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from keryx.config import Settings
from keryx.config.settings import SOURCE_ROOT
from keryx.install import Install
from keryx.setup import service
from keryx.setup.service import ServiceError

AWKWARD = "%h $HOME &<x> \"q\"\\w"


def systemd_unquote(value: str) -> str:
    """What systemd makes of a quoted `Environment=` value: C escapes, then `%%`."""
    assert value.startswith('"') and value.endswith('"'), value
    return re.sub(r"\\(.)|%%", lambda m: m.group(1) or "%", value[1:-1])


def systemd_command(line: str) -> list[str]:
    """What systemd runs for an `Exec*=` line: its quoted words, C escapes, then `%%`, and
    `$$` in each argument (the executable itself is never expanded)."""
    executable, *arguments = shlex.split(line.split("=", 1)[1])
    return [executable.replace("%%", "%")] + [
        re.sub(r"%%|\$\$", lambda m: m.group()[0], argument) for argument in arguments
    ]


class Machine:
    """The commands an install ran, and what each answers."""

    def __init__(self, tmp_path):
        self.calls: list[list[str]] = []
        self.failing: set[str] = set()
        self.bin = tmp_path / f"b in {AWKWARD}"
        self.found = {name: str(self.bin / name) for name in ("uv", "cloudflared", "ngrok")}

    def which(self, name):
        return self.found.get(name)

    def run(self, command, **_):
        self.calls.append(command)
        failed = any(" ".join(command).startswith(prefix) for prefix in self.failing)
        return subprocess.CompletedProcess(command, 1 if failed else 0)

    def ran(self, *words) -> bool:
        return list(words) in self.calls


@pytest.fixture
def machine(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"/opt/tools|x&y%z <v2>{os.pathsep}/usr/bin")
    monkeypatch.setenv("USER", "tester")
    return Machine(tmp_path)


def make_settings(tmp_path, **values) -> Settings:
    values.setdefault("public_host", "keryx.example.com")
    return Settings(_env_file=None, openai_api_key="test", **values)


def install(settings, machine, *, platform="linux", kind=Install.INSTALLER, echo=None):
    lines = []
    service.install(settings, echo=echo or lines.append, run=machine.run, which=machine.which,
                    platform=platform, kind=kind)
    return lines


def unit(name: str) -> str:
    return (Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user" / name).read_text()


def unit_settings(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines()
                if "=" in line and not line.startswith(("#", "Environment=")))


def environment(text: str) -> dict[str, str]:
    values = [systemd_unquote(line.removeprefix("Environment="))
              for line in text.splitlines() if line.startswith("Environment=")]
    return dict(value.split("=", 1) for value in values)


# --- systemd ------------------------------------------------------------------------------


def test_the_units_carry_this_shell_path_and_where_keryx_keeps_things(tmp_path, machine):
    state = tmp_path / f"state {AWKWARD}"
    lines = install(make_settings(tmp_path, state_dir=state), machine)

    home = Path(os.environ["HOME"])
    keryx, tunnel = unit("keryx.service"), unit("cloudflared.service")
    assert environment(keryx) == {
        "PATH": os.environ["PATH"],
        "KERYX_HOME": os.environ["KERYX_HOME"],
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "XDG_CACHE_HOME": f"{home}/.cache",
    }
    assert environment(tunnel) == {"PATH": os.environ["PATH"]}
    for text, log in ((keryx, "keryx"), (tunnel, "cloudflared")):
        found = unit_settings(text)
        assert found["StandardOutput"].replace("%%", "%") == f"append:{state}/logs/{log}.out.log"
        assert found["StandardError"].replace("%%", "%") == f"append:{state}/logs/{log}.err.log"
    assert (state / "logs").is_dir()
    assert (state.stat().st_mode & 0o077) == 0  # owner-only, as keryx itself makes it
    assert machine.ran("systemctl", "--user", "enable", "--now", "keryx.service")
    assert machine.ran("systemctl", "--user", "enable", "--now", "cloudflared.service")
    assert machine.ran("loginctl", "enable-linger", "tester")
    assert any("https://keryx.example.com/twilio/voice" in line for line in lines)


def test_an_installed_keryx_runs_as_this_interpreter_from_home(tmp_path, machine):
    install(make_settings(tmp_path), machine, kind=Install.INSTALLER)

    found = unit_settings(unit("keryx.service"))
    assert systemd_command("ExecStart=" + found["ExecStart"]) == [
        sys.executable, "-m", "keryx", "serve"
    ]
    assert found["WorkingDirectory"].replace("%%", "%") == os.environ["HOME"]


def test_a_clone_runs_through_uv_from_the_checkout(tmp_path, machine):
    install(make_settings(tmp_path), machine, kind=Install.CLONE)

    found = unit_settings(unit("keryx.service"))
    assert systemd_command("ExecStart=" + found["ExecStart"]) == [
        machine.found["uv"], "run", "--project", str(SOURCE_ROOT), "keryx", "serve"
    ]
    assert found["WorkingDirectory"] == str(SOURCE_ROOT).replace("%", "%%")


def test_a_clone_without_uv_says_so(tmp_path, machine):
    del machine.found["uv"]

    with pytest.raises(ServiceError, match="uv is not on PATH"):
        install(make_settings(tmp_path), machine, kind=Install.CLONE)


def test_the_tunnel_keeps_an_awkward_name_whole(tmp_path, machine):
    install(make_settings(tmp_path, cloudflare_tunnel=f"tun {AWKWARD}", port=9090), machine)

    [line] = [line for line in unit("cloudflared.service").splitlines()
              if line.startswith("ExecStart=")]
    assert systemd_command(line) == [
        machine.found["cloudflared"], "tunnel", "--no-autoupdate", "--protocol", "http2", "run",
        "--url", "http://localhost:9090", f"tun {AWKWARD}",
    ]


def test_no_public_host_says_how_to_set_one(tmp_path, machine):
    with pytest.raises(ServiceError, match="keryx config set PUBLIC_HOST"):
        install(make_settings(tmp_path, public_host=None), machine)
    assert machine.calls == []


def test_a_missing_tunnel_says_how_to_create_it(tmp_path, machine):
    machine.failing.add(f"{machine.found['cloudflared']} tunnel info")

    with pytest.raises(ServiceError, match="cloudflared tunnel create keryx"):
        install(make_settings(tmp_path), machine)
    assert not (Path(os.environ["XDG_CONFIG_HOME"]) / "systemd").exists()


def test_no_cloudflared_says_where_to_get_it(tmp_path, machine):
    del machine.found["cloudflared"]

    with pytest.raises(ServiceError, match="cloudflared is not on PATH"):
        install(make_settings(tmp_path), machine)


def test_a_unit_that_will_not_start_stops_the_install(tmp_path, machine):
    machine.failing.add("systemctl --user enable --now keryx.service")

    with pytest.raises(ServiceError, match="exited 1"):
        install(make_settings(tmp_path), machine)


def test_a_service_manager_that_is_not_there_is_an_error_not_a_traceback(tmp_path, machine):
    def run(command, **_):
        if command[0] == "systemctl":
            raise FileNotFoundError(2, "No such file or directory")
        return subprocess.CompletedProcess(command, 0)

    machine.run = run
    with pytest.raises(ServiceError, match="systemctl"):
        install(make_settings(tmp_path), machine)


def test_linger_that_cannot_be_enabled_says_the_sudo_command(tmp_path, machine):
    machine.failing.add("loginctl")

    lines = install(make_settings(tmp_path), machine)

    assert any("sudo loginctl enable-linger tester" in line for line in lines)


def test_uninstall_stops_and_removes_both_units(tmp_path, machine):
    install(make_settings(tmp_path), machine)

    service.uninstall(echo=lambda line: None, run=machine.run, platform="linux")

    units = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user"
    assert list(units.iterdir()) == []
    assert machine.ran("systemctl", "--user", "disable", "--now", "keryx.service")


# --- launchd ------------------------------------------------------------------------------


def agents_dir() -> Path:
    return Path(os.environ["HOME"]) / "Library" / "LaunchAgents"


def test_the_launch_agents_parse_and_carry_the_same(tmp_path, machine):
    state = tmp_path / f"state {AWKWARD}"
    install(make_settings(tmp_path, state_dir=state), machine, platform="darwin")

    agent = plistlib.loads((agents_dir() / "dev.keryx.agent.plist").read_bytes())
    tunnel = plistlib.loads((agents_dir() / "dev.keryx.tunnel.plist").read_bytes())
    assert agent["ProgramArguments"] == [sys.executable, "-m", "keryx", "serve"]
    assert tunnel["ProgramArguments"] == [
        machine.found["ngrok"], "http", "--domain=keryx.example.com", "8080"
    ]
    assert agent["EnvironmentVariables"]["PATH"] == os.environ["PATH"]
    assert agent["EnvironmentVariables"]["KERYX_HOME"] == os.environ["KERYX_HOME"]
    assert tunnel["EnvironmentVariables"]["HOME"] == os.environ["HOME"]
    assert agent["StandardErrorPath"] == f"{state}/logs/keryx.err.log"
    assert tunnel["StandardOutPath"] == f"{state}/logs/ngrok.out.log"
    domain = f"gui/{os.getuid()}"
    assert machine.ran("launchctl", "bootout", f"{domain}/dev.keryx.agent")
    assert machine.ran("launchctl", "bootstrap", domain,
                       str(agents_dir() / "dev.keryx.agent.plist"))


def test_no_ngrok_says_where_to_get_it(tmp_path, machine):
    del machine.found["ngrok"]

    with pytest.raises(ServiceError, match="brew install ngrok"):
        install(make_settings(tmp_path), machine, platform="darwin")


def test_uninstall_on_macos_removes_only_what_is_there(tmp_path, machine):
    install(make_settings(tmp_path), machine, platform="darwin")
    (agents_dir() / "dev.keryx.tunnel.plist").unlink()
    lines = []

    service.uninstall(echo=lines.append, run=machine.run, platform="darwin")

    assert lines == ["removed dev.keryx.agent"]
    assert list(agents_dir().iterdir()) == []


def test_another_platform_is_refused(tmp_path, machine):
    with pytest.raises(ServiceError, match="Linux"):
        install(make_settings(tmp_path), machine, platform="win32")
