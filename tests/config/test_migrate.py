"""`jarvis migrate`: an install as it is today — `~/.jarvis`, a `tasks.db` full of absolute
paths, and a `.env` in the checkout holding the PIN — moved to the XDG directories.

HOME and every XDG variable are the test's own (the conftest), so the `~/.jarvis` here is
one this file builds; the developer's real one never takes part.
"""

import errno
import os
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis import cli
from jarvis.config import Settings
from jarvis.config import migrate as migration
from jarvis.config.files import dump_toml, read_toml, secure_dir
from jarvis.config.migrate import MigrationError, Service, make_plan, rewrite_task_paths, run
from jarvis.restart.service import ServiceTarget
from jarvis.tasks.models import Task, TaskKind, TaskStatus
from jarvis.tasks.store import TaskStore

TODAY = date(2026, 9, 28)
PIN = "482915"
CLIENT = '{"installed": {"client_id": "id", "client_secret": "shh"}}'
runner = CliRunner()


@dataclass
class Machine:
    home: Path
    legacy: Path
    repo: Path

    @property
    def config(self) -> Path:
        return self.home / ".config" / "jarvis"

    @property
    def data(self) -> Path:
        return self.home / ".local" / "share" / "jarvis"

    @property
    def state(self) -> Path:
        return self.home / ".local" / "state" / "jarvis"

    def plan(self):
        return make_plan([self.repo])


def add_task(store: TaskStore, **fields) -> None:
    store._create_sync(Task(id=None, kind=TaskKind.AGENT, description="d", **fields))


@pytest.fixture
def machine(tmp_path, monkeypatch) -> Machine:
    """`~/.jarvis` as a real install left it, and a checkout with a `.env` in it."""
    monkeypatch.delenv("JARVIS_HOME")  # the default, ~/.config/jarvis, is what is under test
    home = Path.home()
    legacy = secure_dir(home / ".jarvis")
    store = TaskStore(legacy / "tasks.db")
    add_task(store, status=TaskStatus.DONE, claude_session_id="s1",
             report_path=str(legacy / "tasks" / "1.md"))
    add_task(store, status=TaskStatus.QUEUED, claude_session_id="s2",
             cwd=str(legacy / "workspace"))
    add_task(store, status=TaskStatus.DONE, agent="codex", claude_session_id="t3",
             report_path=str(legacy / "tasks" / "3.md"))
    add_task(store, status=TaskStatus.DONE, claude_session_id="s4", cwd="/projects/orchard",
             report_path=str(legacy / "tasks" / "4.md"))
    add_task(store, status=TaskStatus.DONE, claude_session_id="s5", cwd=str(legacy),
             internal=True)
    store._close_sync()
    for directory in ("tasks", "calls", "workspace", "google", "approvals"):
        secure_dir(legacy / directory)
    (legacy / "tasks" / "1.md").write_text("the report")
    (legacy / "calls" / "abc.log").write_text("user: hello")
    (legacy / "memory.md").write_text("# What Jarvis knows\n")
    (legacy / "google" / "creds.json").write_text("{}")
    (legacy / "approvals" / "audit.jsonl").write_text("{}\n")
    (legacy / "approvals.sock").touch()
    (legacy / "logs").mkdir(mode=0o775)
    (legacy / "logs").chmod(0o775)
    (legacy / "logs" / "jarvis.log").write_text("INFO started\n")
    (legacy / "logs" / "jarvis.log").chmod(0o664)
    for name in ("pin-failures.json", "report_secret", "gmail_token.json", "running-version",
                 "startup-log-marks.json"):
        (legacy / name).write_text(name)
    for leftover in ("restart-after-task7.sh", "memory.md.real"):
        (legacy / leftover).write_text("not Jarvis's to move")

    repo = tmp_path / "repo"
    (repo / ".secrets").mkdir(parents=True)
    (repo / ".env").write_text(f"OPENAI_API_KEY=sk-test\nJARVIS_PIN={PIN}\nOPENAI_VOICE=marin\n")
    (repo / ".secrets" / "client_secret.json").write_text(CLIENT)
    (repo / ".secrets" / "token.json").write_text("{}")
    return Machine(home, legacy, repo)


