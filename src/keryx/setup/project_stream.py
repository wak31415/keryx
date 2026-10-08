"""Project context for the Mac app: the question, the draft as JSON lines, and what is kept.

The wizard's section (`setup.project_context`) asks whether to explore, runs one read-only
task, then shows each draft to be accepted, edited or dropped. The app does the same in
three calls, each through the section's own code:

- `describe` — what the section says and asks (`keryx setup project-context --describe`).
- `stream` — the task, run as the wizard runs it (`setup.agents.run_task`, as the service),
  told as `step` events — `dispatch`, `working` as the agent reports progress, `reading` —
  then one ending: `done` carrying the draft, held to the limits a call reads, `error`, or
  `cancelled`. Nothing is written: the draft is the owner's to review.
- `apply` — what the owner kept, as one JSON object, written as the wizard writes it
  (`keep_projects`, `remember_facts`), the section marked walked.

The task is not a row in `tasks.db`: the wizard's never was. Cancel interrupts it inside its
event loop, whose closing cancels the turn and closes the agent's session before the
`cancelled` ending is told, so nothing is left running.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keryx.agents.base import RunResult
from keryx.agents.registry import BACKENDS
from keryx.child_env import redact_known
from keryx.config import Settings
from keryx.localmodels.pull import EventLines
from keryx.projects import MAX_BRIEF_CHARS, MAX_BRIEFS_CHARS, discover_projects, summaries_dir
from keryx.setup import agents
from keryx.setup.project_context import (
    INTRO,
    MAX_FACTS,
    QUESTION,
    SAFE_NAME,
    Draft,
    _clip,
    _folders_problem,
    _inside,
    build_prompt,
    choices,
    keep_projects,
    parse_draft,
    remember_facts,
)

#: The section's id in `[setup] walked` and `keryx setup plan`.
SECTION = "projects"
#: The hint on "Yes", for a person: the wizard's names the home directory.
EXPLORE_HINT = "it finds them, starting from your home folder"
#: The most a `working` step's detail carries of one line of the agent's progress.
MAX_DETAIL_CHARS = 300
#: The fewest seconds between two `working` steps, so a busy agent is not a flood.
WORKING_EVERY_S = 1.0
WHERE = "Say where to look: everywhere from your home folder, or only in folders you name."
NOT_FOLDERS = "One of those is not a folder on this Mac."


def describe(settings: Settings) -> dict[str, Any]:
    """What `keryx setup project-context --describe --json` prints: the section's words,
    its question and choices, what is kept already, and the limits a call reads."""
    label = BACKENDS[settings.agent_backend].spoken_name
    kept = summaries_dir(settings)
    return {
        "section": SECTION,
        "title": "Project context",
        "agent": settings.agent_backend,
        "agent_name": label,
        "note": INTRO.format(label=label),
        "question": QUESTION.format(label=label),
        "choices": [{"value": choice.value, "label": choice.label, "hint": choice.hint or None}
                    for choice in choices(EXPLORE_HINT)],
        "home": str(Path.home()),
        "summaries": sorted(path.stem for path in kept.glob("*.md")) if kept.is_dir() else [],
        "projects": sorted(discover_projects(settings)),
        "max_brief_chars": MAX_BRIEF_CHARS,
        "max_briefs_chars": MAX_BRIEFS_CHARS,
        "max_facts": MAX_FACTS,
    }


class ProjectSteps(EventLines):
    """For the Mac app: `step` events (`step`, `message`, `detail`), then exactly one of
    `done` (the draft), `error`, `cancelled`."""

    def __init__(self, write: Callable[[str], None], **kwargs: Any) -> None:
        super().__init__(write, **kwargs)
        self._working_at: float | None = None

    def step(self, step: str, message: str, *, detail: str | None = None) -> None:
        self._emit({"event": "step", "step": step, "message": message, "detail": detail})

    def working(self, message: str, detail: str) -> None:
        """A line of the agent's progress, at most every `WORKING_EVERY_S`."""
        now = self._clock()
        if self._working_at is not None and now - self._working_at < WORKING_EVERY_S:
            return
        self._working_at = now
        self.step("working", message, detail=detail)

    def done(self, settings: Settings, label: str, draft: Draft, folders: list[Path]) -> None:
        known = discover_projects(settings)
        self._end({
            "event": "done",
            "summary": draft_summary(label, draft),
            "projects": [
                {"name": name, "path": path or None, "summary": summary,
                 "known": name in known,
                 "adds_project": name not in known and _inside(path, folders) is not None}
                for name, path, summary in draft.projects
            ],
            "facts": list(draft.facts),
            "dropped": list(draft.dropped),
            "folders": [str(folder) for folder in folders],
        })


