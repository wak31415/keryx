"""Tests for `keryx status`: one document for the Mac app's menu bar, schema 1."""

import asyncio
import dataclasses
import json
from datetime import UTC, datetime

import pytest

from keryx import status
from keryx.app import TASK_DB_NAME
from keryx.approvals.broker import KILL_SWITCH_NAME, STATE_DIR_NAME
from keryx.config import Settings
from keryx.continuity.memory import memory_path
from keryx.restart.service import ServiceTarget
from keryx.restart.store import RECORD_NAME, RestartRecord, RestartStore
from keryx.status import StatusProbes, build_status, failed_status, format_status
from keryx.tasks.models import Task, TaskKind, TaskStatus
from keryx.tasks.store import TaskStore

TARGET = ServiceTarget("launchd", "dev.keryx.agent")

FAKE = StatusProbes(
    target=lambda settings: TARGET,
    installed=lambda target: True,
    active=lambda target: True,
    health=lambda settings: 0,
    running_version=lambda settings: "v0.1.0-3-g1ad6420",
    checkout_version=lambda settings: "v0.1.0-5-gabc1234",
)

#: The Addendum's table, written out here rather than read from the module, so the
#: contract the Swift decoder depends on is pinned by the test and not by the code.
FAILED_DEFAULTS = {
    "storage": {"migration_required": False},
    "service": {"manager": None, "unit": None, "installed": False, "active": False},
    "health": {"reachable": False, "live_sessions": None},
    "version": {"running": None, "checkout": None},
    "restart": {"state": None, "requested_at": None, "failure": None},
    "assistant": {"name": ""},
    "agents": {"default": "", "enabled": []},
    "tasks": {"running": 0, "queued": 0, "unreported": 0},
    "approvals": {"escalation_enabled": True},
    "paths": {
        "config": "", "data": "", "state": "", "cache": "", "logs": "", "memory": "",
    },
}
PARTS = tuple(FAILED_DEFAULTS)


def boom(*args, **kwargs):
    raise OSError("boom")


@pytest.fixture
def settings_with_old_jarvis_dir(settings, tmp_path):
    """An install from before the rename: Jarvis's data directory still holds a file."""
    old = tmp_path / "home" / ".local" / "share" / "jarvis"
    old.mkdir(parents=True)
    (old / "memory.md").write_text("# Memory\n")
    assert settings.storage_refusal()
    return settings


def seed(settings: Settings, *tasks: Task) -> None:
    settings.ensure_dirs()

    async def store_them() -> None:
        store = TaskStore(settings.data_dir / TASK_DB_NAME)
        for task in tasks:
            await store.create(task)
        await store.close()

    asyncio.run(store_them())


def task(status_: TaskStatus, **overrides) -> Task:
    return Task(id=None, kind=TaskKind.AGENT, description="work", status=status_, **overrides)


# --- the whole document ------------------------------------------------------------------


def test_a_healthy_machine_reads_as_the_contract_says(settings):
    report = build_status(settings, probes=FAKE)

    assert report["schema"] == 1
    assert list(report) == ["schema", *PARTS]
    assert report["storage"] == {"ok": True, "error": None, "migration_required": False}
    assert report["service"] == {
        "ok": True, "error": None, "manager": "launchd", "unit": "dev.keryx.agent",
        "installed": True, "active": True,
    }
    assert report["health"] == {"ok": True, "error": None, "reachable": True, "live_sessions": 0}
    assert report["version"] == {
        "ok": True, "error": None,
        "running": "v0.1.0-3-g1ad6420", "checkout": "v0.1.0-5-gabc1234",
    }
    assert report["restart"] == {
        "ok": True, "error": None, "state": None, "requested_at": None, "failure": None,
    }
    assert report["assistant"] == {"ok": True, "error": None, "name": "Lyra"}
    assert report["agents"] == {
        "ok": True, "error": None, "default": "claude", "enabled": ["claude"],
    }
    assert report["tasks"] == {
        "ok": True, "error": None, "running": 0, "queued": 0, "unreported": 0,
    }
    assert report["approvals"] == {"ok": True, "error": None, "escalation_enabled": True}
    json.dumps(report)  # one JSON document, nothing in it that will not serialise


