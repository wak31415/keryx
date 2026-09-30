"""The installers, run for real against a scratch HOME with every service manager faked.

`scripts/install-*.sh` render `ops/` into unit files and hand them to `systemctl` or
`launchctl`. Here HOME, KERYX_HOME and a copy of the repository's `scripts/` and `ops/` are
all under `tmp_path` — a copy, so that a real `.env` in a checkout can never be read — and
`systemctl`, `loginctl`, `launchctl`, `cloudflared`, `ngrok` and `uv` are stubs first on PATH
that only record how they were called. Settings come from `keryx config get`, run with the
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

from keryx.config.files import dump_toml

SOURCE = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
STUBS = ("systemctl", "loginctl", "launchctl", "cloudflared", "ngrok", "uv")
#: A PATH entry nobody would choose, and a stranger's machine might still have.
AWKWARD_DIR = '/opt/tools|x&y%z <v2> "q"\\w'
PLACEHOLDER = re.compile(r"__[A-Z_]+__")

pytestmark = pytest.mark.skipif(BASH is None, reason="the installers are bash scripts")


@pytest.fixture
def machine(tmp_path):
    """A scratch HOME and KERYX_HOME, a copy of the scripts, and stubs that record calls."""
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
    keryx_home = tmp_path / "keryx-home"
    path = os.pathsep.join([str(stubs), AWKWARD_DIR, "/usr/bin", "/bin"])
    environment = {
        "HOME": str(home),
        "USER": "tester",
        "PATH": path,
        "KERYX_HOME": str(keryx_home),
        "KERYX_CLI": f"{sys.executable} -m keryx",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
    }
    machine = {"home": home, "repo": repo, "keryx_home": keryx_home, "env": environment,
               "calls": calls}
    configure(machine, PUBLIC_HOST="keryx.example.com", PORT=8080)
    return machine


def configure(machine, **values) -> None:
    """Write `config.toml` in the machine's KERYX_HOME, replacing what was there."""
    machine["keryx_home"].mkdir(exist_ok=True)
    (machine["keryx_home"] / "config.toml").write_text(dump_toml(values))


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


def test_logs_default_to_the_state_dir_keryx_uses(machine):
    assert lib(machine, 'printf %s "$LOGS"') == f"{machine['home']}/.local/state/keryx/logs"


def test_logs_follow_state_dir_from_the_configuration(machine):
    configure(machine, STATE_DIR="/srv/keryx state")

    assert lib(machine, 'printf %s "$LOGS"') == "/srv/keryx state/logs"


def test_logs_follow_the_xdg_state_directory(machine):
    machine["env"]["XDG_STATE_HOME"] = str(machine["home"] / "xdg state")

    assert lib(machine, 'printf %s "$LOGS"') == f"{machine['home']}/xdg state/keryx/logs"


def test_every_path_comes_from_keryx_and_none_is_reassigned(machine):
    """The resolved paths are prefixed, so an exported DATA_DIR is left exactly as it was."""
    machine["env"]["DATA_DIR"] = "/srv/data"

    out = lib(machine, 'printf "%s|%s|%s" "$DATA_DIR" "$KERYX_DIR" "$RESOLVED_CACHE_DIR"')

    assert out == f"/srv/data|/srv/data|{machine['home']}/.cache/keryx"


def test_a_env_in_the_repository_is_never_read(machine):
    configure(machine)
    (machine["repo"] / ".env").write_text("STATE_DIR=/srv/from-dotenv\n")

    assert lib(machine, 'printf %s "$LOGS"') == f"{machine['home']}/.local/state/keryx/logs"


def test_no_public_host_says_how_to_set_one(machine):
    configure(machine)

    result = subprocess.run(
        [BASH, str(machine["repo"] / "scripts" / "install-systemd.sh")],
        env=machine["env"], capture_output=True, text=True, timeout=60, check=False,
    )

    assert result.returncode == 1
    assert "keryx config set PUBLIC_HOST" in result.stderr


def test_render_inserts_values_literally(machine, tmp_path):
    template = tmp_path / "template"
    template.write_text("a=__A__\n")

    lib(machine, f'render "{template}" "{tmp_path / "out"}" "A=x|y&z\\\\w"')

    assert (tmp_path / "out").read_text() == "a=x|y&z\\w\n"


# --- systemd ------------------------------------------------------------------


def test_the_systemd_units_carry_this_shell_path_and_state_dir(machine):
    configure(
        machine, PUBLIC_HOST="keryx.example.com", STATE_DIR=str(machine["home"] / "keryx-state")
    )

    run("install-systemd.sh", machine)

    units = machine["home"] / ".config" / "systemd" / "user"
    logs = machine["home"] / "keryx-state" / "logs"
    for name, log in (("keryx", "keryx"), ("cloudflared", "cloudflared")):
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
    keryx_unit = (units / "keryx.service").read_text()
    assert f'Environment="KERYX_HOME={machine["keryx_home"]}"' in keryx_unit
    home = machine["home"]
    for name, path in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                       ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
        assert f'Environment="{name}={home}/{path}"' in keryx_unit, name
    assert logs.is_dir()
    # Created by the installer, so owner-only, as `keryx` itself would make it.
    assert (logs.parent.stat().st_mode & 0o077) == 0
    assert "systemctl --user enable --now keryx.service" in machine["calls"].read_text()


# --- launchd ------------------------------------------------------------------