def draft_summary(label: str, draft: Draft) -> str:
    """What came back, in a sentence for a person."""
    if not draft.projects and not draft.facts:
        return f"{label} found nothing to summarise."
    projects = len(draft.projects)
    parts = [f"{projects} project {'summary' if projects == 1 else 'summaries'}"]
    if draft.facts:
        parts.append(f"{len(draft.facts)} {'thing' if len(draft.facts) == 1 else 'things'} "
                     "about you")
    return f"{label} drafted {' and '.join(parts)} for you to check."


def folders_for(explore: bool, named: list[str]) -> tuple[list[Path] | None, str | None]:
    """Where to look — home when exploring, else the folders named — or None and why not:
    one of the two, never both and never neither, as the wizard assumes no answer."""
    if explore == bool(named):
        return None, None
    if explore:
        return [Path.home()], None
    raw = ",".join(named)
    if problem := _folders_problem(raw):
        return None, problem
    return [Path(part.strip()).expanduser() for part in named if part.strip()], None


def stream(
    settings: Settings,
    events: ProjectSteps,
    folders: list[Path],
    *,
    exploring: bool,
    run: Callable[..., Any] | None = None,
) -> int:
    """The section's task, told to `events`; the exit code: 0 with the draft, 1 when the
    agent could not finish or its answer could not be read."""
    agent = settings.agent_backend
    label = BACKENDS[agent].spoken_name
    run = run or agents.run_task
    working = f"{label} is reading your projects."

    def progress(text: str) -> None:
        line = " ".join(redact_known(text, settings).split())
        events.working(working, _clip(line, MAX_DETAIL_CHARS))

    events.step("dispatch", f"Asking {label} to read your projects. This can take a few "
                "minutes.")
    prompt = build_prompt(settings.owner_label, folders, exploring=exploring)
    result: RunResult = asyncio.run(run(settings, agent, prompt, on_progress=progress))
    if not result.ok:
        events.error(f"{label} could not finish reading your projects.",
                     detail=redact_known(result.error or "no answer", settings))
        return 1
    events.step("reading", f"Reading what {label} wrote.")
    try:
        draft = parse_draft(result.final_text)
    except ValueError as exc:
        events.error(f"{label}'s answer could not be read.", detail=str(exc))
        return 1
    events.done(settings, label, draft, folders)
    return 0


def apply(
    settings: Settings, kept: dict[str, Any], *, save: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """What the owner kept of a draft — `{"folders", "projects", "facts"}` — written as the
    wizard writes what was accepted. An entry that is not one is a problem, and then nothing
    is written; a memory too full for the facts is a problem after the summaries are."""
    problems = _kept_problems(kept)
    written: list[str] = []
    added: list[str] = []
    facts: list[str] = []
    applied = not problems
    if applied:
        folders = [Path(folder).expanduser() for folder in kept.get("folders") or []]
        projects = [
            (item["name"], str(item.get("path") or ""), _clip(item["summary"].strip(),
                                                              MAX_BRIEF_CHARS))
            for item in kept.get("projects") or []
        ]
        written, added = keep_projects(settings, projects, folders, save=save)
        facts = [" ".join(fact.split()) for fact in kept.get("facts") or [] if fact.strip()]
        if facts and (problem := remember_facts(settings, facts)):
            problems.append({"id": "facts", "message": _sentence(problem)})
            facts = []
    return {"kept": written, "added_projects": added, "facts_added": len(facts),
            "problems": problems, "applied": applied}


def _sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def _kept_problems(kept: dict[str, Any]) -> list[dict[str, str]]:
    problems = []

    def problem(id: str, message: str) -> None:
        problems.append({"id": id, "message": message})

    for key in kept:
        if key not in ("folders", "projects", "facts"):
            problem(str(key), "That is not part of a draft.")
    folders = kept.get("folders", [])
    if not (isinstance(folders, list) and all(isinstance(f, str) for f in folders)):
        problem("folders", "Give the folders the draft was made from.")
    facts = kept.get("facts", [])
    if not (isinstance(facts, list) and all(isinstance(f, str) for f in facts)):
        problem("facts", "Give each fact as a line of its own.")
    elif len(facts) > MAX_FACTS:
        problem("facts", f"Keep at most {MAX_FACTS} facts.")
    projects = kept.get("projects", [])
    if not isinstance(projects, list):
        problem("projects", "Give the projects as a list.")
        return problems
    seen: set[str] = set()
    for item in projects:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name) or name in seen:
            problem("projects", "A project needs a name of its own, in letters, digits, dots, "
                    "dashes and underscores.")
            continue
        seen.add(name)
        summary, path = item.get("summary"), item.get("path")
        if not isinstance(summary, str) or not summary.strip():
            problem(f"projects.{name}", "Say what the project is, or leave it out.")
        elif path is not None and not isinstance(path, str):
            problem(f"projects.{name}", "Give the project's folder as the draft did.")
    return problems