def test_every_part_has_the_same_keys_good_or_failed(settings):
    report = build_status(settings, probes=FAKE)

    for part in PARTS:
        assert set(report[part]) == {"ok", "error", *FAILED_DEFAULTS[part]}, part


def test_the_paths_are_where_keryx_keeps_things(settings):
    report = build_status(settings, probes=FAKE)

    assert report["paths"] == {
        "ok": True,
        "error": None,
        "config": str(settings.config_dir),
        "data": str(settings.data_dir),
        "state": str(settings.state_dir),
        "cache": str(settings.cache_dir),
        "logs": str(settings.state_dir / "logs"),
        "memory": str(memory_path(settings.data_dir)),
    }


# --- storage -------------------------------------------------------------------------------


def test_a_broken_config_file_is_a_storage_error_not_a_refusal(settings):
    report = build_status(
        settings, probes=FAKE, config_problems={"config_file": "config.toml: bad TOML"}
    )

    assert report["storage"] == {
        "ok": False, "error": "config.toml: bad TOML", "migration_required": False,
    }
    assert report["tasks"]["ok"] is True


def test_migration_needed_skips_the_database(settings_with_old_jarvis_dir):
    report = build_status(settings_with_old_jarvis_dir, probes=FAKE)

    assert report["storage"]["migration_required"] is True
    assert report["storage"]["ok"] is False
    assert "keryx migrate" in report["storage"]["error"]
    assert report["tasks"] == {
        "ok": False, "error": "needs keryx migrate", "running": 0, "queued": 0, "unreported": 0,
    }
    assert not (settings_with_old_jarvis_dir.data_dir / "tasks.db").exists()


def test_migration_needed_still_reports_everything_else(settings_with_old_jarvis_dir):
    report = build_status(settings_with_old_jarvis_dir, probes=FAKE)

    assert report["service"]["ok"] is True
    assert report["assistant"]["name"] == "Lyra"


# --- service -------------------------------------------------------------------------------


def test_no_supervisor_is_a_fact_not_an_error(settings):
    probes = dataclasses.replace(FAKE, target=lambda settings: None, installed=boom, active=boom)

    report = build_status(settings, probes=probes)

    assert report["service"] == {
        "ok": True, "error": None, "manager": None, "unit": None,
        "installed": False, "active": False,
    }


def test_an_installed_service_that_is_stopped(settings):
    probes = dataclasses.replace(FAKE, active=lambda target: False)

    report = build_status(settings, probes=probes)

    assert report["service"]["installed"] is True
    assert report["service"]["active"] is False


def test_a_unit_that_is_not_installed_is_not_asked_whether_it_runs(settings):
    probes = dataclasses.replace(FAKE, installed=lambda target: False, active=boom)

    report = build_status(settings, probes=probes)

    assert report["service"]["ok"] is True
    assert report["service"]["installed"] is False
    assert report["service"]["active"] is False


# --- health --------------------------------------------------------------------------------


def test_nothing_answering_is_information_not_an_error(settings):
    probes = dataclasses.replace(FAKE, health=lambda settings: None)

    report = build_status(settings, probes=probes)

    assert report["health"] == {
        "ok": True, "error": None, "reachable": False, "live_sessions": None,
    }


def test_live_calls_are_counted(settings):
    probes = dataclasses.replace(FAKE, health=lambda settings: 2)

    assert build_status(settings, probes=probes)["health"]["live_sessions"] == 2


def test_the_default_health_probe_is_bounded(monkeypatch, settings):
    seen = {}

    def probe(settings, *, timeout):
        seen["timeout"] = timeout
        return 1

    monkeypatch.setattr(status, "health_probe", probe)

    assert status.default_probes().health(settings) == 1
    assert 1 <= seen["timeout"] <= 2