def migrate(machine: Machine, **kwargs):
    return run(machine.plan(), service=kwargs.pop("service", None),
               fix_permissions=kwargs.pop("fix_permissions", lambda: []), today=TODAY, **kwargs)


def rows(database: Path) -> dict[int, sqlite3.Row]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return {row["id"]: row for row in connection.execute("SELECT * FROM tasks")}
    finally:
        connection.close()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def listing(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


# --- the plan --------------------------------------------------------------------------


def test_the_plan_reads_and_never_writes(machine):
    before = listing(machine.home), listing(machine.repo)

    plan = machine.plan()

    assert not plan.conflicts and not plan.empty
    assert (listing(machine.home), listing(machine.repo)) == before
    assert plan.data_dir == machine.data and plan.config_dir == machine.config
    moves = {step.source.name: step.destination for step in plan.steps}
    assert moves["tasks.db"] == machine.data / "tasks.db"
    assert moves["pin-failures.json"] == machine.data / "pin-failures.json"
    assert moves["logs"] == machine.state / "logs"
    assert moves["approvals"] == machine.state / "approvals"
    assert moves["client_secret.json"] == machine.config / "google_client_secret.json"
    assert moves["approvals.sock"] is None  # removed, not moved
    assert "restart-after-task7.sh" not in moves  # not Jarvis's: stays where it is
    text = "\n".join(plan.describe())
    assert "~/.jarvis/tasks.db  →  ~/.local/share/jarvis/tasks.db" in text
    assert ".env: imported" in text


def test_a_machine_with_nothing_legacy_has_nothing_to_do(machine, tmp_path):
    assert make_plan([tmp_path / "elsewhere"]).empty is False  # ~/.jarvis still is
    migrate(machine)

    assert machine.plan().empty
    assert not machine.plan().conflicts


# --- the run ---------------------------------------------------------------------------


def test_everything_lands_where_it_belongs(machine):
    report = migrate(machine)

    assert (machine.data / "tasks" / "1.md").read_text() == "the report"
    assert (machine.data / "calls" / "abc.log").is_file()
    for name in ("memory.md", "workspace", "google", "pin-failures.json", "report_secret",
                 "gmail_token.json", "tasks.db"):
        assert (machine.data / name).exists(), name
    for name in ("logs/jarvis.log", "approvals/audit.jsonl", "running-version",
                 "startup-log-marks.json"):
        assert (machine.state / name).exists(), name
    assert not (machine.state / "approvals.sock").exists()
    assert (machine.config / "pin").read_text().strip() == PIN
    assert (machine.config / "google_client_secret.json").read_text() == CLIENT
    assert read_toml(machine.config / "config.toml")["OPENAI_VOICE"] == "marin"
    assert read_toml(machine.config / "secrets.toml")["OPENAI_API_KEY"] == "sk-test"
    assert report.imported and report.moved


def test_what_is_not_jarviss_is_kept_aside_and_listed(machine):
    report = migrate(machine)

    retired = machine.home / ".jarvis.migrated-2026-09-28"
    assert not machine.legacy.exists()
    assert report.retired_to == retired
    assert sorted(path.name for path in retired.iterdir()) == [
        "memory.md.real", "restart-after-task7.sh"
    ]
    assert not (machine.repo / ".env").exists()
    assert (machine.repo / ".env.imported-2026-09-28").is_file()
    assert not (machine.repo / ".secrets" / "client_secret.json").exists()
    leftovers = "\n".join(report.leftovers)
    assert "token.json" in leftovers and ".env.imported-2026-09-28" in leftovers
    assert "restart-after-task7.sh" in leftovers


def test_the_database_follows_the_data(machine):
    report = migrate(machine)

    found = rows(machine.data / "tasks.db")
    assert found[1]["report_path"] == str(machine.data / "tasks" / "1.md")
    assert found[2]["cwd"] == str(machine.data / "workspace")
    assert found[4]["cwd"] == "/projects/orchard"  # a project: nothing of Jarvis's
    assert found[4]["report_path"] == str(machine.data / "tasks" / "4.md")
    assert found[5]["cwd"] == str(machine.data)
    assert report.rewritten == 5


def test_claude_sessions_from_the_old_workspace_are_let_go(machine):
    """Claude keeps a session under the directory it ran in, which has moved: a follow-up
    must start afresh rather than resume something that is not there."""
    report = migrate(machine)

    found = rows(machine.data / "tasks.db")
    assert [found[i]["claude_session_id"] for i in (1, 2, 5)] == [None, None, None]
    assert found[3]["claude_session_id"] == "t3"  # Codex: its sessions moved with codex/
    assert found[4]["claude_session_id"] == "s4"  # ran in a project, which has not moved
    assert [task_id for task_id, *_ in report.lost_sessions] == [2, 1, 5]  # unfinished first


def test_the_result_is_what_settings_read(machine):
    migrate(machine)

    settings = Settings(_env_file=None)

    assert settings.pin == PIN and settings.pin_source == "enrolled"
    assert settings.data_dir == machine.data
    assert settings.google_oauth_client() == ("id", "shh")
    assert settings.openai_voice == "marin"


def test_everything_moved_is_owner_only(machine):
    report = migrate(machine)

    assert mode(machine.state / "logs") == 0o700
    assert mode(machine.state / "logs" / "jarvis.log") == 0o600
    for directory in (machine.config, machine.data, machine.state):
        assert mode(directory) == 0o700
    assert any("logs" in line for line in report.tightened)


def test_doctors_fix_runs_last(machine):
    seen = []

    migrate(machine, fix_permissions=lambda: seen.append(machine.legacy.exists()) or ["fixed"])

    assert seen == [True]  # before the old directory is renamed aside: it tightens the new


def test_a_second_run_changes_nothing(machine):
    migrate(machine)
    database = (machine.data / "tasks.db").read_bytes()

    assert machine.plan().empty
    assert rewrite_task_paths(machine.data / "tasks.db", [machine.legacy]) == (0, [])
    assert (machine.data / "tasks.db").read_bytes() == database


def test_an_interrupted_run_finishes_on_the_next(machine, monkeypatch):
    real, done = migration.move, []

    def breaks_half_way(source, destination):
        if len(done) == 4:
            raise OSError(errno.EIO, "I/O error", str(source))
        done.append(source)
        real(source, destination)

    monkeypatch.setattr(migration, "move", breaks_half_way)
    with pytest.raises(MigrationError, match="run `jarvis migrate` again"):
        migrate(machine)
    assert machine.legacy.is_dir() and (machine.repo / ".env").is_file()

    monkeypatch.setattr(migration, "move", real)
    plan = machine.plan()
    assert not plan.conflicts
    assert len(plan.steps) > 0 and all(step.source not in done for step in plan.steps)
    migrate(machine)

    assert not machine.legacy.exists()
    assert (machine.config / "pin").read_text().strip() == PIN
    assert rows(machine.data / "tasks.db")[2]["cwd"] == str(machine.data / "workspace")


def test_empty_directories_made_by_an_earlier_command_are_not_in_the_way(machine):
    """`ensure_dirs` runs on any command, so `jarvis doctor` before the migration leaves
    empty `tasks/`, `calls/`, `logs/` and `approvals/` where things are about to go."""
    Settings(_env_file=None, openai_api_key="x").ensure_dirs()

    plan = machine.plan()

    assert not plan.conflicts
    migrate(machine)
    assert (machine.data / "calls" / "abc.log").is_file()


def test_a_real_conflict_stops_everything_before_it_starts(machine):
    secure_dir(machine.data)
    (machine.data / "memory.md").write_text("a different memory")
    before = listing(machine.legacy)

    plan = machine.plan()

    assert any("memory.md" in conflict for conflict in plan.conflicts)
    with pytest.raises(MigrationError, match="conflicts"):
        run(plan, service=None, fix_permissions=lambda: [])
    assert listing(machine.legacy) == before
    assert (machine.repo / ".env").is_file()


def test_the_same_file_at_both_ends_is_no_conflict(machine):
    secure_dir(machine.config)
    (machine.config / "google_client_secret.json").write_text(CLIENT)

    plan = machine.plan()

    assert not plan.conflicts
    migrate(machine)
    assert not (machine.repo / ".secrets" / "client_secret.json").exists()


def test_a_conflicting_pin_is_refused_before_anything_moves(machine):
    """The `.env` PIN has been the one in use; switching silently would lock them out."""
    (machine.legacy / "pin").write_text("111357\n")

    plan = machine.plan()

    assert any("JARVIS_PIN" in conflict for conflict in plan.conflicts)
    assert all(PIN not in conflict and "111357" not in conflict for conflict in plan.conflicts)


def test_the_same_pin_in_both_places_moves_once(machine):
    (machine.legacy / "pin").write_text(f"{PIN}\n")

    migrate(machine)

    assert (machine.config / "pin").read_text().strip() == PIN


def test_an_env_that_would_not_import_is_refused_up_front(machine):
    (machine.repo / ".env").write_text("PORT=eighty\n")

    assert any("PORT" in conflict for conflict in machine.plan().conflicts)


def test_a_data_dir_naming_the_old_directory_is_unset(machine):
    (machine.legacy / "config.toml").write_text(dump_toml({"DATA_DIR": "~/.jarvis"}))

    plan = machine.plan()
    assert plan.unset_data_dir and plan.data_dir == machine.data
    migrate(machine)

    assert "DATA_DIR" not in read_toml(machine.config / "config.toml")
    assert Settings(_env_file=None).data_dir == machine.data


def test_a_data_dir_kept_elsewhere_keeps_its_data_and_gives_up_the_rest(machine, tmp_path):
    elsewhere = secure_dir(tmp_path / "srv" / "jarvis")
    (elsewhere / "calls").mkdir()
    (elsewhere / "pin").write_text(f"{PIN}\n")
    (elsewhere / "restart.json").write_text("{}")
    for name in ("tasks.db", "calls", "memory.md", "tasks", "workspace", "google",
                 "pin-failures.json", "report_secret", "gmail_token.json"):
        path = machine.legacy / name
        path.rename(elsewhere / name) if path.exists() else None
    (machine.legacy / "config.toml").write_text(dump_toml({"DATA_DIR": str(elsewhere)}))

    plan = machine.plan()
    migrate(machine)

    assert plan.data_dir == elsewhere and not plan.unset_data_dir
    assert (elsewhere / "tasks.db").is_file() and (elsewhere / "calls").is_dir()
    assert not (elsewhere / "pin").exists() and (machine.config / "pin").is_file()
    assert (machine.state / "restart.json").is_file() and not (elsewhere / "restart.json").exists()
    assert read_toml(machine.config / "config.toml")["DATA_DIR"] == str(elsewhere)


def test_a_legacy_directory_still_in_use_is_left_alone(machine, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(machine.legacy))

    plan = machine.plan()

    assert any("DATA_DIR" in conflict for conflict in plan.conflicts)


def test_a_move_across_file_systems_copies_then_removes(tmp_path, monkeypatch):
    real_rename = os.rename

    def cross_device(source, destination):
        if Path(source).parent.name == "from":
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(source, destination)

    monkeypatch.setattr(os, "rename", cross_device)
    source = tmp_path / "from"
    (source / "tree" / "deep").mkdir(parents=True)
    (source / "tree" / "deep" / "file").write_text("contents")
    (source / "tree" / "link").symlink_to("deep/file")
    (source / "single").write_text("one")
    (tmp_path / "to" / ".tree.migrating").mkdir(parents=True)  # a copy that never finished

    migration.move(source / "tree", tmp_path / "to" / "tree")
    migration.move(source / "single", tmp_path / "to" / "single")

    assert (tmp_path / "to" / "tree" / "deep" / "file").read_text() == "contents"
    assert (tmp_path / "to" / "tree" / "link").is_symlink()
    assert (tmp_path / "to" / "single").read_text() == "one"
    assert not (source / "tree").exists() and not (source / "single").exists()
    assert not (tmp_path / "to" / ".tree.migrating").exists()


def test_any_other_move_failure_is_raised(tmp_path, monkeypatch):
    def refused(source, destination):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "rename", refused)
    (tmp_path / "a").write_text("x")

    with pytest.raises(PermissionError):
        migration.move(tmp_path / "a", tmp_path / "b" / "a")


