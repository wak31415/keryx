"""Which repositories a coding task may be pointed at.

Both the `TaskManager` (which resolves a spoken name to a working directory) and the
voice prompt (which tells the model what names exist) have to agree on the answer, so
the discovery lives here rather than inside either of them.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config import Settings

log = logging.getLogger("jarvis.projects")

#: A project may describe itself to the voice model in this file. It is written for
#: someone who will hear it, not for an engineer reading a screen: what the project is,
#: what state it is in, what the words in it mean out loud. A repository's CLAUDE.md is
#: the wrong thing to paste here — thousands of tokens of build detail that the subagent
#: reads for itself anyway, and that would drown a receptionist's prompt.
BRIEF_FILE = ".jarvis-brief.md"
MAX_BRIEF_CHARS = 1500


@dataclass(frozen=True)
class ProjectBrief:
    """One project's own description of itself."""

    name: str
    text: str


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - resolve() rarely fails on a plain path
        return path


def discover_projects(settings: "Settings") -> dict[str, Path]:
    """Every known project: configured ones first, then `projects_root` subdirectories.

    Names are unique, and so are paths — a configured project that points at a
    `projects_root` subdirectory is listed once, under its configured name.
    """
    candidates: dict[str, Path] = {}
    seen: set[Path] = set()

    def add(name: str, path: Path) -> None:
        key = _resolved(path)
        if name in candidates or key in seen:
            return
        candidates[name] = path
        seen.add(key)

    for name, raw in settings.projects.items():
        add(name, Path(raw).expanduser())

    root = settings.projects_root
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        log.exception("could not list the projects root %s", root)
        entries = []
    for entry in entries:
        if not entry.name.startswith(".") and entry.is_dir():
            add(entry.name, entry)
    return candidates


def discover_briefs(projects: dict[str, Path]) -> list[ProjectBrief]:
    """The brief of every project that wrote one; the rest simply have none."""
    briefs: list[ProjectBrief] = []
    for name, path in projects.items():
        brief = path / BRIEF_FILE
        try:
            if not brief.is_file():
                continue
            text = brief.read_text(encoding="utf-8").strip()
        except OSError:
            log.warning("could not read the brief of %s at %s", name, brief)
            continue
        if not text:
            continue
        if len(text) > MAX_BRIEF_CHARS:
            text = text[: MAX_BRIEF_CHARS - 1].rstrip() + "…"
        briefs.append(ProjectBrief(name=name, text=text))
    return briefs
