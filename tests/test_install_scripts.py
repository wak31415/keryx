"""The installers, run for real against a scratch HOME with every service manager faked.

`scripts/install-*.sh` render `ops/` into unit files and hand them to `systemctl` or
`launchctl`. Here HOME, JARVIS_HOME and a copy of the repository's `scripts/` and `ops/` are
all under `tmp_path` — a copy, so that a real `.env` in a checkout can never be read — and
`systemctl`, `loginctl`, `launchctl`, `cloudflared`, `ngrok` and `uv` are stubs first on PATH
that only record how they were called. Settings come from `jarvis config get`, run with the
interpreter running the tests, so what is checked is exactly what the installer writes, and
nothing reaches the machine's own service manager.

The PATH the installer runs with carries a directory whose name holds the characters that
break a naive render: `|` is `render`'s sed delimiter, `&` means "the whole match" to sed,
`%` is a systemd specifier, and `&`/`<` are markup in a plist.
"""

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.config.files import dump_toml

SOURCE = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
STUBS = ("systemctl", "loginctl", "launchctl", "cloudflared", "ngrok", "uv")
#: A PATH entry nobody would choose, and a stranger's machine might still have.
AWKWARD_DIR = '/opt/tools|x&y%z <v2> "q"\\w'
PLACEHOLDER = re.compile(r"__[A-Z_]+__")

pytestmark = pytest.mark.skipif(BASH is None, reason="the installers are bash scripts")


@pytest.fixture
def machine(tmp_path):
    """A scratch HOME and JARVIS_HOME, a copy of the scripts, and stubs that record calls."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "re po"  # a space, which the default `uv` command must survive
    for name in ("scripts", "ops"):
        shutil.copytree(SOURCE / name, repo / name)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    calls = tmp_path / "calls.log"
    for name in STUBS:
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit 0\n')
        stub.chmod(0o755)
    jarvis_home = tmp_path / "jarvis-home"
    path = os.pathsep.join([str(stubs), AWKWARD_DIR, "/usr/bin", "/bin"])
    environment = {
        "HOME": str(home),
        "USER": "tester",
        "PATH": path,
        "JARVIS_HOME": str(jarvis_home),
        "JARVIS_CLI": f"{sys.executable} -m jarvis",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
    }
    machine = {"home": home, "repo": repo, "jarvis_home": jarvis_home, "env": environment,
               "calls": calls}
    configure(machine, PUBLIC_HOST="jarvis.example.com", PORT=8080)
    return machine


def configure(machine, **values) -> None:
    """Write `config.toml` in the machine's JARVIS_HOME, replacing what was there."""
    machine["jarvis_home"].mkdir(exist_ok=True)
    (machine["jarvis_home"] / "config.toml").write_text(dump_toml(values))


