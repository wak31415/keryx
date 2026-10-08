"""Project context from the Mac app: the question, the agent's draft as JSON lines, and only
what the owner kept of it written — each through the wizard's own code."""

import asyncio
import dataclasses
import json
import os
import stat

import pytest
from typer.testing import CliRunner
from wording import for_the_app

from keryx.agents.base import RunResult
from keryx.agents.registry import BACKENDS
from keryx.cli import app
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.continuity.memory import read_memory, seed_memory
from keryx.projects import MAX_BRIEF_CHARS
from keryx.projects import summaries_dir as summaries
from keryx.setup import project_stream
from keryx.setup.project_context import Draft

runner = CliRunner()
SECRET = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def home(monkeypatch, tmp_path, every_agent_installed):
    """A hermetic install with two projects under PROJECTS_ROOT, as every command loads it."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None,
        openai_api_key=SECRET,
        owner_name="Ada",
        data_dir=tmp_path / "keryx",
        projects_root=tmp_path / "projects",
        skills_dir=tmp_path / "skills",
    )
    for name in ("orchard", "weather"):
        (settings.projects_root / name).mkdir(parents=True)
    monkeypatch.setattr("keryx.cli.load_settings", lambda **overrides: settings)
    return settings


def answer(projects, facts=()) -> str:
    body = json.dumps({"facts": list(facts), "projects": projects})
    return f"Here you go:\n```json\n{body}\n```\nSPOKEN_SUMMARY: I summarised them."


class Agent:
    """`setup.agents.run_task`, faked: the prompt it was given, progress, then `result`."""

    def __init__(self, result: RunResult, progress=()) -> None:
        self.result, self.progress = result, list(progress)
        self.prompts: list[str] = []

    async def __call__(self, settings, agent, prompt, *, on_progress=None):
        self.prompts.append(prompt)
        for line in self.progress:
            on_progress(line)
        return self.result


def draft_stream(*args: str):
    result = runner.invoke(app, ["setup", "project-context", *args, "--json"])
    return result, [json.loads(line) for line in result.stdout.splitlines()]


def ends_once(events) -> str:
    endings = [e["event"] for e in events if e["event"] in ("done", "error", "cancelled")]
    assert len(endings) == 1 and events[-1]["event"] == endings[0], events
    return endings[0]


# --- the question ----------------------------------------------------------------------


def test_the_question_is_the_wizards_worded_for_the_app(home):
    (summaries(home)).mkdir(parents=True)
    (summaries(home) / "orchard.md").write_text("Soil sensors.\n")

    result = runner.invoke(app, ["setup", "project-context", "--describe", "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["question"] == "Let Claude explore your projects?"
    assert [choice["value"] for choice in document["choices"]] == ["explore", "folders", "no"]
    assert document["choices"][0]["hint"] == project_stream.EXPLORE_HINT
    assert document["choices"][1]["hint"] is None
    labels = [choice["label"] for choice in document["choices"]]
    for text in (document["note"], document["question"], *labels, project_stream.EXPLORE_HINT):
        assert for_the_app(text), text
    assert document["summaries"] == ["orchard"]
    assert document["projects"] == ["orchard", "weather"]
    assert document["max_brief_chars"] == MAX_BRIEF_CHARS and document["max_facts"] == 5


def test_without_json_the_question_is_a_line(home):
    result = runner.invoke(app, ["setup", "project-context", "--describe"])

    assert result.exit_code == 0
    assert "Let Claude explore your projects? (explore, folders, no)" in result.stdout


# --- the draft -------------------------------------------------------------------------


def test_exploring_drafts_from_home_and_writes_nothing(home, monkeypatch, tmp_path):
    thesis = tmp_path / "thesis"
    thesis.mkdir()
    agent = Agent(
        RunResult(ok=True, final_text=answer(
            [{"name": "orchard", "path": str(home.projects_root / "orchard"),
              "summary": "Soil sensors."},
             {"name": "thesis", "path": str(thesis), "summary": "The thesis."}],
            ["Works on sensors."],
        )),
        progress=[f"[tool] Read {{'file_path': '/x', 'key': '{SECRET}'}}", "second line"],
    )
    monkeypatch.setattr("keryx.setup.agents.run_task", agent)

    result, events = draft_stream("--explore")

    assert result.exit_code == 0, result.output
    assert ends_once(events) == "done"
    steps = [event["step"] for event in events if event["event"] == "step"]
    assert steps == ["dispatch", "working", "reading"]  # the second line is within a second
    for event in events[:-1]:
        assert set(event) == {"event", "step", "message", "detail"}
        assert for_the_app(event["message"])
    assert "Read" in events[1]["detail"] and SECRET not in result.stdout
    done = events[-1]
    assert done["summary"] == ("Claude drafted 2 project summaries and 1 thing about you for "
                               "you to check.")
    assert for_the_app(done["summary"])
    assert done["folders"] == [str(os.path.expanduser("~"))]
    assert done["projects"][0] == {"name": "orchard", "path": str(home.projects_root / "orchard"),
                                   "summary": "Soil sensors.", "known": True,
                                   "adds_project": False}
    assert done["projects"][1]["known"] is False
    assert done["facts"] == ["Works on sensors."] and done["dropped"] == []
    assert "find where Ada keeps their work" in agent.prompts[0]
    assert not summaries(home).exists() and not read_memory(home.data_dir)


def test_named_folders_are_the_only_ones_read(home, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "thesis").mkdir(parents=True)
    agent = Agent(RunResult(ok=True, final_text=answer(
        [{"name": "thesis", "path": str(elsewhere / "thesis"), "summary": "The thesis."}])))
    monkeypatch.setattr("keryx.setup.agents.run_task", agent)

    result, events = draft_stream("--folder", str(elsewhere))

    assert result.exit_code == 0, result.output
    done = events[-1]
    assert done["folders"] == [str(elsewhere)]
    assert done["projects"][0]["adds_project"] is True
    assert str(elsewhere) in agent.prompts[0] and "find where" not in agent.prompts[0]


def test_nothing_found_says_so(home, monkeypatch):
    monkeypatch.setattr("keryx.setup.agents.run_task",
                        Agent(RunResult(ok=True, final_text=answer([]))))

    _, events = draft_stream("--explore")

    assert events[-1]["summary"] == "Claude found nothing to summarise."


@pytest.mark.parametrize("args", [(), ("--explore", "--folder", "/tmp")])
def test_where_to_look_has_no_answer_assumed(home, monkeypatch, args):
    agent = Agent(RunResult(ok=True, final_text=answer([])))
    monkeypatch.setattr("keryx.setup.agents.run_task", agent)

    result, events = draft_stream(*args)

    assert result.exit_code == 2 and ends_once(events) == "error"
    assert events[0]["message"] == project_stream.WHERE and not agent.prompts


def test_a_folder_that_is_not_one_is_refused_before_anything_runs(home, monkeypatch, tmp_path):
    agent = Agent(RunResult(ok=True, final_text=answer([])))
    monkeypatch.setattr("keryx.setup.agents.run_task", agent)

    result, events = draft_stream("--folder", str(tmp_path / "missing"))

    assert result.exit_code == 2 and ends_once(events) == "error"
    assert events[0]["message"] == project_stream.NOT_FOLDERS
    assert "missing" in events[0]["detail"] and not agent.prompts


def test_an_agent_that_cannot_finish_says_why_in_the_detail(home, monkeypatch):
    monkeypatch.setattr("keryx.setup.agents.run_task", Agent(
        RunResult(ok=False, error=f"401 from the provider for key {SECRET}")))

    result, events = draft_stream("--explore")

    assert result.exit_code == 1 and ends_once(events) == "error"
    assert events[-1]["message"] == "Claude could not finish reading your projects."
    assert "401" in events[-1]["detail"] and SECRET not in result.stdout


def test_an_answer_that_cannot_be_read_is_an_error(home, monkeypatch):
    monkeypatch.setattr("keryx.setup.agents.run_task",
                        Agent(RunResult(ok=True, final_text="I could not do it.")))

    result, events = draft_stream("--explore")

    assert result.exit_code == 1 and ends_once(events) == "error"
    assert events[-1]["message"] == "Claude's answer could not be read."
    assert events[-2]["step"] == "reading"


def test_the_service_may_not_dispatch_it(home, monkeypatch):
    agent = Agent(RunResult(ok=True, final_text=answer([])))
    monkeypatch.setattr("keryx.setup.agents.run_task", agent)
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result, events = draft_stream("--explore")

    assert result.exit_code == 1 and "only the owner" in events[0]["message"]
    assert not agent.prompts


def test_the_draft_is_for_the_app_alone(home):
    result = runner.invoke(app, ["setup", "project-context", "--explore"])

    assert result.exit_code == 2 and "keryx setup" in result.stderr


def test_two_of_describe_apply_and_a_draft_are_a_wrong_command_line(home):
    result = runner.invoke(app, ["setup", "project-context", "--describe", "--apply"])

    assert result.exit_code == 2


class Hanging:
    """An agent that is still reading when Cancel comes, and is closed after it."""

    def __init__(self) -> None:
        self.closed = False
        self.actor_during: str | None = None

    async def open(self, task, *, resume=None):
        return self

    async def run(self, prompt, *, on_progress):
        self.actor_during = os.environ.get("KERYX_ACTOR")
        on_progress("looking around")

        def cancel() -> None:
            raise KeyboardInterrupt  # what the SIGTERM handler turns the app's Cancel into

        asyncio.get_running_loop().call_soon(cancel)
        await asyncio.sleep(60)

    async def close(self):
        self.closed = True


def test_cancel_closes_the_agent_before_it_ends_and_leaves_the_actor_as_it_was(home,
                                                                               monkeypatch):
    agent = Hanging()
    spec = BACKENDS["claude"]
    monkeypatch.setitem(BACKENDS, "claude",
                        dataclasses.replace(spec, make_runner=lambda settings: agent))
    monkeypatch.delenv("KERYX_ACTOR", raising=False)

    result, events = draft_stream("--explore")

    assert result.exit_code == 130 and ends_once(events) == "cancelled"
    assert agent.actor_during == "service" and agent.closed
    assert "KERYX_ACTOR" not in os.environ


def test_a_summary_names_one_project_and_no_facts():
    draft = Draft(projects=[("orchard", "", "Soil.")])

    assert project_stream.draft_summary("Codex", draft) == (
        "Codex drafted 1 project summary for you to check.")


# --- what is kept ----------------------------------------------------------------------


def keep(kept, *, as_json=True):
    args = ["setup", "project-context", "--apply"] + (["--json"] if as_json else [])
    result = runner.invoke(app, args, input=json.dumps(kept))
    return result, (json.loads(result.stdout) if as_json and result.stdout.strip() else None)


def test_what_was_kept_is_written_as_the_wizard_writes_it(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "thesis").mkdir(parents=True)
    seed_memory(home.data_dir, owner="Ada", assistant="Lyra", facts=["Old fact."])

    result, outcome = keep({
        "folders": [str(home.projects_root), str(elsewhere)],
        "projects": [
            {"name": "orchard", "path": str(home.projects_root / "orchard"),
             "summary": "Soil sensors."},
            {"name": "thesis", "path": str(elsewhere / "thesis"),
             "summary": "x " * MAX_BRIEF_CHARS},
            {"name": "stray", "path": str(tmp_path), "summary": "Not in a chosen folder."},
        ],
        "facts": ["Writes a thesis."],
    })

    assert result.exit_code == 0, result.output
    assert outcome == {"kept": ["orchard", "thesis", "stray"], "added_projects": ["thesis"],
                       "facts_added": 1, "problems": [], "walked": True}
    written = summaries(home)
    assert (written / "orchard.md").read_text() == "Soil sensors.\n"
    assert len((written / "thesis.md").read_text().strip()) <= MAX_BRIEF_CHARS
    assert stat.S_IMODE((written / "orchard.md").stat().st_mode) == 0o600
    assert ConfigStore().stored()["PROJECTS"] == {"thesis": str(elsewhere / "thesis")}
    memory = read_memory(home.data_dir)
    assert "- Old fact." in memory and "- Writes a thesis." in memory
    assert "projects" in ConfigStore().walked_sections()


def test_nothing_kept_is_walked_and_writes_nothing(home):
    result, outcome = keep({"folders": [], "projects": [], "facts": []})

    assert result.exit_code == 0
    assert outcome["kept"] == [] and outcome["walked"] is True
    assert not summaries(home).exists() and not read_memory(home.data_dir)


@pytest.mark.parametrize(
    ("kept", "problem"),
    [({"projects": [{"name": "../etc", "summary": "x"}]}, "projects"),
     ({"projects": [{"name": "a", "summary": "x"}, {"name": "a", "summary": "y"}]}, "projects"),
     ({"projects": [{"name": "a", "summary": " "}]}, "projects.a"),
     ({"projects": [{"name": "a", "summary": "x", "path": 3}]}, "projects.a"),
     ({"projects": {"a": "x"}}, "projects"),
     ({"projects": ["a"]}, "projects"),
     ({"facts": "one"}, "facts"), ({"facts": ["f"] * 6}, "facts"),
     ({"folders": "/x"}, "folders"), ({"notes": []}, "notes")],
)
def test_an_entry_that_is_not_one_is_named_and_nothing_is_written(home, kept, problem):
    result, outcome = keep(kept)

    assert result.exit_code == 1
    assert [p["id"] for p in outcome["problems"]] == [problem]
    assert all(for_the_app(p["message"]) for p in outcome["problems"])
    assert outcome["walked"] is False and not summaries(home).exists()
    assert "projects" not in ConfigStore().walked_sections()


def test_a_memory_too_full_for_the_facts_keeps_the_summaries_and_says_so(home, monkeypatch):
    def full(*args, **kwargs):
        raise ValueError("that memory is 9000 characters, and a call reads 8000")

    monkeypatch.setattr("keryx.setup.project_context.add_standing_facts", full)

    result, outcome = keep({"projects": [{"name": "orchard", "summary": "Soil."}],
                            "facts": ["One more."]})

    assert result.exit_code == 1
    assert outcome["kept"] == ["orchard"] and outcome["facts_added"] == 0
    assert outcome["problems"] == [{"id": "facts", "message": (
        "That memory is 9000 characters, and a call reads 8000.")}]
    assert outcome["walked"] is True


def test_projects_the_store_refuses_are_not_added(home, monkeypatch, tmp_path):
    from keryx.config.store import ConfigError

    def refuses(self, values, **kwargs):
        raise ConfigError("refused")

    monkeypatch.setattr(ConfigStore, "set", refuses)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "thesis").mkdir(parents=True)

    _, outcome = keep({"folders": [str(elsewhere)], "projects": [
        {"name": "thesis", "path": str(elsewhere / "thesis"), "summary": "The thesis."}]})

    assert outcome["kept"] == ["thesis"] and outcome["added_projects"] == []


def test_the_service_may_not_keep_a_draft(home, monkeypatch):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result, _ = keep({"projects": [{"name": "orchard", "summary": "Soil."}]})

    assert result.exit_code == 1 and "only the owner" in result.stderr
    assert not summaries(home).exists()


def test_stdin_that_is_not_one_object_is_a_wrong_command_line(home):
    result = runner.invoke(app, ["setup", "project-context", "--apply", "--json"], input="[]")

    assert result.exit_code == 2 and not result.stdout.strip()


def test_without_json_what_was_kept_is_a_line(home):
    kept = runner.invoke(app, ["setup", "project-context", "--apply"],
                         input=json.dumps({"projects": [{"name": "orchard", "summary": "S."}]}))
    refused = runner.invoke(app, ["setup", "project-context", "--apply"],
                            input=json.dumps({"facts": "one"}))

    assert kept.exit_code == 0 and "1 project summaries kept, 0 facts added" in kept.stdout
    assert refused.exit_code == 1 and "facts:" in refused.stderr
