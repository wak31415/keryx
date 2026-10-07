"""The Mac app's JSON contract: every key its decoders rely on is in what the CLI prints.

`macos/Tests/KeryxKitTests/Fixtures/contract.json` maps a command line to the keys the
Swift side reads, by path: `""` is the document, `"servers.llm"` a nested object, and a
path that lands on a list means its first element. Renaming a field here without the app
fails this test, not the owner's menu bar.

Each command runs against a store seeded so that no list it walks is empty, with the same
fakes as the rest of the suite: no service manager, no loopback GET, no `git`, no network.
"""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keryx import status
from keryx.cli import app
from keryx.config import Settings
from keryx.continuity.memory import memory_path
from keryx.localmodels import catalog, download, hardware, servers, use
from keryx.localmodels.hardware import Hardware
from keryx.tasks.models import Task, TaskKind, TaskStatus, task_log_path
from keryx.tasks.store import TaskStore

CONTRACT = Path(__file__).parents[1] / "macos/Tests/KeryxKitTests/Fixtures/contract.json"
#: What the placeholders in a contract command line stand for.
TASK_ID = 1
MODEL = catalog.by_key("qwen3-coder-30b-a3b")

runner = CliRunner()


def contract() -> dict[str, dict[str, list[str]]]:
    return json.loads(CONTRACT.read_text())


def argv(command: str) -> list[str]:
    words = {"ID": str(TASK_ID), "NAME": MODEL.key}
    return [words.get(word, word) for word in command.split()]


@pytest.fixture
def seeded(tmp_path, monkeypatch, every_agent_installed):
    """A store with one finished task (report, log, usage), an approval, and a memory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path / "projects"))
    settings = Settings()
    settings.ensure_dirs()

    report = settings.data_dir / "tasks" / f"{TASK_ID}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Task 1\n\ncheck the logs\n\n---\n\nAll fine.\n")
    task = Task(id=None, kind=TaskKind.AGENT, description="check the logs", project="keryx",
                status=TaskStatus.DONE, report_path=str(report), input_tokens=10,
                output_tokens=2, cost_usd=0.01)

    async def seed() -> None:
        store = TaskStore(settings.data_dir / "tasks.db")
        try:
            assert (await store.create(task)).id == TASK_ID
        finally:
            await store.close()

    asyncio.run(seed())
    task_log_path(settings.data_dir, TASK_ID).write_text(
        "[09:00:01] read the logs\n[09:00:05] [followup] and the old ones\n"
    )
    audit = settings.state_dir / "approvals" / "audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps({"ts": "2026-10-05T09:00:00+00:00", "event": "raised",
                                 "request_id": 1, "summary": "Claude wants to run: ls"}) + "\n")
    memory_path(settings.data_dir).write_text("## Standing facts\n\n- Likes it short.\n")

    # The status command's probes, answered (`tests/test_cli.py`'s `quiet_status_probes`).
    monkeypatch.setattr(status, "default_probes", lambda: status.StatusProbes(
        target=lambda settings: None, installed=lambda target: False,
        active=lambda target: False, health=lambda settings: None,
        running_version=lambda settings: None, checkout_version=lambda settings: None,
    ))
    # The models commands: a known machine, no server asked, and one model on disk (sparse).
    monkeypatch.setattr(hardware, "detect", lambda: Hardware("metal", "Apple silicon", 0.0, 36.0))
    monkeypatch.setattr(use, "unit_installed", lambda kind: False)
    monkeypatch.setattr(use, "probe", lambda url: (False, None))
    monkeypatch.setattr(servers, "free_port", lambda preferred: preferred)
    monkeypatch.setattr("keryx.localmodels.runtimes.llama_server", lambda cache_dir, **_: None)
    model = download.model_path(settings.cache_dir, MODEL)
    model.parent.mkdir(parents=True, exist_ok=True)
    with model.open("wb") as out:
        out.truncate(MODEL.size)
    return settings


def at(document: object, path: str, command: str) -> object:
    """The object at `path` (dotted), stepping into the first element of any list."""
    node = document
    for part in [p for p in path.split(".") if p]:
        node = _first(node, command, part)
        assert isinstance(node, dict), f"{command}: {path!r} is not an object"
        assert part in node, f"{command}: no {part!r} on the way to {path!r}"
        node = node[part]
    return _first(node, command, path)


def _first(node: object, command: str, path: str) -> object:
    if isinstance(node, list):
        assert node, f"{command}: {path!r} is an empty list; seed it so the contract is checked"
        return node[0]
    return node


def test_the_contract_names_every_command_the_app_reads_as_json():
    assert set(contract()) == {
        "status --json", "doctor --json", "tasks list --json", "tasks show ID --json",
        "tasks usage --json", "models list --json", "models use NAME --json",
        "config list --json", "approvals --json", "memory --json",
    }


@pytest.mark.parametrize("command", sorted(contract()))
def test_every_key_the_app_needs_is_printed(command, seeded):
    result = runner.invoke(app, argv(command))

    assert result.exit_code in ((0, 1) if command == "doctor --json" else (0,)), result.output
    document = json.loads(result.stdout)
    for path, keys in contract()[command].items():
        node = at(document, path, command)
        assert isinstance(node, dict), f"{command}: {path!r} is not an object"
        missing = [key for key in keys if key not in node]
        assert not missing, f"{command}: {path or 'the document'} has no {missing}"

