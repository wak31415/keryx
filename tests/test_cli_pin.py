"""`keryx pin` and `keryx pin set`: the owner sets or replaces the phone PIN from the Mac
app, through the same doors `keryx setup` uses, and never from inside the service.

The digits are the one thing these tests look for everywhere: in stdout, in stderr, in the
JSON and in every log record, on the paths that succeed and on the ones that refuse.
"""

import json
import logging
import re
import stat
import sys
from pathlib import Path

import pytest
import typer
from cli_patch import patch_cli
from typer.testing import CliRunner

import keryx
from keryx import cli, status
from keryx.cli import app
from keryx.cli import pin as cli_pin
from keryx.config import Settings, pin_file, read_enrolled_pin, write_enrolled_pin
from keryx.config.files import legacy_home
from keryx.config.pin import PIN_PATTERN
from keryx.config.settings import ConfigFileError
from keryx.pin_guard import PinGuard
from keryx.restart.service import ServiceTarget
from keryx.setup import pin as pin_flow

runner = CliRunner()
FIRST = "482915"
NEW = "739104"
STATUS_KEYS = {"set", "source", "enrolment_open", "locked", "locked_until"}


def probes(*, active: bool = False, health: int | None = None) -> status.StatusProbes:
    return status.StatusProbes(
        target=lambda settings: ServiceTarget("launchd", "dev.keryx.agent"),
        installed=lambda target: active,
        active=lambda target: active,
        health=lambda settings: health,
        running_version=lambda settings: None,
        checkout_version=lambda settings: None,
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Settings from the store alone, no service running, the data kept in scratch space."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "keryx"))
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setattr(status, "default_probes", lambda: probes())
    return Settings().config_dir


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def run(*args, input=None):
    return runner.invoke(app, ["pin", *args], input=input)


def twice(digits: str, again: str | None = None) -> str:
    return f"{digits}\n{digits if again is None else again}\n"


def assert_silent_about(result, logs, *secrets: str) -> None:
    """Not one of `secrets` anywhere a person or a log file could see it."""
    seen = [result.stdout, result.stderr, *(record.getMessage() for record in logs.records)]
    for secret in secrets:
        for text in seen:
            assert secret not in text


def pin_status() -> dict:
    result = run("--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# --- setting the first PIN -------------------------------------------------------------------


def test_a_first_pin_is_typed_twice_on_stdin_and_enrolled(home, logs):
    result = run("set", "--stdin", "--json", input=twice(FIRST))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"set": True, "replaced": False, "restart_needed": False}
    assert read_enrolled_pin(home) == FIRST
    assert stat.S_IMODE(pin_file(home).stat().st_mode) == 0o600
    assert_silent_about(result, logs, FIRST)


def test_a_first_pin_goes_through_the_one_way_door(home, monkeypatch):
    calls = []
    real = pin_flow.write_enrolled_pin

    def spy(where, digits):
        calls.append(where)
        return real(where, digits)

    monkeypatch.setattr(pin_flow, "write_enrolled_pin", spy)

    assert run("set", "--stdin", input=twice(FIRST)).exit_code == 0
    assert calls == [home]


def test_text_output_says_it_is_set_and_never_what(home, logs):
    result = run("set", "--stdin", input=twice(FIRST))

    assert result.exit_code == 0, result.output
    assert "PIN set" in result.stdout
    assert_silent_about(result, logs, FIRST)


def test_a_windows_line_ending_is_only_a_line_ending(home):
    result = run("set", "--stdin", input=f"{FIRST}\r\n{FIRST}\r\n")

    assert result.exit_code == 0, result.output
    assert read_enrolled_pin(home) == FIRST


def test_the_last_newline_may_be_left_off(home):
    assert run("set", "--stdin", input=f"{FIRST}\n{FIRST}").exit_code == 0
    assert read_enrolled_pin(home) == FIRST


# --- refusals on what was typed: exit 2, nothing written -------------------------------------


def test_two_pins_that_differ_are_refused(home, logs):
    result = run("set", "--stdin", "--json", input=twice(FIRST, NEW))

    assert result.exit_code == 2
    assert "The two PINs differ." in result.stderr
    assert result.stdout == ""
    assert not pin_file(home).exists()
    assert_silent_about(result, logs, FIRST, NEW)


def test_the_pins_are_compared_in_constant_time_before_either_is_judged(home, monkeypatch):
    compared = []
    real = pin_flow.hmac.compare_digest

    def spy(a, b):
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(pin_flow.hmac, "compare_digest", spy)
    judged = []
    monkeypatch.setattr(pin_flow, "pin_problem", lambda digits: judged.append(digits))

    result = run("set", "--stdin", input=twice("12", "34"))

    assert result.exit_code == 2
    assert compared == [(b"12", b"34")]
    assert judged == []


@pytest.mark.parametrize("digits", [
    "12345",  # too short
    "123456789",  # too long
    "48291a",
    "482 915",
    " 482915",
    "482915 ",
    "٤٨٢٩١٥",  # digits, but not ones a keypad has
    "111111",  # a repeated digit, refused at a terminal too
    "345678",  # a run
    "",
])
def test_a_pin_that_is_not_worth_having_is_refused(home, logs, digits):
    result = run("set", "--stdin", "--json", input=twice(digits))

    assert result.exit_code == 2
    assert result.stdout == ""
    assert not pin_file(home).exists()
    if digits.strip():
        assert_silent_about(result, logs, digits.strip())


@pytest.mark.parametrize("stdin", ["", f"{FIRST}\n", f"{FIRST}\n{FIRST}\n{FIRST}\n",
                                   f"{FIRST}\n\n{FIRST}\n"])
def test_stdin_carries_the_pin_exactly_twice(home, logs, stdin):
    result = run("set", "--stdin", "--json", input=stdin)

    assert result.exit_code == 2
    assert "twice" in result.stderr
    assert not pin_file(home).exists()
    assert_silent_about(result, logs, FIRST)


def test_the_pin_is_taken_from_stdin_only(home):
    result = run("set", "--json")

    assert result.exit_code == 2
    assert "--stdin" in result.stderr
    assert not pin_file(home).exists()


# --- refusals on the machine: exit 1, nothing changed -----------------------------------------


def test_an_existing_pin_is_not_replaced_without_asking(home, logs):
    write_enrolled_pin(home, FIRST)

    result = run("set", "--stdin", "--json", input=twice(NEW))

    assert result.exit_code == 1
    assert "A PIN is already set." in result.stderr
    assert read_enrolled_pin(home) == FIRST
    assert_silent_about(result, logs, FIRST, NEW)


def test_replace_puts_the_new_pin_in_place_by_an_atomic_rename(home, logs, monkeypatch):
    write_enrolled_pin(home, FIRST)
    renamed = []
    real = pin_flow.replace_pin_at_keyboard

    def spy(where, digits):
        renamed.append(where)
        real(where, digits)

    monkeypatch.setattr(pin_flow, "replace_pin_at_keyboard", spy)

    result = run("set", "--stdin", "--replace", "--json", input=twice(NEW))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"set": True, "replaced": True, "restart_needed": False}
    assert renamed == [home]
    assert read_enrolled_pin(home) == NEW
    assert stat.S_IMODE(pin_file(home).stat().st_mode) == 0o600
    assert_silent_about(result, logs, FIRST, NEW)


