"""Project context: the agent's JSON held to the limits a call reads, and nothing kept that the
owner did not accept."""

import json
import stat

import pytest

from jarvis.agents.base import RunResult
from jarvis.config.store import ConfigStore
from jarvis.continuity.memory import read_memory, seed_memory
from jarvis.projects import MAX_BRIEF_CHARS, MAX_BRIEFS_CHARS, discover_briefs, discover_projects
from jarvis.projects import summaries_dir as summaries
from jarvis.setup import project_context
from jarvis.setup.project_context import build_prompt, parse_draft

from .fakes import DEFAULT


def answer(projects, facts=()) -> str:
    body = json.dumps({"facts": list(facts), "projects": projects})
    return f"Here you go:\n```json\n{body}\n```\nSPOKEN_SUMMARY: I summarised them."


# --- the prompt and the parse ----------------------------------------------------------


def test_the_prompt_carries_the_folders_the_limits_and_the_rules(tmp_path):
    prompt = build_prompt("Ada", [tmp_path / "a", tmp_path / "b"])

    assert f"- {tmp_path / 'a'}" in prompt and f"- {tmp_path / 'b'}" in prompt
    assert str(MAX_BRIEF_CHARS) in prompt and str(MAX_BRIEFS_CHARS) in prompt
    assert "Read only" in prompt and ".env" in prompt
    assert '{"facts"' in prompt  # the braces survive formatting


def test_the_json_is_found_whatever_is_around_it():
    draft = parse_draft(answer([{"name": "orchard", "path": "/p", "summary": "Soil."}], ["Hi."]))

    assert draft.projects == [("orchard", "/p", "Soil.")]
    assert draft.facts == ["Hi."]


def test_a_summary_past_the_limit_is_cut_at_a_word():
    long = "word " * 1000
    [(name, _, summary)] = parse_draft(
        answer([{"name": "big", "path": "", "summary": long}])
    ).projects

    assert len(summary) <= MAX_BRIEF_CHARS and summary.endswith("…")


def test_projects_past_the_total_are_left_out_whole_and_named():
    one = "x" * (MAX_BRIEF_CHARS - 10)
    projects = [{"name": f"p{i}", "path": "", "summary": one} for i in range(6)]

    draft = parse_draft(answer(projects))

    assert sum(len(summary) for _, _, summary in draft.projects) <= MAX_BRIEFS_CHARS
    assert draft.dropped == ["p4", "p5"]


@pytest.mark.parametrize("name", ["", "../escape", ".hidden", "a/b", "x" * 80])
def test_a_name_that_is_not_a_safe_file_name_is_dropped(name):
    assert parse_draft(answer([{"name": name, "summary": "s"}])).projects == []


def test_facts_are_capped_at_five():
    assert len(parse_draft(answer([], [f"f{i}" for i in range(9)])).facts) == 5


def test_no_json_at_all_says_so():
    with pytest.raises(ValueError, match="no JSON object"):
        parse_draft("I could not find anything. SPOKEN_SUMMARY: nothing")


# --- the section ---------------------------------------------------------------------------


@pytest.fixture
def root(make_ctx):
    ctx = make_ctx([])
    root = ctx.settings.projects_root
    for name in ("orchard", "weather"):
        (root / name).mkdir(parents=True)
    return root


def test_the_owner_accepts_edits_and_drops_and_only_that_is_kept(make_ctx, world, root, tmp_path):
    outside = tmp_path / "elsewhere" / "thesis"
    outside.mkdir(parents=True)
    world.task_result = RunResult(
        ok=True,
        final_text=answer(
            [
                {"name": "orchard", "path": str(root / "orchard"), "summary": "Soil sensors."},
                {"name": "weather", "path": str(root / "weather"), "summary": "Forecasts."},
                {"name": "thesis", "path": str(outside), "summary": "The thesis."},
            ],
            ["Works on sensors.", "Writes a thesis."],
        ),
    )
    ctx = make_ctx(
        [
            ("Folders to look through", DEFAULT),
            ("May Claude look through", True),
            ("summary of orchard", "accept"),
            ("summary of weather", "drop"),
            ("summary of thesis", "edit"),
            ("Summary", "The PhD thesis, on soil."),
            ("remember about you", ["1"]),
        ]
    )
    seed_memory(ctx.settings.data_dir, owner="Ada", facts=["Old fact."])

    project_context.run_section(ctx)

    [(kind, agent, prompt)] = [call for call in world.calls if call[0] == "task"]
    assert agent == "claude" and str(root) in prompt
    written = summaries(ctx.settings)
    assert sorted(path.name for path in written.iterdir()) == ["orchard.md", "thesis.md"]
    assert (written / "thesis.md").read_text().strip() == "The PhD thesis, on soil."
    assert stat.S_IMODE((written / "orchard.md").stat().st_mode) == 0o600
    assert ConfigStore().stored()["PROJECTS"] == {"thesis": str(outside)}
    memory = read_memory(ctx.settings.data_dir)
    assert "- Old fact." in memory and "- Writes a thesis." in memory
    assert "Works on sensors" not in memory
    briefs = discover_briefs(discover_projects(ctx.settings), summaries=written)
    assert {brief.name: brief.text for brief in briefs} == {
        "orchard": "Soil sensors.",
        "thesis": "The PhD thesis, on soil.",
    }