def test_path_rewriting_compares_literally_never_as_a_pattern(tmp_path):
    """`LIKE '/srv/a_b%'` would match `/srv/axb/…`; `substr` matches only itself."""
    database = tmp_path / "tasks.db"
    store = TaskStore(database)
    add_task(store, cwd="/srv/axb/workspace", report_path="/srv/a_b/tasks/1.md")
    add_task(store, cwd="/srv/a_b-sibling", claude_session_id="kept")
    store._close_sync()

    changed, lost = rewrite_task_paths(database, [Path("/srv/a_b")])

    found = rows(database)
    assert found[1]["cwd"] == "/srv/axb/workspace"
    assert found[1]["report_path"] == f"{tmp_path}/tasks/1.md"
    assert found[2]["cwd"] == "/srv/a_b-sibling" and found[2]["claude_session_id"] == "kept"
    assert (changed, lost) == (1, [])


def test_a_database_that_predates_the_columns_is_left_alone(tmp_path):
    database = tmp_path / "tasks.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")

    assert rewrite_task_paths(database, [Path("/old")]) == (0, [])
    assert rewrite_task_paths(tmp_path / "missing.db", [Path("/old")]) == (0, [])


def test_two_entries_that_would_become_one_file_are_a_conflict(machine):
    """setup's copy of the client file, and the one in `.secrets/`: which is real is theirs
    to say."""
    (machine.legacy / "google_client_secret.json").write_text(CLIENT.replace("shh", "other"))

    plan = machine.plan()

    assert any("would both become" in conflict for conflict in plan.conflicts)