def test_the_default_probes_ask_from_outside_the_unit(monkeypatch, settings):
    seen = {}

    def resolve(settings, **kwargs):
        seen.update(kwargs)
        return TARGET

    monkeypatch.setattr(status, "resolve_target", resolve)

    assert status.default_probes().target(settings) == TARGET
    assert seen == {"from_outside": True}


# --- version, restart, assistant, agents, approvals ---------------------------------------


def test_versions_that_were_never_stamped_are_null(settings):
    probes = dataclasses.replace(
        FAKE, running_version=lambda settings: None, checkout_version=lambda settings: None
    )

    report = build_status(settings, probes=probes)

    assert report["version"] == {"ok": True, "error": None, "running": None, "checkout": None}


def test_the_default_running_version_is_the_service_stamp(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "running-version").write_text("v9.9.9\n")

    assert status.default_probes().running_version(settings) == "v9.9.9"


def test_a_failed_restart_is_reported_with_its_error(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    asked = datetime(2026, 10, 6, 9, 30, tzinfo=UTC).isoformat()
    RestartStore(settings.state_dir / RECORD_NAME).save(
        RestartRecord(
            requested_at=asked, number="+15555550123", state="failed",
            error="launchctl exited 5",
        )
    )

    report = build_status(settings, probes=FAKE)

    assert report["restart"] == {
        "ok": True, "error": None, "state": "failed", "requested_at": asked,
        "failure": "launchctl exited 5",
    }
    assert "5555550123" not in json.dumps(report)  # the record's number never leaves it


def test_a_pending_restart_has_no_failure(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    RestartStore(settings.state_dir / RECORD_NAME).save(RestartRecord(requested_at="x"))

    report = build_status(settings, probes=FAKE)

    assert report["restart"]["state"] == "pending"
    assert report["restart"]["failure"] is None


def test_the_assistant_is_called_by_its_configured_name(settings):
    renamed = settings.model_copy(update={"assistant_name": "Ada"})

    assert build_status(renamed, probes=FAKE)["assistant"]["name"] == "Ada"


def test_the_agents_are_the_default_first_then_the_enabled(settings):
    both = settings.model_copy(
        update={"agent_backend": "codex", "agents_enabled": ["claude", "codex"]}
    )

    report = build_status(both, probes=FAKE)

    assert report["agents"]["default"] == "codex"
    assert report["agents"]["enabled"] == ["codex", "claude"]


def test_the_kill_switch_turns_approval_calls_off(settings):
    switch = settings.state_dir / STATE_DIR_NAME / KILL_SWITCH_NAME
    switch.parent.mkdir(parents=True)
    switch.write_text("")

    assert build_status(settings, probes=FAKE)["approvals"]["escalation_enabled"] is False


# --- tasks ---------------------------------------------------------------------------------


def test_tasks_are_counted_and_unreported_is_finished_and_not_told(settings):
    seed(
        settings,
        task(TaskStatus.RUNNING),
        task(TaskStatus.QUEUED),
        task(TaskStatus.QUEUED),
        task(TaskStatus.DONE),  # not told
        task(TaskStatus.FAILED),  # not told
        task(TaskStatus.DONE, reported_at=datetime.now(UTC)),  # told
        task(TaskStatus.CANCELLED),  # nothing to tell
        task(TaskStatus.DONE, internal=True),  # housekeeping, never told
        task(TaskStatus.RUNNING, internal=True),  # the memory update, not their work
    )

    report = build_status(settings, probes=FAKE)

    assert report["tasks"] == {
        "ok": True, "error": None, "running": 1, "queued": 2, "unreported": 2,
    }


def test_no_database_yet_is_nothing_to_count_and_is_not_created(settings):
    report = build_status(settings, probes=FAKE)

    assert report["tasks"]["ok"] is True
    assert not (settings.data_dir / TASK_DB_NAME).exists()


def test_the_task_counts_are_one_read(monkeypatch, settings):
    seed(settings, task(TaskStatus.RUNNING))
    calls = []
    real = TaskStore.list

    async def counting(self, **kwargs):
        calls.append(kwargs)
        return await real(self, **kwargs)

    monkeypatch.setattr(TaskStore, "list", counting)

    build_status(settings, probes=FAKE)

    assert len(calls) == 1
    assert calls[0].get("status") is None


# --- one broken part does not take the others ---------------------------------------------


def test_one_broken_part_does_not_take_the_others(settings):
    probes = dataclasses.replace(FAKE, health=lambda s: (_ for _ in ()).throw(OSError("boom")))
    report = build_status(settings, probes=probes)
    assert report["health"] == {
        "ok": False, "error": "boom", "reachable": False, "live_sessions": None,
    }
    assert report["assistant"]["name"] == "Lyra"


@pytest.mark.parametrize("part", PARTS)
def test_a_part_that_raises_keeps_its_shape_and_spares_the_rest(monkeypatch, settings, part):
    healthy = build_status(settings, probes=FAKE)
    monkeypatch.setattr(status, f"_{part}", boom)

    report = build_status(settings, probes=FAKE)

    assert report[part] == {"ok": False, "error": "boom", **FAILED_DEFAULTS[part]}
    assert set(report[part]) == set(healthy[part])
    for other in PARTS:
        if other != part:
            assert report[other] == healthy[other], other


def test_an_exception_with_no_message_still_says_what_it_was(settings):
    def silent(target):
        raise TimeoutError

    report = build_status(settings, probes=dataclasses.replace(FAKE, installed=silent))

    assert report["service"]["error"] == "TimeoutError"


def test_settings_that_cannot_load_still_make_a_whole_document():
    report = failed_status("ASSISTANT_NAME is a coding agent's name")

    assert report["schema"] == 1
    for part in PARTS:
        assert report[part] == {
            "ok": False, "error": "ASSISTANT_NAME is a coding agent's name",
            **FAILED_DEFAULTS[part],
        }


# --- the text a person reads -----------------------------------------------------------------


def test_the_text_of_a_healthy_machine_is_five_lines(settings):
    lines = format_status(build_status(settings, probes=FAKE))

    assert len(lines) == 5
    assert lines[0].split() == ["Keryx", "running", "(launchd", "dev.keryx.agent)"]
    assert "v0.1.0-3-g1ad6420" in lines[1] and "checkout v0.1.0-5-gabc1234" in lines[1]
    assert lines[2].split()[1:] == ["none", "live"]
    assert lines[3].split()[1:] == ["0", "running,", "0", "queued,", "0", "not", "yet", "told"]
    assert lines[4].split()[2:] == ["on"]


def test_the_text_says_stopped_and_not_installed(settings):
    stopped = build_status(settings, probes=dataclasses.replace(FAKE, active=lambda t: False))
    missing = build_status(settings, probes=dataclasses.replace(FAKE, target=lambda s: None))

    assert format_status(stopped)[0].split()[:2] == ["Keryx", "stopped"]
    assert format_status(missing)[0].split() == ["Keryx", "not", "installed"]


def test_the_text_says_nothing_is_answering_and_the_switch_is_off(settings):
    switch = settings.state_dir / STATE_DIR_NAME / KILL_SWITCH_NAME
    switch.parent.mkdir(parents=True)
    switch.write_text("")
    probes = dataclasses.replace(
        FAKE, health=lambda s: None, checkout_version=lambda s: "v0.1.0-3-g1ad6420"
    )

    lines = format_status(build_status(settings, probes=probes))

    assert lines[1].split() == ["version", "v0.1.0-3-g1ad6420"]  # nothing to compare
    assert lines[2].split()[1:] == ["not", "answering"]
    assert "off" in lines[4]


def test_the_text_adds_the_migration_line_when_there_is_one(settings_with_old_jarvis_dir):
    lines = format_status(build_status(settings_with_old_jarvis_dir, probes=FAKE))

    assert len(lines) == 6
    assert lines[3].split()[1:] == ["unknown", "(needs", "keryx", "migrate)"]
    assert lines[5].startswith("storage") and "keryx migrate" in lines[5]


def test_the_text_of_a_failed_part_is_unknown_with_why(settings):
    lines = format_status(failed_status("broken"))

    assert len(lines) == 6
    for line in lines:
        assert "broken" in line
