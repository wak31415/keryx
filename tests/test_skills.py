"""Tests for discovering the Claude skills installed on this machine."""

from pathlib import Path

from jarvis.skills import MAX_DESCRIPTION_CHARS, Skill, discover_skills


def write_skill(root: Path, directory: str, body: str) -> Path:
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(body, encoding="utf-8")
    return path


def test_a_skill_is_its_name_and_description(tmp_path):
    write_skill(
        tmp_path,
        "wandb-query",
        "---\nname: wandb-query\ndescription: Query W&B runs from the terminal.\n---\n\nBody.\n",
    )

    assert discover_skills(tmp_path) == [
        Skill(name="wandb-query", description="Query W&B runs from the terminal.")
    ]


def test_a_folded_description_is_joined_into_one_line(tmp_path):
    """Real skills write long descriptions as YAML block scalars."""
    write_skill(
        tmp_path,
        "codex",
        "---\n"
        "name: codex\n"
        "description: >-\n"
        "  Delegate tasks to OpenAI Codex\n"
        "  for a second opinion.\n"
        "allowed-tools: Bash\n"
        "---\n",
    )

    assert discover_skills(tmp_path) == [
        Skill(name="codex", description="Delegate tasks to OpenAI Codex for a second opinion.")
    ]


def test_a_long_description_is_shortened(tmp_path):
    write_skill(tmp_path, "verbose", f"---\nname: verbose\ndescription: {'word ' * 100}\n---\n")

    (skill,) = discover_skills(tmp_path)
    assert len(skill.description) <= MAX_DESCRIPTION_CHARS
    assert skill.description.endswith("…")


def test_the_directory_name_stands_in_for_a_missing_name(tmp_path):
    write_skill(tmp_path, "mermaid", "---\ndescription: Author Mermaid diagrams.\n---\n")

    assert discover_skills(tmp_path)[0].name == "mermaid"


def test_entries_without_a_usable_skill_file_are_skipped(tmp_path):
    write_skill(tmp_path, "fine", "---\nname: fine\ndescription: Something useful.\n---\n")
    write_skill(tmp_path, "no-description", "---\nname: no-description\n---\n")
    (tmp_path / "not-a-skill").mkdir()
    (tmp_path / "README.md").write_text("not a skill either", encoding="utf-8")

    assert [skill.name for skill in discover_skills(tmp_path)] == ["fine"]


def test_a_missing_skills_directory_is_not_an_error(tmp_path):
    assert discover_skills(tmp_path / "nothing-here") == []