def test_nothing_is_read_without_permission(make_ctx, world, root):
    ctx = make_ctx([("Folders", DEFAULT), ("May Claude look through", False)])

    project_context.run_section(ctx)

    assert not [call for call in world.calls if call[0] == "task"]


def test_a_folder_that_is_not_there_is_refused(make_ctx, world, tmp_path):
    ctx = make_ctx([("Folders", str(tmp_path / "nope"))])

    project_context.run_section(ctx)

    assert any("Not a folder" in line for line in ctx.ui.lines("error"))


def test_a_task_that_fails_says_why(make_ctx, world, root):
    world.task_result = RunResult(ok=False, error="rate limited")
    ctx = make_ctx([("Folders", DEFAULT), ("May Claude", True)])

    project_context.run_section(ctx)

    assert any("rate limited" in line for line in ctx.ui.lines("error"))
    assert not summaries(ctx.settings).exists()


def test_a_draft_that_is_not_a_json_object_is_skipped_for_the_next_one():
    text = 'first [1, 2] then {"projects": [{"name": "a", "summary": "s"}, "junk"]}'

    assert parse_draft(text).projects == [("a", "", "s")]


def test_a_repeated_project_is_kept_once():
    projects = [{"name": "a", "summary": "one"}, {"name": "a", "summary": "two"}]

    assert parse_draft(answer(projects)).projects == [("a", "", "one")]


def test_an_answer_that_cannot_be_read_says_so(make_ctx, world, root):
    world.task_result = RunResult(ok=True, final_text="no json here")
    ctx = make_ctx([("Folders", DEFAULT), ("May Claude", True)])

    project_context.run_section(ctx)

    assert any("could not be read" in line for line in ctx.ui.lines("error"))


def test_dropped_projects_are_named_and_an_edit_to_nothing_keeps_nothing(make_ctx, world, root):
    big = "x" * (MAX_BRIEF_CHARS - 10)
    world.task_result = RunResult(
        ok=True,
        final_text=answer([{"name": f"p{i}", "path": "", "summary": big} for i in range(5)]),
    )
    ctx = make_ctx(
        [("Folders", DEFAULT), ("May Claude", True)]
        + [(f"summary of p{i}", "drop") for i in range(3)]
        + [("summary of p3", "edit"), ("Summary", "")]
    )

    project_context.run_section(ctx)

    assert any("p4" in line for line in ctx.ui.lines("warn"))
    assert not summaries(ctx.settings).exists()
    assert ConfigStore().stored() == {}


def test_no_facts_kept_and_a_memory_too_full_are_both_quiet_failures(make_ctx, world, root):
    world.task_result = RunResult(ok=True, final_text=answer([], ["A fact."]))
    none_kept = make_ctx([("Folders", DEFAULT), ("May Claude", True), ("remember", [])])
    project_context.run_section(none_kept)
    assert not (none_kept.settings.data_dir / "memory.md").exists()

    from jarvis.continuity.memory import MAX_MEMORY_CHARS

    world.task_result = RunResult(ok=True, final_text=answer([], ["x" * MAX_MEMORY_CHARS]))
    too_full = make_ctx([("Folders", DEFAULT), ("May Claude", True), ("remember", ["0"])])
    project_context.run_section(too_full)
    assert any("a call reads" in line for line in too_full.ui.lines("error"))


def test_blank_folders_skip_the_section(make_ctx, world):
    project_context.run_section(make_ctx([("Folders", "")]))

    assert not [call for call in world.calls if call[0] == "task"]
