"""Which repositories a coding task may be pointed at.

Both the `TaskManager` (which resolves a spoken name to a working directory) and the
voice prompt (which tells the model what names exist) have to agree on the answer, so
the discovery lives here rather than inside either of them.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config import Settings

log = logging.getLogger("jarvis.projects")


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