def run(script: str, machine, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [BASH, str(machine["repo"] / "scripts" / script), *args],
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
        [BASH, "-c", f'source "{machine["repo"] / "scripts" / "lib.sh"}"\n{snippet}'],
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


# --- where things are --------------------------------------------------------


def test_logs_default_to_the_state_dir_jarvis_uses(machine):
    assert lib(machine, 'printf %s "$LOGS"') == f"{machine['home']}/.local/state/jarvis/logs"


def test_logs_follow_state_dir_from_the_configuration(machine):
    configure(machine, STATE_DIR="/srv/jarvis state")

    assert lib(machine, 'printf %s "$LOGS"') == "/srv/jarvis state/logs"


def test_logs_follow_the_xdg_state_directory(machine):
    machine["env"]["XDG_STATE_HOME"] = str(machine["home"] / "xdg state")

    assert lib(machine, 'printf %s "$LOGS"') == f"{machine['home']}/xdg state/jarvis/logs"


def test_every_path_comes_from_jarvis_and_none_is_reassigned(machine):
    """The resolved paths are prefixed, so an exported DATA_DIR is left exactly as it was."""
    machine["env"]["DATA_DIR"] = "/srv/data"

    out = lib(machine, 'printf "%s|%s|%s" "$DATA_DIR" "$JARVIS_DIR" "$RESOLVED_CACHE_DIR"')

    assert out == f"/srv/data|/srv/data|{machine['home']}/.cache/jarvis"


def test_a_env_in_the_repository_is_never_read(machine):
    configure(machine)
    (machine["repo"] / ".env").write_text("STATE_DIR=/srv/from-dotenv\n")

    assert lib(machine, 'printf %s "$LOGS"') == f"{machine['home']}/.local/state/jarvis/logs"


def test_no_public_host_says_how_to_set_one(machine):
    configure(machine)

    result = subprocess.run(
        [BASH, str(machine["repo"] / "scripts" / "install-systemd.sh")],
        env=machine["env"], capture_output=True, text=True, timeout=60, check=False,
    )

    assert result.returncode == 1
    assert "jarvis config set PUBLIC_HOST" in result.stderr


def test_render_inserts_values_literally(machine, tmp_path):
    template = tmp_path / "template"
    template.write_text("a=__A__\n")

    lib(machine, f'render "{template}" "{tmp_path / "out"}" "A=x|y&z\\\\w"')

    assert (tmp_path / "out").read_text() == "a=x|y&z\\w\n"


# --- systemd ------------------------------------------------------------------


def test_the_systemd_units_carry_this_shell_path_and_state_dir(machine):
    configure(
        machine, PUBLIC_HOST="jarvis.example.com", STATE_DIR=str(machine["home"] / "jarvis-state")
    )

    run("install-systemd.sh", machine)

    units = machine["home"] / ".config" / "systemd" / "user"
    logs = machine["home"] / "jarvis-state" / "logs"
    for name, log in (("jarvis", "jarvis"), ("cloudflared", "cloudflared")):
        text = (units / f"{name}.service").read_text()
        assert not PLACEHOLDER.search(text), text
        settings = dict(
            line.split("=", 1) for line in text.splitlines() if "=" in line and line[0] != "#"
        )
        [path_line] = [
            line for line in text.splitlines() if line.startswith('Environment="PATH=')
        ]
        path = systemd_unquote('"' + path_line.removeprefix('Environment="PATH='))
        assert path == machine["env"]["PATH"]
        assert settings["StandardOutput"] == f"append:{logs}/{log}.out.log"
        assert settings["StandardError"] == f"append:{logs}/{log}.err.log"
    jarvis_unit = (units / "jarvis.service").read_text()
    assert f'Environment="JARVIS_HOME={machine["jarvis_home"]}"' in jarvis_unit
    assert logs.is_dir()
    # Created by the installer, so owner-only, as `jarvis` itself would make it.
    assert (logs.parent.stat().st_mode & 0o077) == 0
    assert "systemctl --user enable --now jarvis.service" in machine["calls"].read_text()


# --- launchd ------------------------------------------------------------------


def test_the_launch_agents_carry_this_shell_path_and_state_dir(machine, tmp_path):
    state_dir = tmp_path / "state <&> more"
    configure(machine, PUBLIC_HOST="jarvis.example.com", STATE_DIR=str(state_dir))

    run("install-launchd.sh", machine)

    agents = machine["home"] / "Library" / "LaunchAgents"
    logs = f"{state_dir}/logs"
    for label, log in (("dev.jarvis.agent", "jarvis"), ("dev.jarvis.tunnel", "ngrok")):
        raw = (agents / f"{label}.plist").read_bytes()
        assert not PLACEHOLDER.search(raw.decode()), raw
        plist = plistlib.loads(raw)  # launchd refuses a plist that does not parse
        assert plist["EnvironmentVariables"]["PATH"] == machine["env"]["PATH"]
        assert plist["StandardOutPath"] == f"{logs}/{log}.out.log"
        assert plist["StandardErrorPath"] == f"{logs}/{log}.err.log"
    agent = plistlib.loads((agents / "dev.jarvis.agent.plist").read_bytes())
    assert agent["EnvironmentVariables"]["JARVIS_HOME"] == str(machine["jarvis_home"])


# --- the approval hook -----------------------------------------------------------


def hook_commands(machine) -> dict[str, str]:
    settings = json.loads((machine["home"] / ".claude" / "settings.json").read_text())
    return {
        event: groups[0]["hooks"][0]["command"] for event, groups in settings["hooks"].items()
    }


def test_the_hook_is_always_told_where_the_state_dir_is(machine):
    """Even at the default: the hook never reads the configuration, and one that guessed
    differently from the broker would leave every prompt unescalated."""
    run("install-claude-hook.sh", machine)

    commands = hook_commands(machine)
    target = machine["home"] / ".claude" / "hooks" / "jarvis_approval.py"
    state_dir = f"{machine['home']}/.local/state/jarvis"
    assert shlex.split(commands["PermissionRequest"]) == [
        "env",
        f"JARVIS_STATE_DIR={state_dir}",
        "python3",
        str(target),
    ]
    assert f"[ -e {state_dir}/approvals/PENDING ]" in shlex.split(commands["PostToolUse"])[2]


def test_the_hook_is_pointed_at_a_state_dir_set_in_the_configuration(machine):
    configure(machine, STATE_DIR=str(machine["home"] / "jarvis state"))

    run("install-claude-hook.sh", machine)

    state_dir = f"{machine['home']}/jarvis state"
    commands = hook_commands(machine)
    assert shlex.split(commands["PermissionRequest"])[:2] == [
        "env",
        f"JARVIS_STATE_DIR={state_dir}",
    ]
    shell = shlex.split(commands["PostToolUse"])
    assert shell[:2] == ["sh", "-c"]
    assert f"[ -e '{state_dir}/approvals/PENDING' ]" in shell[2]
    assert f"exec {commands['PermissionRequest']};" in shell[2]


def test_the_resolve_command_still_runs_when_nothing_is_pending(machine):
    configure(machine, STATE_DIR=str(machine["home"] / "jarvis state"))
    run("install-claude-hook.sh", machine)

    result = subprocess.run(
        hook_commands(machine)["PostToolUse"],
        shell=True,  # noqa: S602 - it is a shell command line; this is how the CLI runs it
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=machine["env"],
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_default_command_keeps_a_repository_path_with_a_space_whole(machine, tmp_path):
    """Without JARVIS_CLI, `uv run --project <repo> jarvis config get` — the path one argument."""
    stub = tmp_path / "bin" / "uv"
    stub.write_text('#!/bin/sh\nfor arg in "$@"; do echo "[$arg]"; done\n')
    env = {key: value for key, value in machine["env"].items() if key != "JARVIS_CLI"}

    result = subprocess.run(
        [BASH, "-c", f'source "{machine["repo"] / "scripts" / "lib.sh"}"\nconfig_value PORT'],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )

    assert f"[{machine['repo']}]" in result.stdout.splitlines()
    assert result.stdout.splitlines()[-3:] == ["[config]", "[get]", "[PORT]"]
