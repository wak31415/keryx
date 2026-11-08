"""How Keryx was installed (`keryx.install`), and the commands that depend on it:
`keryx upgrade` and `keryx service install|uninstall`."""

import importlib.metadata
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from keryx import cli
from keryx import install as install_module
from keryx.install import (
    INSTALL_COMMAND,
    Install,
    add_agent_command,
    find_uv,
    how_installed,
    private_uv,
    release,
)
from keryx.restart import version
from keryx.restart.service import ServiceTarget

runner = CliRunner()


# --- which install this is ----------------------------------------------------------------


def test_a_checkout_is_a_clone_whatever_the_environment(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "uv-receipt.toml").write_text("")

    assert how_installed(source_root=tmp_path, prefix=tmp_path / "venv") is Install.CLONE


def test_an_environment_uv_made_is_the_installers(tmp_path):
    (tmp_path / "uv-receipt.toml").write_text("")

    assert how_installed(source_root=tmp_path / "site", prefix=tmp_path) is Install.INSTALLER
    assert how_installed(source_root=tmp_path / "site", prefix=tmp_path / "other") is Install.PIP


def test_this_suite_runs_from_a_clone():
    assert how_installed() is Install.CLONE


@pytest.mark.parametrize(("kind", "command"), [
    (Install.CLONE, "uv sync --extra codex"),
    (Install.INSTALLER, INSTALL_COMMAND),
    (Install.PIP, "pip install 'keryx-voice[codex]'"),
])
def test_adding_an_agent_is_said_the_way_keryx_was_installed(kind, command):
    assert add_agent_command("codex", kind) == command


def test_the_installers_own_uv_comes_before_the_machines(monkeypatch):
    monkeypatch.setattr(install_module.shutil, "which", lambda name: "/usr/local/bin/uv")
    assert find_uv() == "/usr/local/bin/uv"

    private_uv().parent.mkdir(parents=True)
    private_uv().write_text("")
    assert find_uv() == str(private_uv())


def test_a_release_with_no_metadata_is_unknown(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)

    assert release() is None


def test_outside_a_clone_the_version_is_the_release_and_git_is_never_asked(monkeypatch):
    monkeypatch.setattr(version, "how_installed", lambda: Install.INSTALLER)
    monkeypatch.setattr(version, "release", lambda: "0.2.0")
    monkeypatch.setattr(version.subprocess, "run", pytest.fail)

    assert version.current_version() == "0.2.0"


# --- keryx upgrade ------------------------------------------------------------------------


class Commands:
    """`subprocess.run` for `keryx upgrade`: what it ran, and what each answers."""

    def __init__(self, *, upgraded=0, new="0.2.0", starts=0, restarted=0):
        self.ran: list[list[str]] = []
        self.answers = {"upgrade": upgraded, "--version": starts, "restart": restarted}
        self.new = new

    def __call__(self, argv, **_):
        self.ran.append(argv)
        word = next(word for word in ("upgrade", "--version", "restart") if word in argv)
        return subprocess.CompletedProcess(argv, self.answers[word], f"keryx {self.new}\n",
                                           "Traceback: boom")

    def words(self) -> list[str]:
        return [next(w for w in ("upgrade", "--version", "restart") if w in argv)
                for argv in self.ran]


@pytest.fixture
def upgrading(monkeypatch):
    def setup(commands, *, kind=Install.INSTALLER, uv="/data/keryx/bin/uv", service=True):
        monkeypatch.setattr(cli, "how_installed", lambda: kind)
        monkeypatch.setattr(cli, "find_uv", lambda: uv)
        monkeypatch.setattr(cli, "release", lambda: "0.1.2")
        monkeypatch.setattr(cli.subprocess, "run", commands)
        target = ServiceTarget("systemd", "keryx.service") if service else None
        monkeypatch.setattr(cli, "resolve_target", lambda settings, **_: target)
        return runner.invoke(cli.app, ["upgrade"])

    return setup


