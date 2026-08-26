"""Prompt templates, loaded from the installed package rather than the source tree.

The markdown files live next to this module and are read with `importlib.resources`, so
they work the same from a wheel, an editable install or a zip. Templates use plain
`str.format` placeholders, which means they must not contain any other curly braces.
"""

import logging
from datetime import datetime
from importlib import resources
from typing import TYPE_CHECKING

from jarvis.projects import ProjectBrief, discover_briefs, discover_projects
from jarvis.skills import Skill, discover_skills

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config import Settings

log = logging.getLogger("jarvis.prompts")

VOICE_SYSTEM_PROMPT = "voice_system.md"

_TIME_FORMAT = "%A %d %B %Y, %H:%M"
_OPENING_HEADING = "## Why this session opened"
#: Both of these sections carry their own heading so that an empty one disappears from the
#: prompt entirely, rather than leaving a heading with nothing under it for the model to
#: wonder about.
_PENDING_HEADING = "## What he has not heard yet"
_MEMORY_HEADING = (
    "## What you remember\n\n"
    "Written down after earlier calls, because you keep no memory of them yourself. It is "
    "background: use it to understand what he means and what he is in the middle of. Do "
    "not read it out, and do not treat it as today's news — check before you assert "
    "anything from it as still true."
)
_NO_SKILLS = "none installed"
_NO_BRIEFS = "nothing written down yet"


def load_prompt(name: str) -> str:
    """Read a packaged prompt template by file name (e.g. `voice_system.md`)."""
    resource = resources.files("jarvis.prompts").joinpath(name)
    if not resource.is_file():
        raise FileNotFoundError(f"no such prompt template: {name}")
    return resource.read_text(encoding="utf-8")


class _Defaulting(dict):
    """Format mapping that blanks unknown placeholders instead of raising."""

    def __missing__(self, key: str) -> str:
        log.warning("prompt template has an unknown placeholder: %s", key)
        return ""


def render_prompt(name: str, /, **values: str) -> str:
    """A packaged template rendered with `values`; an unknown placeholder blanks out."""
    return load_prompt(name).format_map(_Defaulting(values))


def _format_skills(skills: list[Skill]) -> str:
    """The skill catalog as prompt lines, one per skill."""
    if not skills:
        return _NO_SKILLS
    return "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)


def _nest_headings(text: str) -> str:
    """Every markdown heading in `text` pushed one level deeper.

    The memory document is written to be read on its own (`jarvis memory`), so its sections
    are `##`. Dropped into the prompt unchanged they would sit at the same level as the
    prompt's own sections, and "Standing facts" would read as an instruction to Jarvis
    rather than as part of what it remembers.
    """
    return "\n".join(
        f"#{line}" if line.startswith("#") else line for line in text.splitlines()
    )


def _format_briefs(briefs: list[ProjectBrief]) -> str:
    """Each project's own words about itself, under its name."""
    if not briefs:
        return _NO_BRIEFS
    return "\n\n".join(f"### {brief.name}\n\n{brief.text}" for brief in briefs)


def render_voice_prompt(
    settings: "Settings",
    *,
    channel: str,
    caller: str | None,
    authorized: bool,
    projects: list[str] | None = None,
    skills: list[Skill] | None = None,
    briefs: list[ProjectBrief] | None = None,
    opening_context: str | None = None,
    pending: str | None = None,
    memory: str | None = None,
) -> str:
    """Render the voice system prompt for one session.

    `projects` defaults to every project the `TaskManager` can resolve — the configured
    ones plus the subdirectories of `projects_root` — so the model offers names that
    actually dispatch. `skills` defaults to the skills installed for the Claude CLI, so
    it can recognise work the back office is good at without being told they exist.
    `briefs` defaults to the `.jarvis-brief.md` of every project that wrote one.
    `opening_context` is the reason the session was opened (a task summary on a call-back,
    say) and is dropped from the prompt when there is none. `pending` and `memory` come
    from a `Briefing` (see `jarvis.briefing`) and are dropped the same way: a first call on
    a fresh machine renders neither section, rather than an empty heading.
    """
    known = discover_projects(settings)
    names = list(known) if projects is None else projects
    catalog = discover_skills(settings.skills_dir) if skills is None else skills
    written = discover_briefs(known) if briefs is None else briefs
    return render_prompt(
        VOICE_SYSTEM_PROMPT,
        now=datetime.now().strftime(_TIME_FORMAT),
        channel=channel,
        caller=caller or "unknown",
        authorized="yes" if authorized else "no",
        projects=", ".join(names) if names else "none configured",
        skills=_format_skills(catalog),
        project_briefs=_format_briefs(written),
        opening_context=f"{_OPENING_HEADING}\n\n{opening_context}" if opening_context else "",
        pending_tasks=f"{_PENDING_HEADING}\n\n{pending}" if pending else "",
        memory=f"{_MEMORY_HEADING}\n\n{_nest_headings(memory)}" if memory else "",
    )
