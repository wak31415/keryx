"""Project context: a coding agent drafts a summary of each project, and the owner decides.

A call carries a short brief per project — what it is, what state it is in, what its words
mean out loud — so that "how is the orchard thing going" means something to the voice
model. Writing twenty of those by hand is the step nobody does, so this section asks first
("May Claude look through these folders…?"), then sends one read-only task through the
same runner a call would (`prompts/setup_project_context.md`), and shows each draft to be
accepted, edited or dropped. Nothing is kept that the owner did not accept.

Accepted summaries go to `DATA_DIR/projects/<name>.md` (0600), which
`projects.discover_briefs` reads for any project without a `.jarvis-brief.md` of its own;
a project found outside `PROJECTS_ROOT` is added to `PROJECTS` so it can be named on a call.
Accepted facts go into the memory's standing facts. The limits are the ones a call reads —
`MAX_BRIEF_CHARS` each, `MAX_BRIEFS_CHARS` in all — enforced here as well as asked for.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.agents.registry import BACKENDS
from jarvis.config import write_private
from jarvis.continuity.memory import add_standing_facts
from jarvis.projects import MAX_BRIEF_CHARS, MAX_BRIEFS_CHARS, discover_projects, summaries_dir
from jarvis.prompts import render_prompt
from jarvis.setup.context import SetupContext
from jarvis.setup.ui import Choice

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


def build_prompt(owner: str, folders: list[Path]) -> str:
    return render_prompt(
        PROMPT,
        owner=owner,
        folders="\n".join(f"- {folder}" for folder in folders),
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
        "A call carries a short brief of each project, so Jarvis knows what you mean by "
        "name. An agent can draft them for you to check."
    )
    default = str(settings.projects_root) if settings.projects_root.is_dir() else ""
    raw = ui.text("Folders to look through, comma-separated (blank to skip)", default=default)
    folders = [Path(part.strip()).expanduser() for part in raw.split(",") if part.strip()]
    missing = [str(folder) for folder in folders if not folder.is_dir()]
    if missing:
        ui.error(f"Not a folder: {', '.join(missing)}")
        return
    if not folders:
        return
    if not ui.confirm(
        f"May {label} look through these folders to summarise your projects? It reads only, "
        "and skips anything secret.",
        default=True,
    ):
        return
    with ui.spinner(f"{label} is reading your projects — this can take a few minutes…"):
        result = asyncio.run(
            ctx.probes.run_task(settings, agent, build_prompt(settings.owner_label, folders))
        )
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
    _review_projects(ctx, draft)
    _review_facts(ctx, draft)


def _review_projects(ctx: SetupContext, draft: Draft) -> None:
    ui, settings = ctx.ui, ctx.settings
    known = discover_projects(settings)
    kept = 0
    new_projects = dict(settings.projects)
    for name, path, summary in draft.projects:
        ui.panel(name, summary)
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
        if name not in known and path and Path(path).is_dir():
            new_projects[name] = path
    if new_projects != settings.projects:
        ctx.save({"PROJECTS": json.dumps(new_projects)})
    if kept:
        ui.success(f"{kept} project summaries kept in {summaries_dir(settings)}")


def _review_facts(ctx: SetupContext, draft: Draft) -> None:
    if not draft.facts:
        return
    ui, settings = ctx.ui, ctx.settings
    keep = ui.checkbox(
        "Which of these should Jarvis remember about you?",
        [Choice(str(index), fact, checked=True) for index, fact in enumerate(draft.facts)],
    )
    facts = [draft.facts[int(index)] for index in keep]
    if not facts:
        return
    try:
        add_standing_facts(settings.data_dir, owner=settings.owner_label, facts=facts)
    except ValueError as exc:
        ui.error(str(exc))
        return
    ui.success(f"{len(facts)} facts added to the memory")