def test_a_legacy_store_that_does_not_parse_is_still_moved(machine):
    (machine.legacy / "config.toml").write_text("this is [ not toml")

    plan = machine.plan()

    assert not plan.conflicts
    assert machine.legacy / "config.toml" in [step.source for step in plan.steps]


def test_an_entry_gone_by_the_time_it_moves_is_skipped(machine):
    """`tasks.db-wal`, which the checkpoint's last close takes away with it."""
    (machine.legacy / "tasks.db-wal").write_text("")
    plan = machine.plan()
    (machine.legacy / "tasks.db-wal").unlink()

    run(plan, service=None, fix_permissions=lambda: [], today=TODAY)

    assert not (machine.data / "tasks.db-wal").exists()


def test_an_import_that_fails_after_all_is_reported(machine, monkeypatch):
    def refuses(self, path, **kwargs):
        raise migration.ConfigError("no")

    monkeypatch.setattr(migration.ConfigStore, "import_env", refuses)

    with pytest.raises(MigrationError, match="was not imported"):
        migrate(machine)


def test_a_data_dir_that_cannot_be_unset_is_reported(machine, monkeypatch):
    (machine.legacy / "config.toml").write_text(dump_toml({"DATA_DIR": "~/.jarvis"}))

    def refuses(self, keys, **kwargs):
        raise migration.ConfigError("no")

    monkeypatch.setattr(migration.ConfigStore, "unset", refuses)

    with pytest.raises(MigrationError, match="could not be unset"):
        migrate(machine)