def test_replace_with_no_pin_there_is_a_first_pin(home):
    result = run("set", "--stdin", "--replace", "--json", input=twice(FIRST))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["replaced"] is False
    assert read_enrolled_pin(home) == FIRST


def test_a_failed_replacement_leaves_the_old_pin(home, logs, monkeypatch):
    write_enrolled_pin(home, FIRST)

    def broken(where, digits):
        raise OSError(f"disk full writing {digits}")

    monkeypatch.setattr(pin_flow, "replace_pin_at_keyboard", broken)

    result = run("set", "--stdin", "--replace", "--json", input=twice(NEW))

    assert result.exit_code == 1
    assert read_enrolled_pin(home) == FIRST
    assert_silent_about(result, logs, FIRST, NEW)


def test_a_pin_that_appears_meanwhile_is_never_overwritten(home, monkeypatch):
    def raced(where, digits):
        write_enrolled_pin(where, FIRST)
        return False

    monkeypatch.setattr(pin_flow, "write_enrolled_pin", raced)

    result = run("set", "--stdin", "--json", input=twice(NEW))

    assert result.exit_code == 1
    assert "A PIN is already set." in result.stderr
    assert read_enrolled_pin(home) == FIRST


def test_a_first_write_that_fails_part_way_is_not_called_a_pin(home, monkeypatch):
    """O_EXCL created the file and the write failed: what is left is no PIN."""
    def partial(where, digits):
        pin_file(where).parent.mkdir(parents=True, exist_ok=True)
        pin_file(where).write_text(digits[:2])
        return False

    monkeypatch.setattr(pin_flow, "write_enrolled_pin", partial)

    result = run("set", "--stdin", "--json", input=twice(NEW))

    assert result.exit_code == 1
    assert "could not be written" in result.stderr
    assert "already set" not in result.stderr


