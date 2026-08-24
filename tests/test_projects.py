"""Tests for project discovery, which the manager and the voice prompt share."""

from jarvis.projects import MAX_BRIEF_CHARS, discover_briefs, discover_projects


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
    (root / "vidmem").mkdir(parents=True)
    (root / "vidmem" / ".jarvis-brief.md").write_text(
        "A video model with a memory.\n", encoding="utf-8"
    )
    (root / "quiet").mkdir()
    settings.projects_root = root

    briefs = discover_briefs(discover_projects(settings))

    assert [(b.name, b.text) for b in briefs] == [("vidmem", "A video model with a memory.")]


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