def test_an_old_directory_emptied_by_the_move_is_removed_not_renamed(machine):
    for leftover in ("restart-after-task7.sh", "memory.md.real"):
        (machine.legacy / leftover).unlink()

    report = migrate(machine)

    assert report.retired_to is None
    assert not machine.legacy.exists()
    assert not list(machine.home.glob(".jarvis.migrated-*"))


def test_an_earlier_retirement_on_the_same_day_is_not_overwritten(machine):
    (machine.home / ".jarvis.migrated-2026-09-28").mkdir()

    report = migrate(machine)

    assert report.retired_to == machine.home / ".jarvis.migrated-2026-09-28-2"


def test_tightening_skips_what_it_cannot_and_need_not_touch(machine, tmp_path):
    plan = machine.plan()
    (machine.legacy / "logs" / "link").symlink_to(tmp_path / "nowhere")

    run(plan, service=None, fix_permissions=lambda: [], today=TODAY)

    assert (machine.state / "logs" / "link").is_symlink()


# --- the service -----------------------------------------------------------------------


class FakeManager:
    """systemd, as far as `Service` can tell: records every command, and what was on disk
    when it ran."""

    def __init__(self, machine: Machine, *, stops: bool = True) -> None:
        self.machine, self.stops = machine, stops
        self.running = True
        self.log: list[tuple[str, bool]] = []

    def __call__(self, argv, **kwargs):
        verb = argv[2]
        self.log.append((verb, (self.machine.legacy / "tasks.db").exists()))
        if verb == "stop" and self.stops:
            self.running = False
        elif verb == "start":
            self.running = True
        out = "active\n" if self.running else "inactive\n"
        return subprocess.CompletedProcess(argv, 0 if self.running else 3, out, "")