def test_a_pin_that_cannot_be_written_says_so(home, monkeypatch):
    monkeypatch.setattr(pin_flow, "write_enrolled_pin", lambda where, digits: False)

    result = run("set", "--stdin", "--json", input=twice(NEW))

    assert result.exit_code == 1
    assert "could not be written" in result.stderr


def test_an_unusable_pin_file_is_replaced_only_when_asked(home, logs):
    pin_file(home).parent.mkdir(parents=True, exist_ok=True)
    pin_file(home).write_text("oops\n")

    refused = run("set", "--stdin", "--json", input=twice(NEW))
    replaced = run("set", "--stdin", "--replace", "--json", input=twice(NEW))

    assert refused.exit_code == 1
    assert "--replace" in refused.stderr
    assert replaced.exit_code == 0, replaced.output
    assert json.loads(replaced.stdout)["replaced"] is True
    assert read_enrolled_pin(home) == NEW


@pytest.mark.parametrize("variable", ["KERYX_PIN", "JARVIS_PIN"])
def test_a_pin_in_the_environment_wins_and_is_never_shadowed(home, logs, monkeypatch, variable):
    monkeypatch.setenv(variable, FIRST)

    result = run("set", "--stdin", "--replace", "--json", input=twice(NEW))

    assert result.exit_code == 1
    assert "environment" in result.stderr
    assert variable in result.stderr  # the one actually set, so the owner knows what to remove
    assert not pin_file(home).exists()
    assert_silent_about(result, logs, FIRST, NEW)


@pytest.mark.parametrize("variable", ["KERYX_PIN", "JARVIS_PIN"])
def test_a_malformed_pin_in_the_environment_is_still_the_environments(
    home, logs, monkeypatch, variable
):
    """`serve` refuses to start on it, so a file written beside it would be a PIN nothing
    reads, reported as a success."""
    monkeypatch.setenv(variable, "12345")

    found = pin_status()
    result = run("set", "--stdin", "--replace", "--json", input=twice(NEW))

    assert found == {"set": False, "source": "environment", "enrolment_open": False,
                     "locked": False, "locked_until": None}
    assert result.exit_code == 1
    assert variable in result.stderr
    assert not pin_file(home).exists()
    assert_silent_about(result, logs, NEW)


def test_a_pin_still_in_jarvis_waits_for_the_migration(home, logs):
    legacy = legacy_home()
    legacy.mkdir(parents=True)
    pin_file(legacy).write_text(f"{FIRST}\n")

    result = run("set", "--stdin", "--replace", "--json", input=twice(NEW))

    assert result.exit_code == 1
    assert "keryx migrate" in result.stderr
    assert not pin_file(home).exists()
    assert_silent_about(result, logs, FIRST, NEW)


def test_old_files_waiting_to_move_hold_the_pin_back_too(home, tmp_path):
    (Path.cwd() / ".env").write_text("OWNER_NAME=x\n")

    result = run("set", "--stdin", "--json", input=twice(NEW))

    assert result.exit_code == 1
    assert "keryx migrate" in result.stderr
    assert not pin_file(home).exists()


# --- owner-only -------------------------------------------------------------------------------


class Untouchable:
    """A stdin that fails the test if anything reads it."""

    def __getattr__(self, name):
        raise AssertionError(f"stdin was touched ({name}) before the actor was checked")


