"""The skills installed for the coding agents, so the voice model knows they exist.

A skill is a directory with a `SKILL.md` whose front matter carries a name and a
one-line description. The subagent finds and runs them by itself; this module exists
only so the voice prompt can list what the back office is good at, and the owner never
has to name a skill out loud.

Front matter is parsed by hand rather than with a YAML dependency: the two fields we
want are plain `key: value` lines, and a malformed skill should cost us that one entry,
not the session.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("keryx.skills")

SKILL_FILE = "SKILL.md"
#: The skills that ship in the Keryx repository, beside `src/`.
BUNDLED_SKILLS = Path(__file__).resolve().parents[2] / "skills"
#: How to write the owner's own voice tools; its path is in every subagent's prompt.
CUSTOM_TOOLS_SKILL = BUNDLED_SKILLS / "keryx-custom-tools" / SKILL_FILE
#: Descriptions are written for a reader with a screen; the prompt only needs the gist.
MAX_DESCRIPTION_CHARS = 160


@dataclass(frozen=True)
class Skill:
    """One installed skill: what to call it and what it is for."""

    name: str
    description: str


def _front_matter(text: str) -> dict[str, str]:
    """The `key: value` lines of a leading `---` block, lowercased keys.

    A value of `>`, `>-`, `|` or `|-` starts a YAML block scalar, whose text is the
    indented lines under it — real skills are written both ways.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    fields: dict[str, str] = {}
    key: str | None = None
    block: list[str] = []

    def flush() -> None:
        if key is not None:
            fields[key] = " ".join(block).strip()

    for line in lines[1:]:
        if line.strip() == "---":
            break
        indented = line.startswith((" ", "\t"))
        if key is not None and (indented or not line.strip()):
            block.append(line.strip())
            continue
        name, separator, value = line.partition(":")
        if not separator or indented:
            continue
        flush()
        key, block = name.strip().lower(), []
        value = value.strip()
        if value in {">", ">-", "|", "|-"}:  # a block scalar: the text is on the next lines
            continue
        fields[key] = value.strip("\"'")
        key = None
    flush()
    return fields


def _shorten(description: str) -> str:
    """One line, short enough to be worth a prompt's space."""
    collapsed = " ".join(description.split())
    if len(collapsed) <= MAX_DESCRIPTION_CHARS:
        return collapsed
    return collapsed[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"


def discover_skills(root: Path) -> list[Skill]:
    """Every skill under `root`, by name. Never raises: an unreadable skill is skipped."""
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        log.exception("could not list the skills directory %s", root)
        return []

    skills: list[Skill] = []
    for entry in entries:
        manifest = entry / SKILL_FILE
        if not manifest.is_file():
            continue
        try:
            fields = _front_matter(manifest.read_text(encoding="utf-8"))
        except OSError:
            log.warning("could not read %s", manifest)
            continue
        description = fields.get("description", "")
        if not description:
            continue
        name = fields.get("name") or entry.name
        skills.append(Skill(name=name, description=_shorten(description)))
    return skills


def discover_skills_in(roots: Iterable[Path]) -> list[Skill]:
    """Every skill under any of `roots`, each name once: the first root that has it wins.

    One root per enabled coding agent. A skill both agents have installed is the same skill
    to the voice model, which only needs to know the work is possible.
    """
    found: dict[str, Skill] = {}
    for root in roots:
        for skill in discover_skills(root):
            found.setdefault(skill.name, skill)
    return list(found.values())