def test_the_service_is_stopped_before_anything_moves_and_started_after(machine):
    manager = FakeManager(machine)
    service = Service(ServiceTarget("systemd", "jarvis.service"), run=manager,
                      sleep=lambda _: None)
    rerendered = []

    report = migrate(machine, service=service,
                     rerender=lambda: rerendered.append(manager.running) or ["re-rendered"])

    verbs = [verb for verb, _ in manager.log]
    assert verbs[:2] == ["is-active", "stop"]
    assert all(before for verb, before in manager.log if verb == "stop")  # nothing moved yet
    assert ("start", False) in manager.log  # and started once everything had
    assert rerendered == [False] and report.rerendered == ["re-rendered"]
    assert report.started is True


def test_a_service_that_will_not_stop_moves_nothing(machine):
    manager = FakeManager(machine, stops=False)
    service = Service(ServiceTarget("systemd", "jarvis.service"), run=manager,
                      sleep=lambda _: None)
    before = listing(machine.legacy)

    with pytest.raises(MigrationError, match="did not stop"):
        migrate(machine, service=service)

    assert listing(machine.legacy) == before


def test_a_stopped_service_is_not_started_by_the_migration(machine):
    manager = FakeManager(machine)
    manager.running = False
    service = Service(ServiceTarget("systemd", "jarvis.service"), run=manager)

    report = migrate(machine, service=service)

    assert "stop" not in [verb for verb, _ in manager.log]
    assert "start" not in [verb for verb, _ in manager.log]
    assert report.started is None


def test_a_service_the_installer_already_started_is_not_started_twice(machine):
    manager = FakeManager(machine)
    service = Service(ServiceTarget("systemd", "jarvis.service"), run=manager,
                      sleep=lambda _: None)

    def installer_starts_it():
        manager.running = True
        return ["install-systemd.sh: done"]

    report = migrate(machine, service=service, rerender=installer_starts_it)

    assert "start" not in [verb for verb, _ in manager.log]
    assert report.started is True


@pytest.mark.parametrize(
    ("manager", "stdout", "code", "running"),
    [
        ("systemd", "active\n", 0, True),
        ("systemd", "deactivating\n", 0, True),  # not stopped yet: nothing may move
        ("systemd", "inactive\n", 3, False),
        ("systemd", "failed\n", 3, False),
        ("launchd", "state = running\n", 0, True),
        ("launchd", "state = not running\n", 0, False),
        ("launchd", "", 113, False),  # not loaded at all
    ],
)
def test_whether_the_service_is_running(manager, stdout, code, running):
    from jarvis.restart.service import is_active

    target = ServiceTarget(manager, "unit")

    def answer(argv, **kwargs):
        return subprocess.CompletedProcess(argv, code, stdout, "")

    assert is_active(target, run=answer) is running


