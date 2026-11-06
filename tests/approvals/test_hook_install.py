"""`keryx approvals --install-hook`: the hook copied into Claude Code and wired up.

HOME (and so `~/.claude`) is the test's own, from the conftest.
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from keryx.approvals.hook_install import hook_source, install_hook
from keryx.cli import app


def claude() -> Path:
    return Path(os.environ["HOME"]) / ".claude"


def hook_commands() -> dict[str, str]:
    settings = json.loads((claude() / "settings.json").read_text())
    return {event: groups[-1]["hooks"][0]["command"]
            for event, groups in settings["hooks"].items()}


def test_the_hook_is_always_told_where_the_state_dir_is(tmp_path):
    """Even at the default: the hook never reads the configuration, and one that guessed
    differently from the broker would leave every prompt unescalated."""
    state_dir = tmp_path / "keryx state"

    install_hook(state_dir)

    target = claude() / "hooks" / "keryx_approval.py"
    commands = hook_commands()
    assert shlex.split(commands["PermissionRequest"]) == [
        "env", f"KERYX_STATE_DIR={state_dir}", "python3", str(target)
    ]
    shell = shlex.split(commands["PostToolUse"])
    assert shell[:2] == ["sh", "-c"]
    assert f"[ -e '{state_dir}/approvals/PENDING' ]" in shell[2]
    assert f"exec {commands['PermissionRequest']};" in shell[2]
    assert target.read_bytes() == hook_source().read_bytes()
    assert os.access(target, os.X_OK)


def test_installing_again_replaces_the_hook_and_leaves_the_rest(tmp_path):
    """Two hooks would hand every prompt to the broker twice; somebody else's hook on the
    same event is left alone, and the rest of the settings are kept."""
    claude().mkdir(parents=True)
    theirs = {"hooks": [{"type": "command", "command": "their-own-hook"}]}
    before = {"model": "opus", "hooks": {"PermissionRequest": [theirs]}}
    (claude() / "settings.json").write_text(json.dumps(before))

    install_hook(tmp_path / "state")
    [backup] = (claude() / "backups").iterdir()
    assert json.loads(backup.read_text()) == before
    install_hook(tmp_path / "state")

    settings = json.loads((claude() / "settings.json").read_text())
    assert settings["model"] == "opus"
    assert settings["hooks"]["PermissionRequest"][0] == theirs
    assert len(settings["hooks"]["PermissionRequest"]) == 2
    assert len(settings["hooks"]["Stop"]) == 1


def test_uninstall_takes_out_only_the_hook(tmp_path):
    theirs = {"hooks": [{"type": "command", "command": "their-own-hook"}]}
    claude().mkdir(parents=True)
    (claude() / "settings.json").write_text(json.dumps({"hooks": {"Stop": [theirs]}}))
    install_hook(tmp_path / "state")

    install_hook(tmp_path / "state", uninstall=True)

    assert json.loads((claude() / "settings.json").read_text()) == {"hooks": {"Stop": [theirs]}}
    assert not (claude() / "hooks" / "keryx_approval.py").exists()


def test_the_resolve_command_still_runs_when_nothing_is_pending(tmp_path):
    install_hook(tmp_path / "keryx state")

    result = subprocess.run(
        hook_commands()["PostToolUse"],
        shell=True,  # noqa: S602 - it is a shell command line; this is how the CLI runs it
        input="{}", capture_output=True, text=True, timeout=30, check=False,
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_command_installs_into_the_configured_state_dir(tmp_path):
    result = CliRunner().invoke(app, ["approvals", "--install-hook"])

    assert result.exit_code == 0, result.output
    assert "KERYX_STATE_DIR=" in hook_commands()["PermissionRequest"]
    assert "keryx approvals --disable" in result.output


def test_the_service_may_not_install_the_hook(monkeypatch):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = CliRunner().invoke(app, ["approvals", "--install-hook"])

    assert result.exit_code == 1
    assert not (claude() / "settings.json").exists()


def test_a_settings_file_that_is_not_json_is_left_alone_and_said():
    claude().mkdir(parents=True)
    (claude() / "settings.json").write_text("{not json")

    result = CliRunner().invoke(app, ["approvals", "--install-hook"])

    assert result.exit_code == 1
    assert "could not change Claude Code's settings" in result.output
    assert (claude() / "settings.json").read_text() == "{not json"


def test_two_actions_at_once_are_refused():
    result = CliRunner().invoke(app, ["approvals", "--install-hook", "--disable"])

    assert result.exit_code == 2