def test_the_launch_agents_carry_this_shell_path_and_state_dir(machine, tmp_path):
    state_dir = tmp_path / "state <&> more"
    configure(machine, PUBLIC_HOST="keryx.example.com", STATE_DIR=str(state_dir))
    xdg_state = tmp_path / "xdg <state>"
    machine["env"]["XDG_STATE_HOME"] = str(xdg_state)

    run("install-launchd.sh", machine)

    agents = machine["home"] / "Library" / "LaunchAgents"
    logs = f"{state_dir}/logs"
    for label, log in (("dev.keryx.agent", "keryx"), ("dev.keryx.tunnel", "ngrok")):
        raw = (agents / f"{label}.plist").read_bytes()
        assert not PLACEHOLDER.search(raw.decode()), raw
        plist = plistlib.loads(raw)  # launchd refuses a plist that does not parse
        assert plist["EnvironmentVariables"]["PATH"] == machine["env"]["PATH"]
        assert plist["StandardOutPath"] == f"{logs}/{log}.out.log"
        assert plist["StandardErrorPath"] == f"{logs}/{log}.err.log"
    agent = plistlib.loads((agents / "dev.keryx.agent.plist").read_bytes())
    environment = agent["EnvironmentVariables"]
    assert environment["KERYX_HOME"] == str(machine["keryx_home"])
    assert environment["XDG_DATA_HOME"] == f"{machine['home']}/.local/share"
    assert environment["XDG_STATE_HOME"] == str(xdg_state)


def test_the_units_carry_an_xdg_directory_the_shell_moved(machine):
    """What the installer's terminal resolved is what the service resolves, whatever the
    user manager's own environment says."""
    machine["env"]["XDG_DATA_HOME"] = str(machine["home"] / "data & more")

    run("install-systemd.sh", machine)

    unit = (machine["home"] / ".config" / "systemd" / "user" / "keryx.service").read_text()
    [line] = [line for line in unit.splitlines() if line.startswith('Environment="XDG_DATA')]
    assert systemd_unquote(line.removeprefix("Environment=")) == (
        f"XDG_DATA_HOME={machine['home']}/data & more"
    )


# --- the dev loop -----------------------------------------------------------------


def test_the_dev_loop_logs_the_tunnel_beside_the_services_logs(machine):
    run("dev.sh", machine)

    logs = machine["home"] / ".local" / "state" / "keryx" / "logs"
    assert (logs / "cloudflared.log").exists()
    assert not (machine["repo"] / ".cloudflared.log").exists()
    assert "uv run keryx serve --no-wakeword" in machine["calls"].read_text()


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
    target = machine["home"] / ".claude" / "hooks" / "keryx_approval.py"
    state_dir = f"{machine['home']}/.local/state/keryx"
    assert shlex.split(commands["PermissionRequest"]) == [
        "env",
        f"KERYX_STATE_DIR={state_dir}",
        "python3",
        str(target),
    ]
    assert f"[ -e {state_dir}/approvals/PENDING ]" in shlex.split(commands["PostToolUse"])[2]


def test_the_hook_from_before_the_rename_is_replaced_not_joined(machine):
    """Two hooks would hand every prompt to the broker twice, and the old one's socket is
    gone; somebody else's hook on the same event is left alone."""
    claude = machine["home"] / ".claude"
    (claude / "hooks").mkdir(parents=True)
    old = claude / "hooks" / "jarvis_approval.py"
    old.write_text("# the old hook\n")
    theirs = {"hooks": [{"type": "command", "command": "their-own-hook"}]}
    old_command = f"env JARVIS_STATE_DIR=/x python3 {old}"
    old_group = {"hooks": [{"type": "command", "command": old_command}]}
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"PermissionRequest": [theirs, old_group], "Stop": [old_group]}})
    )

    run("install-claude-hook.sh", machine)

    settings = json.loads((claude / "settings.json").read_text())
    text = json.dumps(settings)
    assert "jarvis_approval.py" not in text and not old.exists()
    assert settings["hooks"]["PermissionRequest"][0] == theirs
    assert "keryx_approval.py" in settings["hooks"]["PermissionRequest"][1]["hooks"][0]["command"]
    assert len(settings["hooks"]["Stop"]) == 1


def test_the_hook_is_pointed_at_a_state_dir_set_in_the_configuration(machine):
    configure(machine, STATE_DIR=str(machine["home"] / "keryx state"))

    run("install-claude-hook.sh", machine)

    state_dir = f"{machine['home']}/keryx state"
    commands = hook_commands(machine)
    assert shlex.split(commands["PermissionRequest"])[:2] == [
        "env",
        f"KERYX_STATE_DIR={state_dir}",
    ]
    shell = shlex.split(commands["PostToolUse"])
    assert shell[:2] == ["sh", "-c"]
    assert f"[ -e '{state_dir}/approvals/PENDING' ]" in shell[2]
    assert f"exec {commands['PermissionRequest']};" in shell[2]


def test_the_resolve_command_still_runs_when_nothing_is_pending(machine):
    configure(machine, STATE_DIR=str(machine["home"] / "keryx state"))
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
    """Without KERYX_CLI, `uv run --project <repo> keryx config get` — the path one argument."""
    stub = tmp_path / "bin" / "uv"
    stub.write_text('#!/bin/sh\nfor arg in "$@"; do echo "[$arg]"; done\n')
    env = {key: value for key, value in machine["env"].items() if key != "KERYX_CLI"}

    result = subprocess.run(
        [BASH, "-c", f'source "{machine["repo"] / "scripts" / "lib.sh"}"\nconfig_value PORT'],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )

    assert f"[{machine['repo']}]" in result.stdout.splitlines()
    assert result.stdout.splitlines()[-3:] == ["[config]", "[get]", "[PORT]"]
