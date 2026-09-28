"""Tests for project discovery, which the manager and the voice prompt share."""

import logging

from jarvis.projects import (
    MAX_BRIEF_CHARS,
    MAX_BRIEFS_CHARS,
    discover_briefs,
    discover_projects,
)


def test_configured_projects_come_first_then_the_root(settings, tmp_path):
    root = tmp_path / "projects"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir()
    elsewhere = tmp_path / "elsewhere" / "gamma"
    elsewhere.mkdir(parents=True)
    settings.projects = {"gamma": str(elsewhere)}
    settings.projects_root = root

    assert list(discover_projects(settings)) == ["gamma", "alpha", "beta"]


def test_a_configured_project_inside_the_root_is_listed_once(settings, tmp_path):
    root = tmp_path / "projects"
    (root / "jarvis").mkdir(parents=True)
    settings.projects = {"the voice thing": str(root / "jarvis")}
    settings.projects_root = root

    assert list(discover_projects(settings)) == ["the voice thing"]


def test_dot_directories_and_files_are_not_projects(settings, tmp_path):
    root = tmp_path / "projects"
    (root / ".cache").mkdir(parents=True)
    (root / "real").mkdir()
    (root / "notes.txt").write_text("hi", encoding="utf-8")
    settings.projects_root = root

    assert list(discover_projects(settings)) == ["real"]


def test_a_missing_root_is_not_an_error(settings, tmp_path):
    settings.projects_root = tmp_path / "nothing-here"

    assert discover_projects(settings) == {}


# --- briefs ------------------------------------------------------------------


def test_a_project_with_a_brief_describes_itself(settings, tmp_path):
    root = tmp_path / "projects"
    (root / "orchard-sensor-net").mkdir(parents=True)
    (root / "orchard-sensor-net" / ".jarvis-brief.md").write_text(
        "Soil sensors in an orchard.\n", encoding="utf-8"
    )
    (root / "quiet").mkdir()
    settings.projects_root = root

    briefs = discover_briefs(discover_projects(settings))

    assert [(b.name, b.text) for b in briefs] == [
        ("orchard-sensor-net", "Soil sensors in an orchard.")
    ]


def test_a_very_long_brief_is_capped(settings, tmp_path):
    root = tmp_path / "projects"
    (root / "verbose").mkdir(parents=True)
    (root / "verbose" / ".jarvis-brief.md").write_text("word " * 2000, encoding="utf-8")
    settings.projects_root = root

    (brief,) = discover_briefs(discover_projects(settings))

    assert len(brief.text) <= MAX_BRIEF_CHARS
    assert brief.text.endswith("…")


def test_an_empty_brief_is_no_brief(settings, tmp_path):
    root = tmp_path / "projects"
    (root / "blank").mkdir(parents=True)
    (root / "blank" / ".jarvis-brief.md").write_text("   \n", encoding="utf-8")
    settings.projects_root = root

    assert discover_briefs(discover_projects(settings)) == []


def test_the_briefs_together_are_capped_and_what_is_left_out_is_logged(tmp_path, caplog):
    """Every brief rides on every call; a folder of forty projects must not mean a prompt
    forty briefs long. Whole briefs are left out rather than one cut mid-sentence."""
    projects = {}
    for index in range(10):
        path = tmp_path / f"project-{index}"
        path.mkdir()
        (path / ".jarvis-brief.md").write_text(f"{index} " + "x" * (MAX_BRIEF_CHARS - 10))
        projects[f"project-{index}"] = path
    (tmp_path / "short").mkdir()
    (tmp_path / "short" / ".jarvis-brief.md").write_text("A short one.")
    projects["short"] = tmp_path / "short"

    with caplog.at_level(logging.WARNING, logger="jarvis.projects"):
        briefs = discover_briefs(projects)

    assert sum(len(brief.text) for brief in briefs) <= MAX_BRIEFS_CHARS
    kept = [brief.name for brief in briefs]
    assert kept[0] == "project-0"  # in discovery order: configured projects first
    assert "short" in kept  # a brief that still fits is not dropped for coming late
    assert "project-9" not in kept
    assert "project-9" in caplog.text


# --- setup's summaries --------------------------------------------------------------


def test_a_summary_stands_in_for_a_missing_brief_and_a_repo_brief_wins(settings, tmp_path):
    from jarvis.projects import summaries_dir

    for name in ("orchard", "weather", "quiet"):
        (settings.projects_root / name).mkdir(parents=True)
    (settings.projects_root / "orchard" / ".jarvis-brief.md").write_text("The repo's own.")
    written = summaries_dir(settings)
    written.mkdir(parents=True)
    (written / "orchard.md").write_text("Setup's draft.")
    (written / "weather.md").write_text("Forecasts.")
    (written / "stranger.md").write_text("Not a project here.")

    briefs = discover_briefs(discover_projects(settings), summaries=written)

    assert {brief.name: brief.text for brief in briefs} == {
        "orchard": "The repo's own.",
        "weather": "Forecasts.",
    }


def test_without_a_summaries_directory_only_repo_briefs_count(settings):
    (settings.projects_root / "weather").mkdir(parents=True)
    written = settings.data_dir / "projects"
    written.mkdir(parents=True)
    (written / "weather.md").write_text("Forecasts.")

    assert discover_briefs(discover_projects(settings)) == []
