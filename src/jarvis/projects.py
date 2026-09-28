"""Which repositories a task may be pointed at.

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
#: Where `jarvis setup` keeps the summaries it drafted, one `<project>.md` each, under
#: `data_dir`: for the projects whose repository carries no brief of its own. A repository's
#: own `BRIEF_FILE` always wins — it travels with the code and is somebody's decision.
SUMMARIES_DIR = "projects"
MAX_BRIEF_CHARS = 1500
#: All the briefs together. They ride in the system prompt of every call, re-sent to the
#: realtime provider each time, so a projects root with forty briefed repositories would
#: otherwise make every call carry forty of them. Four full briefs: one and a half times
#: what the memory may add (`continuity.memory.MAX_MEMORY_CHARS`), which is the other thing
#: every call carries — the prompt stays a receptionist's notes, not a filing cabinet.
MAX_BRIEFS_CHARS = 4 * MAX_BRIEF_CHARS


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


def summaries_dir(settings: "Settings") -> Path:
    """`data_dir/projects`, where setup's project summaries live."""
    return settings.data_dir / SUMMARIES_DIR


def discover_briefs(
    projects: dict[str, Path], *, summaries: Path | None = None
) -> list[ProjectBrief]:
    """The brief of every project that has one; the rest simply have none.

    A project's brief is its own `BRIEF_FILE`, else `summaries/<name>.md` when a directory
    of summaries is given.

    Taken in discovery order (configured projects first) until `MAX_BRIEFS_CHARS`: a brief
    that would go past it is left out whole, and named in a warning, rather than cut off
    mid-sentence — and a shorter one after it still gets in.
    """
    briefs: list[ProjectBrief] = []
    left_out: list[str] = []
    total = 0
    for name, path in projects.items():
        brief = path / BRIEF_FILE
        if not brief.is_file() and summaries is not None:
            brief = summaries / f"{name}.md"
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
        if total + len(text) > MAX_BRIEFS_CHARS:
            left_out.append(name)
            continue
        total += len(text)
        briefs.append(ProjectBrief(name=name, text=text))
    if left_out:
        log.warning(
            "left %d project brief(s) out of the prompt, past %d characters in all: %s",
            len(left_out),
            MAX_BRIEFS_CHARS,
            ", ".join(left_out),
        )
    return briefs