@pytest.mark.parametrize("actor", ["KERYX_ACTOR", "JARVIS_ACTOR"])
def test_the_service_is_refused_before_stdin_is_read(home, monkeypatch, capsys, actor):
    monkeypatch.setenv(actor, "service")
    monkeypatch.setattr(sys, "stdin", Untouchable())

    with pytest.raises(typer.Exit) as refused:
        cli_pin.pin_set(stdin=True, replace=True, as_json=True)

    assert refused.value.exit_code == 1
    assert "only the owner" in capsys.readouterr().err
    assert not pin_file(home).exists()


class Terminal:
    """A stdin that is a terminal, and fails the test if anything reads it."""

    def isatty(self):
        return True

    def read(self, *args):
        raise AssertionError("a PIN was read from a terminal")


def test_a_pin_is_never_typed_into_a_terminal_that_echoes_it(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Terminal())

    with pytest.raises(typer.Exit) as refused:
        cli_pin.pin_set(stdin=True, replace=False, as_json=True)

    assert refused.value.exit_code == 2
    assert "keryx setup" in capsys.readouterr().err
    assert not pin_file(home).exists()


def test_a_crash_never_prints_the_locals_that_hold_the_digits():
    assert cli.app.pretty_exceptions_show_locals is False


def test_the_service_is_refused_through_the_command_line_too(home, monkeypatch, logs):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = run("set", "--stdin", "--json", input=twice(FIRST))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert not pin_file(home).exists()
    assert_silent_about(result, logs, FIRST)


def test_no_voice_tool_or_plugin_can_reach_the_setter():
    """The setter's names appear nowhere the voice model's tools are built."""
    source = Path(keryx.__file__).parent
    reachable = [*source.glob("tools/**/*.py"), *source.glob("plugins/**/*.py"),
                 source / "session.py", source / "server.py", source / "app.py"]
    for path in reachable:
        text = path.read_text()
        for name in ("replace_pin_at_keyboard", "write_enrolled_pin", "setup.pin"):
            assert name not in text, f"{path} names {name}"
        for names in FROM_SETUP.findall(text):
            assert "pin" not in re.findall(r"\w+", names), f"{path} imports keryx.setup.pin"


#: `from keryx.setup import …`, on one line or in parentheses, and the names it imports.
FROM_SETUP = re.compile(r"from\s+keryx\.setup\s+import\s+(\([^)]*\)|[^\n]*)")


def test_the_reachability_check_sees_every_way_of_importing_the_setter():
    for line in ("from keryx.setup import auth, pin",
                 "from keryx.setup import (\n    auth,\n    pin as flow,\n)"):
        assert "pin" in re.findall(r"\w+", FROM_SETUP.findall(line)[0])
    assert "pin" not in re.findall(r"\w+", FROM_SETUP.findall("from keryx.setup import auth")[0])


# --- restart_needed ---------------------------------------------------------------------------


@pytest.mark.parametrize("running", [probes(active=True), probes(health=0)])
def test_a_running_service_keeps_its_old_pin_until_it_restarts(home, monkeypatch, running):
    monkeypatch.setattr(status, "default_probes", lambda: running)

    result = run("set", "--stdin", "--json", input=twice(FIRST))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["restart_needed"] is True
    assert "restart" in run("set", "--stdin", "--replace", input=twice(NEW)).stdout


def test_a_probe_that_fails_is_taken_as_a_service_that_may_be_running(home, monkeypatch):
    def broken(settings):
        raise RuntimeError("launchctl is gone")

    monkeypatch.setattr(status, "default_probes", lambda: status.StatusProbes(
        target=broken, installed=lambda t: False, active=lambda t: False,
        health=broken, running_version=lambda s: None, checkout_version=lambda s: None))

    result = run("set", "--stdin", "--json", input=twice(FIRST))

    assert json.loads(result.stdout)["restart_needed"] is True


# --- status -----------------------------------------------------------------------------------


def test_a_fresh_machine_has_no_pin_and_an_open_door(home):
    assert pin_status() == {"set": False, "source": None, "enrolment_open": True,
                            "locked": False, "locked_until": None}


