"""Prompt templates, loaded from the installed package rather than the source tree.

The markdown files live next to this module and are read with `importlib.resources`, so
they work the same from a wheel, an editable install or a zip. Templates use plain
`str.format` placeholders, which means they must not contain any other curly braces.
"""

import logging
from datetime import datetime
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config import Settings

log = logging.getLogger("jarvis.prompts")

VOICE_SYSTEM_PROMPT = "voice_system.md"

_TIME_FORMAT = "%A %d %B %Y, %H:%M"
_OPENING_HEADING = "## Why this session opened"


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


def render_voice_prompt(
    settings: "Settings",
    *,
    channel: str,
    caller: str | None,
    authorized: bool,
    projects: list[str] | None = None,
    opening_context: str | None = None,
) -> str:
    """Render the voice system prompt for one session.

    `projects` defaults to the project names from `settings`; pass a list to override it.
    `opening_context` is the reason the session was opened (a task summary on a call-back,
    say) and is dropped from the prompt when there is none.
    """
    names = sorted(settings.projects) if projects is None else projects
    return render_prompt(
        VOICE_SYSTEM_PROMPT,
        now=datetime.now().strftime(_TIME_FORMAT),
        channel=channel,
        caller=caller or "unknown",
        authorized="yes" if authorized else "no",
        projects=", ".join(names) if names else "none configured",
        opening_context=f"{_OPENING_HEADING}\n\n{opening_context}" if opening_context else "",
    )
