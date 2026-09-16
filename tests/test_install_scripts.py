"""The installers, run for real against a scratch HOME with every service manager faked.

`scripts/install-*.sh` render `ops/` into unit files and hand them to `systemctl` or
`launchctl`. Here HOME, the config directories and the env file are all under `tmp_path`,
and `systemctl`, `loginctl`, `launchctl`, `cloudflared`, `ngrok` and `uv` are stubs first on
PATH that only record how they were called — so what is checked is exactly what the
installer writes, and nothing reaches the machine's own service manager.

The PATH the installer runs with carries a directory whose name holds the characters that
break a naive render: `|` is `render`'s sed delimiter, `&` means "the whole match" to sed,
`%` is a systemd specifier, and `&`/`<` are markup in a plist.
"""

import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BASH = shutil.which("bash")
STUBS = ("systemctl", "loginctl", "launchctl", "cloudflared", "ngrok", "uv")
#: A PATH entry nobody would choose, and a stranger's machine might still have.
AWKWARD_DIR = '/opt/tools|x&y%z <v2> "q"\\w'
PLACEHOLDER = re.compile(r"__[A-Z_]+__")

pytestmark = pytest.mark.skipif(BASH is None, reason="the installers are bash scripts")


@pytest.fixture
def machine(tmp_path):
    """A scratch HOME, an env file, and stub binaries that record every call."""
    home = tmp_path / "home"
    home.mkdir()
    stubs = tmp_path / "bin"
    stubs.mkdir()
    calls = tmp_path / "calls.log"
    for name in STUBS:
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit 0\n')
        stub.chmod(0o755)
    env_file = tmp_path / "jarvis.env"
    env_file.write_text("PUBLIC_HOST=jarvis.example.com\nPORT=8080\n")
    path = os.pathsep.join([str(stubs), AWKWARD_DIR, "/usr/bin", "/bin"])
    environment = {
        "HOME": str(home),
        "USER": "tester",
        "PATH": path,
        "JARVIS_ENV_FILE": str(env_file),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
    }
    return {"home": home, "env_file": env_file, "env": environment, "calls": calls}


def run(script: str, machine, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [BASH, str(SCRIPTS / script), *args],
        env=machine["env"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def lib(machine, snippet: str) -> str:
    """Source `lib.sh` in a clean bash and run `snippet`; its stdout."""
    result = subprocess.run(
        [BASH, "-c", f'source "{SCRIPTS / "lib.sh"}"\n{snippet}'],
        env=machine["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def systemd_unquote(value: str) -> str:
    """What systemd makes of a quoted `Environment=` value: C escapes, then `%%`."""
    assert value.startswith('"') and value.endswith('"'), value
    return re.sub(r"\\(.)|%%", lambda m: m.group(1) or "%", value[1:-1])


# --- DATA_DIR ---------------------------------------------------------------


def test_logs_default_to_the_data_dir_jarvis_uses(machine):
    assert lib(machine, 'printf %s "$LOGS"') == f"{machine['home']}/.jarvis/logs"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("~/elsewhere", "{home}/elsewhere"),
        ('"~/quoted/"', "{home}/quoted"),
        ("/srv/jarvis", "/srv/jarvis"),
        ("state", "{repo}/state"),  # relative to the service's working directory
    ],
)
def test_logs_follow_data_dir_from_the_env_file(machine, configured, expected):
    machine["env_file"].write_text(f"DATA_DIR={configured}\n")

    logs = lib(machine, 'printf %s "$LOGS"')

    assert logs == expected.format(home=machine["home"], repo=REPO) + "/logs"


def test_render_inserts_values_literally(machine, tmp_path):
    template = tmp_path / "template"
    template.write_text("a=__A__\n")

    lib(machine, f'render "{template}" "{tmp_path / "out"}" "A=x|y&z\\\\w"')

    assert (tmp_path / "out").read_text() == "a=x|y&z\\w\n"


# --- systemd ------------------------------------------------------------------


def test_the_systemd_units_carry_this_shell_path_and_data_dir(machine):
    machine["env_file"].write_text(
        "PUBLIC_HOST=jarvis.example.com\nDATA_DIR=~/jarvis-data\n"
    )

    run("install-systemd.sh", machine)

    units = machine["home"] / ".config" / "systemd" / "user"
    logs = machine["home"] / "jarvis-data" / "logs"
    for name, log in (("jarvis", "jarvis"), ("cloudflared", "cloudflared")):
        text = (units / f"{name}.service").read_text()
        assert not PLACEHOLDER.search(text), text
        settings = dict(
            line.split("=", 1) for line in text.splitlines() if "=" in line and line[0] != "#"
        )
        path = systemd_unquote('"' + settings["Environment"].removeprefix('"PATH='))
        assert path == machine["env"]["PATH"]
        assert settings["StandardOutput"] == f"append:{logs}/{log}.out.log"
        assert settings["StandardError"] == f"append:{logs}/{log}.err.log"
    assert logs.is_dir()
    # Created by the installer, so owner-only, as `jarvis` itself would make it.
    assert (logs.parent.stat().st_mode & 0o077) == 0
    assert "systemctl --user enable --now jarvis.service" in machine["calls"].read_text()


# --- launchd ------------------------------------------------------------------


def test_the_launch_agents_carry_this_shell_path_and_data_dir(machine, tmp_path):
    data_dir = tmp_path / "data <&> more"
    machine["env_file"].write_text(f"PUBLIC_HOST=jarvis.example.com\nDATA_DIR={data_dir}\n")

    run("install-launchd.sh", machine)

    agents = machine["home"] / "Library" / "LaunchAgents"
    logs = f"{data_dir}/logs"
    for label, log in (("dev.jarvis.agent", "jarvis"), ("dev.jarvis.tunnel", "ngrok")):
        raw = (agents / f"{label}.plist").read_bytes()
        assert not PLACEHOLDER.search(raw.decode()), raw
        plist = plistlib.loads(raw)  # launchd refuses a plist that does not parse
        assert plist["EnvironmentVariables"]["PATH"] == machine["env"]["PATH"]
        assert plist["StandardOutPath"] == f"{logs}/{log}.out.log"
        assert plist["StandardErrorPath"] == f"{logs}/{log}.err.log"