def test_upgrade_moves_the_service_onto_the_new_release(upgrading):
    commands = Commands()

    result = upgrading(commands)

    assert result.exit_code == 0, result.output
    assert commands.ran[0] == ["/data/keryx/bin/uv", "tool", "upgrade", "--quiet", "keryx-voice"]
    assert commands.ran[-1] == [sys.executable, "-m", "keryx", "restart", "--reason",
                                "upgraded to 0.2.0"]
    assert "keryx 0.1.2 → 0.2.0" in result.output
    assert "uv" not in result.output.replace("keryx", "")


def test_upgrade_with_nothing_newer_restarts_nothing(upgrading):
    commands = Commands(new="0.1.2")

    result = upgrading(commands)

    assert result.exit_code == 0
    assert "keryx 0.1.2 is the latest" in result.output
    assert "restart" not in commands.words()


def test_upgrade_without_a_service_only_installs(upgrading):
    commands = Commands()

    result = upgrading(commands, service=False)

    assert result.exit_code == 0
    assert commands.words() == ["upgrade", "--version"]


def test_a_new_release_that_will_not_start_is_never_restarted_into(upgrading):
    commands = Commands(starts=1)

    result = upgrading(commands)

    assert result.exit_code == 1
    assert "does not start" in result.output and "boom" in result.output
    assert "restart" not in commands.words()


def test_a_failed_upgrade_says_nothing_changed(upgrading):
    commands = Commands(upgraded=2)

    result = upgrading(commands)

    assert result.exit_code == 1
    assert "unchanged" in result.output
    assert commands.words() == ["upgrade"]


def test_a_restart_that_refuses_is_the_exit_code(upgrading):
    assert upgrading(Commands(restarted=1)).exit_code == 1


@pytest.mark.parametrize(("kind", "said"), [
    (Install.CLONE, "runs from a clone"),
    (Install.PIP, "pip install --upgrade keryx-voice"),
])
def test_upgrade_outside_the_installer_says_what_to_do_and_does_nothing(upgrading, kind, said):
    commands = Commands()

    result = upgrading(commands, kind=kind)

    assert result.exit_code == 1
    assert said in result.output
    assert commands.ran == []


def test_upgrade_without_the_installers_uv_points_at_the_installer(upgrading):
    result = upgrading(Commands(), uv=None)

    assert result.exit_code == 1
    assert INSTALL_COMMAND in result.output


def test_a_task_upgrades_and_asks_for_the_restart_instead_of_restarting(upgrading, monkeypatch):
    """On a call the task runs inside the service, where a restart would kill it."""
    monkeypatch.setenv("KERYX_ACTOR", "service")
    commands = Commands()

    result = upgrading(commands)

    assert result.exit_code == 0, result.output
    assert commands.words() == ["upgrade", "--version"]
    assert "`RESTART_REQUIRED: upgraded Keryx to 0.2.0` above your SPOKEN_SUMMARY:" in (
        result.output
    )


@pytest.mark.parametrize("kind", [Install.CLONE, Install.PIP])
def test_a_task_outside_the_installer_is_told_to_ask_for_the_restart(upgrading, monkeypatch,
                                                                      kind):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = upgrading(Commands(), kind=kind)

    assert result.exit_code == 1
    assert "then a `RESTART_REQUIRED:` line" in result.output
    assert "keryx restart" not in result.output


# --- keryx service ------------------------------------------------------------------------


def test_service_install_says_what_is_missing(monkeypatch):
    result = runner.invoke(cli.app, ["service", "install"])

    assert result.exit_code == 1
    assert "keryx config set PUBLIC_HOST" in result.output


def test_service_install_and_uninstall_reach_the_installer(monkeypatch):
    done = []
    monkeypatch.setattr(cli.service_units, "install", lambda settings, **_: done.append("in"))
    monkeypatch.setattr(cli.service_units, "uninstall", lambda **_: done.append("out"))

    assert runner.invoke(cli.app, ["service", "install"]).exit_code == 0
    assert runner.invoke(cli.app, ["service", "uninstall"]).exit_code == 0
    assert done == ["in", "out"]