def test_a_manager_that_cannot_be_asked_counts_as_running():
    from jarvis.restart.service import is_active

    def hangs(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    assert is_active(ServiceTarget("systemd", "jarvis.service"), run=hangs) is True


def test_launchd_is_booted_out_and_bootstrapped_from_its_plist():
    target = ServiceTarget("launchd", "dev.jarvis.agent")

    assert target.stop_command()[:2] == ["launchctl", "bootout"]
    assert target.start_command()[:2] == ["launchctl", "bootstrap"]
    assert target.start_command()[-1].endswith("Library/LaunchAgents/dev.jarvis.agent.plist")


# --- the command -----------------------------------------------------------------------


@pytest.fixture
def command(machine, monkeypatch):
    """`jarvis migrate` run from the machine's checkout, with nothing real to re-render."""
    monkeypatch.chdir(machine.repo)
    monkeypatch.setattr(cli, "repo_root", lambda: machine.repo)
    ran: list[list[str]] = []
    monkeypatch.setattr(cli, "run_command", lambda argv: ran.append(argv) or 0)
    monkeypatch.setattr(cli, "health_probe", lambda settings: None)
    return ran


def test_dry_run_prints_the_plan_and_touches_nothing(machine, command):
    before = listing(machine.home), listing(machine.repo)

    result = runner.invoke(cli.app, ["migrate", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "~/.jarvis/tasks.db  →  ~/.local/share/jarvis/tasks.db" in result.output
    assert (listing(machine.home), listing(machine.repo)) == before


def test_the_command_migrates_and_says_what_is_left(machine, command):
    claude = machine.home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text('{"hooks": {"x": "python3 jarvis_approval.py"}}')

    result = runner.invoke(cli.app, ["migrate", "--yes"])

    assert result.exit_code == 0, result.output
    assert (machine.config / "pin").is_file()
    assert "sessions are gone" in result.output and "#2" in result.output
    assert "token.json" in result.output
    assert [Path(argv[0]).name for argv in command] == ["install-claude-hook.sh"]
    assert PIN not in result.output
    assert "nothing to migrate" in runner.invoke(cli.app, ["migrate"]).output


def test_the_command_rerenders_an_installed_service(machine, command, monkeypatch):
    manager = FakeManager(machine)
    monkeypatch.setattr(cli, "resolve_target",
                        lambda settings, **_: ServiceTarget("systemd", "jarvis.service"))
    monkeypatch.setattr(cli, "MigratingService",
                        lambda target: Service(target, run=manager, sleep=lambda _: None))

    result = runner.invoke(cli.app, ["migrate", "--yes"])

    assert result.exit_code == 0, result.output
    assert [Path(argv[0]).name for argv in command] == ["install-systemd.sh"]
    assert "the service is running again" in result.output


def test_the_command_summarises_a_long_list_and_a_service_that_did_not_come_back(
    machine, command, monkeypatch
):
    report = migration.Report(
        moved=["one"],
        rewritten=3,
        lost_sessions=[(n, "done", f"task {n}") for n in range(1, 14)],
        started=False,
    )
    monkeypatch.setattr(cli, "run_migration", lambda plan, **kwargs: report)

    result = runner.invoke(cli.app, ["migrate", "--yes"])

    assert "moved 1 entry" in result.output and "3 path(s) rewritten" in result.output
    assert "#10" in result.output and "#11" not in result.output
    assert "… and 3 more" in result.output
    assert "did not start" in result.output


def test_an_installer_that_fails_says_to_run_it_again(machine, command, monkeypatch):
    monkeypatch.setattr(cli, "run_command", lambda argv: 1)
    claude = machine.home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("jarvis_approval.py")

    assert cli._rerender(ServiceTarget("systemd", "jarvis.service")) == [
        "install-systemd.sh: exited 1, run it again",
        "approval hook: exited 1, run it again",
    ]


def test_the_command_will_not_cut_a_live_call_off(machine, command, monkeypatch):
    monkeypatch.setattr(cli, "resolve_target",
                        lambda settings, **_: ServiceTarget("systemd", "jarvis.service"))
    monkeypatch.setattr(cli, "health_probe", lambda settings: 1)

    result = runner.invoke(cli.app, ["migrate", "--yes"])

    assert result.exit_code == 1 and "live call" in result.output
    assert machine.legacy.is_dir()


def test_the_command_lists_conflicts_and_stops(machine, command):
    (machine.legacy / "pin").write_text("111357\n")

    result = runner.invoke(cli.app, ["migrate", "--yes"])

    assert result.exit_code == 1
    assert "settled" in result.output and machine.legacy.is_dir()


def test_without_a_terminal_it_wants_yes(machine, command):
    result = runner.invoke(cli.app, ["migrate"])

    assert result.exit_code == 2 and "--yes" in result.output
    assert machine.legacy.is_dir()


def test_a_failure_part_way_is_reported(machine, command, monkeypatch):
    def fails(*args, **kwargs):
        raise MigrationError("stopped at somewhere")

    monkeypatch.setattr(cli, "run_migration", fails)

    result = runner.invoke(cli.app, ["migrate", "--yes"])

    assert result.exit_code == 1 and "stopped at somewhere" in result.output


def test_the_service_may_not_migrate(machine, command, monkeypatch):
    monkeypatch.setenv("JARVIS_ACTOR", "service")

    result = runner.invoke(cli.app, ["migrate", "--yes"])

    assert result.exit_code == 1 and "only the owner" in result.output
    assert machine.legacy.is_dir()
