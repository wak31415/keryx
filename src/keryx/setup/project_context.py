"""Project context: a coding agent drafts a summary of each project, and the owner decides.

A call carries a short brief per project — what it is, what state it is in, what its words
mean out loud — so that "how is the orchard thing going" means something to the voice
model. Writing twenty of those by hand is the step nobody does, so this section asks first
— explore, explore only the folders the owner names, or not at all, with no answer assumed —
then sends one read-only task through the same runner a call would
(`prompts/setup_project_context.md`), and shows each draft to be accepted, edited or
dropped. Nothing is kept that the owner did not accept. Exploring starts from the home
directory and leaves the agent to find where the projects are; nothing about this
machine's layout is assumed.

Accepted summaries go to `DATA_DIR/projects/<name>.md` (0600), which
`projects.discover_briefs` reads for any project without a `.keryx-brief.md` of its own;
a project Keryx does not already know is added to `PROJECTS` so it can be named on a call —
but only one inside the folders it was allowed to read, and none under a hidden directory.
Accepted facts go into the memory's standing facts. The limits are the ones a call reads —
`MAX_BRIEF_CHARS` each, `MAX_BRIEFS_CHARS` in all — enforced here as well as asked for.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from keryx.agents.registry import BACKENDS
from keryx.config import write_private
from keryx.continuity.memory import add_standing_facts
from keryx.projects import MAX_BRIEF_CHARS, MAX_BRIEFS_CHARS, discover_projects, summaries_dir
from keryx.prompts import render_prompt
from keryx.setup.context import SetupContext
from keryx.setup.ui import Choice

PROMPT = "setup_project_context.md"
MAX_FACTS = 5
#: A summary's file name is its project's name, so it has to be a safe one.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass
class Draft:
    """What the agent came back with, already held to the limits."""

    facts: list[str] = field(default_factory=list)
    projects: list[tuple[str, str, str]] = field(default_factory=list)  # name, path, summary
    dropped: list[str] = field(default_factory=list)


#: Where a project is, said to the agent: in folders the owner named, or anywhere it finds.
NAMED_FOLDERS = (
    "A project is a directory directly inside one of those folders (or the folder itself, "
    "when it is a repository)."
)
EXPLORING = (
    "Those are where to start, not a list of projects: find where {owner} keeps their work "
    "— usually a folder like `projects`, `code`, `src` or `repos`, or repositories directly "
    "inside — and treat each directory there as a project. Look a few levels down at most, "
    "and stay out of system directories and other people's files."
)


def build_prompt(owner: str, folders: list[Path], *, exploring: bool = False) -> str:
    return render_prompt(
        PROMPT,
        owner=owner,
        folders="\n".join(f"- {folder}" for folder in folders),
        where=(EXPLORING.format(owner=owner) if exploring else NAMED_FOLDERS),
        max_brief=str(MAX_BRIEF_CHARS),
        max_total=str(MAX_BRIEFS_CHARS),
    )


def parse_draft(text: str) -> Draft:
    """The JSON object in the agent's answer, held to the limits a call reads.

    Tolerant of a fence or a sentence around it — the first `{` that starts a whole JSON
    object is the one — and strict about everything in it: a project with no usable name or
    summary is dropped, a summary past `MAX_BRIEF_CHARS` is cut at a word, and projects past
    `MAX_BRIEFS_CHARS` in all are dropped whole and named.
    """
    data = _first_object(text.split("SPOKEN_SUMMARY:")[0])
    draft = Draft()
    facts = data.get("facts") if isinstance(data.get("facts"), list) else []
    draft.facts = [" ".join(str(fact).split()) for fact in facts if str(fact).strip()][:MAX_FACTS]
    total = 0
    seen: set[str] = set()
    for item in data.get("projects") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not SAFE_NAME.fullmatch(name) or not summary or name in seen:
            continue
        summary = _clip(summary, MAX_BRIEF_CHARS)
        if total + len(summary) > MAX_BRIEFS_CHARS:
            draft.dropped.append(name)
            continue
        seen.add(name)
        total += len(summary)
        draft.projects.append((name, str(item.get("path") or "").strip(), summary))
    return draft


def _first_object(text: str) -> dict:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("the answer had no JSON object in it")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def run_section(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    agent = settings.agent_backend
    label = BACKENDS[agent].spoken_name
    ui.note(
        "A call carries a short brief of each project, so Keryx knows what you mean by "
        f"name. {label} can explore your projects and draft them for you to check: it is "
        "asked to read only and to skip anything secret, and nothing is kept until you "
        "accept it."
    )
    home = Path.home()
    how = ui.select(
        f"Let {label} explore your projects?",
        [
            Choice("explore", "Yes", hint=f"it finds them, starting from {home}"),
            Choice("folders", "Yes, only in folders I name"),
            Choice("no", "No"),
        ],
    )
    if how == "no":
        return
    folders = [home] if how == "explore" else _ask_folders(ctx)
    prompt = build_prompt(settings.owner_label, folders, exploring=how == "explore")
    with ui.spinner(f"{label} is reading your projects — this can take a few minutes…"):
        result = asyncio.run(ctx.probes.run_task(settings, agent, prompt))
    if not result.ok:
        ui.error(f"{label} could not finish: {result.error or 'no answer'}")
        return
    try:
        draft = parse_draft(result.final_text)
    except ValueError as exc:
        ui.error(f"{label}'s answer could not be read: {exc}")
        return
    if draft.dropped:
        ui.warn(f"Left out, past {MAX_BRIEFS_CHARS} characters in all: {', '.join(draft.dropped)}")
    _review_projects(ctx, draft, folders)
    _review_facts(ctx, draft)


def _ask_folders(ctx: SetupContext) -> list[Path]:
    raw = ctx.ui.text("Folders to look through, comma-separated", validate=_folders_problem)
    return _folders(raw)


def _folders(raw: str) -> list[Path]:
    return [Path(part.strip()).expanduser() for part in raw.split(",") if part.strip()]


def _folders_problem(raw: str) -> str | None:
    folders = _folders(raw)
    if not folders:
        return "Name at least one folder (Esc goes back)."
    if missing := [str(folder) for folder in folders if not folder.is_dir()]:
        return f"Not a folder: {', '.join(missing)}"
    return None


def _inside(path: str, folders: list[Path]) -> Path | None:
    """`path` resolved, when it is a directory inside one of `folders` and not under a
    hidden one (`~/.ssh` is nobody's project); else None."""
    if not path:
        return None
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return None
    for folder in folders:
        base = folder.expanduser().resolve()
        if resolved.is_dir() and resolved.is_relative_to(base):
            hidden = any(part.startswith(".") for part in resolved.relative_to(base).parts)
            return None if hidden else resolved
    return None


def _review_projects(ctx: SetupContext, draft: Draft, folders: list[Path]) -> None:
    ui, settings = ctx.ui, ctx.settings
    known = discover_projects(settings)
    kept = 0
    new_projects = dict(settings.projects)
    for name, path, summary in draft.projects:
        inside = _inside(path, folders)
        ui.panel(f"{name} — {path or 'no path given'}", summary)
        choice = ui.select(
            f"Keep the summary of {name}?",
            [Choice("accept", "Accept"), Choice("edit", "Edit"), Choice("drop", "Drop")],
            default="accept",
        )
        if choice == "drop":
            continue
        if choice == "edit":
            summary = _clip(ui.text("Summary", default=summary, multiline=True), MAX_BRIEF_CHARS)
            if not summary:
                continue
        write_private(summaries_dir(settings) / f"{name}.md", summary + "\n")
        kept += 1
        # Only a folder they chose to have read becomes a project by name: a path from
        # anywhere else is what a prompt injected into a README would ask for, and projects
        # widen where a keypad approval may write (`approvals.policy`).
        if name not in known and inside is not None:
            new_projects[name] = str(inside)
    if new_projects != settings.projects:
        ctx.save({"PROJECTS": json.dumps(new_projects)})
    if kept:
        ui.success(f"{kept} project summaries kept in {summaries_dir(settings)}")


def _review_facts(ctx: SetupContext, draft: Draft) -> None:
    if not draft.facts:
        return
    ui, settings = ctx.ui, ctx.settings
    keep = ui.checkbox(
        "Which of these should Keryx remember about you?",
        [Choice(str(index), fact, checked=True) for index, fact in enumerate(draft.facts)],
    )
    facts = [draft.facts[int(index)] for index in keep]
    if not facts:
        return
    try:
        add_standing_facts(
            settings.data_dir,
            owner=settings.owner_label,
            assistant=settings.assistant_name,
            facts=facts,
        )
    except ValueError as exc:
        ui.error(str(exc))
        return
    ui.success(f"{len(facts)} facts added to the memory")