def test_an_enrolled_pin_is_reported_by_where_it_is_never_what(home, logs):
    write_enrolled_pin(home, FIRST)

    result = run("--json")

    assert json.loads(result.stdout) == {"set": True, "source": "file", "enrolment_open": False,
                                         "locked": False, "locked_until": None}
    assert_silent_about(result, logs, FIRST)
    assert str(len(FIRST)) not in result.stdout  # not even how many digits


def test_an_unusable_pin_file_is_no_pin_and_still_a_shut_door(home):
    pin_file(home).parent.mkdir(parents=True, exist_ok=True)
    pin_file(home).write_text("oops\n")

    assert pin_status() == {"set": False, "source": "file", "enrolment_open": False,
                            "locked": False, "locked_until": None}


def test_a_pin_in_the_environment_is_reported_as_the_environments(home, monkeypatch):
    monkeypatch.setenv("KERYX_PIN", FIRST)

    found = pin_status()

    assert (found["set"], found["source"], found["enrolment_open"]) == (True, "environment", False)


def test_a_pin_still_in_jarvis_is_set_and_shuts_the_door(home):
    legacy = legacy_home()
    legacy.mkdir(parents=True)
    pin_file(legacy).write_text(f"{FIRST}\n")

    found = pin_status()

    assert (found["set"], found["source"], found["enrolment_open"]) == (True, "jarvis", False)


@pytest.mark.parametrize("variable", ["KERYX_PIN", "JARVIS_PIN"])
def test_the_status_text_names_the_variable_actually_set(home, monkeypatch, variable):
    monkeypatch.setenv(variable, FIRST)
    usable = run().stdout
    monkeypatch.setenv(variable, "12345")
    malformed = run().stdout

    assert f"set by {variable}" in usable
    assert f"{variable} is set but isn't 6-8 digits" in malformed
    assert "fix or remove it" in malformed
    assert FIRST not in usable and "12345" not in malformed


def test_a_malformed_environment_pin_is_no_pin_even_beside_a_good_file(home, monkeypatch):
    write_enrolled_pin(home, FIRST)
    monkeypatch.setenv("KERYX_PIN", "12345")

    found = pin_status()

    assert (found["set"], found["source"], found["enrolment_open"]) == (False, "environment", False)


def test_the_status_text_names_the_state(home):
    write_enrolled_pin(home, FIRST)

    result = run()

    assert result.exit_code == 0
    assert "set" in result.stdout
    assert FIRST not in result.stdout


def test_the_status_text_says_when_there_is_none(home):
    assert "not set" in run().stdout
    pin_file(home).parent.mkdir(parents=True, exist_ok=True)
    pin_file(home).write_text("oops\n")
    assert "none usable" in run().stdout


def test_digits_no_keypad_has_are_no_pin_in_a_file_either():
    assert PIN_PATTERN.fullmatch("\u0664\u0668\u0662\u0669\u0661\u0665") is None


def lock_pin_entry() -> PinGuard:
    settings = Settings()
    guard = PinGuard.for_settings(settings)
    for _ in range(settings.pin_failure_limit):
        guard.record_failure()
    return guard


def test_a_locked_pin_is_reported_as_locked(home):
    write_enrolled_pin(home, FIRST)
    lock_pin_entry()

    found = pin_status()

    assert found["locked"] is True
    assert isinstance(found["locked_until"], str)
    assert set(found) == STATUS_KEYS


def test_setting_a_pin_never_lifts_the_lockout(home):
    write_enrolled_pin(home, FIRST)
    lock_pin_entry()
    failures = Settings().data_dir / "pin-failures.json"
    before = failures.read_bytes()
    locked = pin_status()

    result = run("set", "--stdin", "--replace", "--json", input=twice(NEW))

    assert result.exit_code == 0, result.output
    assert failures.read_bytes() == before
    assert pin_status() == locked
    assert pin_status()["locked"] is True


def test_the_status_text_says_when_entry_is_locked(home):
    write_enrolled_pin(home, FIRST)
    lock_pin_entry()

    assert "locked" in run().stdout


def test_settings_that_will_not_load_are_an_error_on_stderr(home, monkeypatch):
    def broken():
        raise ConfigFileError("config.toml will not parse")

    patch_cli(monkeypatch, "_load_settings_reporting", broken)

    for result in (run("--json"), run("set", "--stdin", "--json", input=twice(FIRST))):
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "will not parse" in result.stderr
