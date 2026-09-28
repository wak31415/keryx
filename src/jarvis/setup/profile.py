"""Telling a new Jarvis whom it works for, before its first call.

Every call opens knowing only what is in `memory.md`, and on a fresh machine that is
nothing until an authorized call has ended and the memory update has written something.
This is the shortcut, in two shapes:

- **`jarvis setup`'s "About you" section** (`run_section`): what Jarvis will mostly be
  used for and a few standing facts, shown back as the document a call will read, and
  written through `continuity.memory.seed_memory` — so `memory_skeleton` stays the only
  place the document's structure is written down.
- **`jarvis memory seed --file FILE|-`** (`seed`): the same, from a file or stdin, for a
  coding agent setting Jarvis up. `--json` prints `setup_summary`, and the exit code is the
  contract (`STATUSES`).

`setup_report` and `setup_summary` say what a call will carry and what it will not: the
projects a task can be pointed at, which of them have a brief, how many characters the
briefs and the memory add to *every* call — both are sent to the realtime provider each
time — and the skills. The subagents' own map of the owner's world is each enabled agent's
instructions file (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`), which this only names.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis.agents.registry import BACKENDS, skill_dirs
from jarvis.config import OWNER_FALLBACK, PIN_FROM_ENV, PIN_FROM_FILE, Settings, pin_file
from jarvis.continuity.memory import (
    MAX_MEMORY_CHARS,
    compose_memory,
    memory_path,
    read_memory,
    seed_memory,
)
from jarvis.projects import (
    BRIEF_FILE,
    MAX_BRIEFS_CHARS,
    ProjectBrief,
    discover_briefs,
    discover_projects,
    summaries_dir,
)
from jarvis.setup.context import SetupContext
from jarvis.setup.ui import Choice
from jarvis.skills import Skill, discover_skills_in

Echo = Callable[[str], None]

#: What became of the memory. It is what `--json` reports as `status`, and the exit code
#: is a function of it: `_REFUSED` is exit 1 — a memory was wanted and not written — and
#: everything else is exit 0, because reading the report is not a failure.
STATUSES = {
    "written": "the memory was written",
    "unchanged": "nothing was given to write down, so the memory was left as it is",
    "declined": "the memory was shown and not confirmed",
    "exists": "a memory is already there; --force replaces it",
    "too_long": f"that memory is longer than the {MAX_MEMORY_CHARS} characters a call reads",
    "raced": "something else wrote the memory first",
}
_REFUSED = frozenset({"exists", "too_long", "raced"})

#: One line about the PIN per source. Never the digits: this output is pasted into
#: terminals and logs. Each one stands on its own, because a document has no "the line
#: above" to point at.
PIN_NOTES = {
    PIN_FROM_ENV: "set from JARVIS_PIN in the environment, which wins over DATA_DIR/pin",
    PIN_FROM_FILE: "set, and kept in DATA_DIR/pin",
    None: (
        "not set — `jarvis setup` asks for one at the keyboard, or the first call can key "
        "one in; until a PIN exists, nothing of yours is read out on the phone and no task "
        "can be dispatched"
    ),
}
#: The fourth state, and the one only the keyboard clears: a `data_dir/pin` that is not
#: 6-8 digits is no PIN *and* no enrolment, because `O_EXCL` will not replace a file that
#: is there. Sending them to the first call instead would be sending them nowhere.
PIN_SEALED_NOTE = (
    "not set, and no call can set one — {path} is not 6 to 8 digits; `jarvis setup` "
    "replaces it"
)

#: What "What will you mostly use Jarvis for?" offers; each becomes a standing fact.
USES = {
    "email": "email triage",
    "coding": "coding tasks in their repositories",
    "calendar": "their calendar",
    "research": "research and reading",
    "cluster": "jobs on the compute cluster",
}


def subagent_memories(settings: Settings) -> dict[str, str]:
    """Where each enabled agent's subagents learn about the owner's world, the default first."""
    home = str(Path.home())
    return {
        name: str(BACKENDS[name].instructions_file()).replace(home, "~", 1)
        for name in settings.enabled_agents
    }


def pin_note(settings: Settings) -> str:
    """The one line about this machine's PIN: where it came from, or what to do about it."""
    if settings.pin_source is not None:
        return PIN_NOTES[settings.pin_source]
    if settings.pin_enrolment_open:
        return PIN_NOTES[None]
    return PIN_SEALED_NOTE.format(path=pin_file(settings.data_dir))


def facts_from_text(text: str) -> list[str]:
    """One fact per non-blank line; a markdown heading is not a fact."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def read_facts(source: str) -> list[str]:
    """The facts in the file at `source`, or on standard input for `-`."""
    if source == "-":
        return facts_from_text(sys.stdin.read())
    return facts_from_text(Path(source).expanduser().read_text(encoding="utf-8"))


def exit_code(status: str) -> int:
    """1 when a memory was wanted and not written, 0 otherwise. See `STATUSES`."""
    return 1 if status in _REFUSED else 0


def seed(settings: Settings, facts: list[str], *, force: bool, echo: Echo) -> str:
    """Write a first memory from `facts`, without asking. Returns a `STATUSES` key.

    Nothing given is `unchanged` even when a memory is already there: nothing was wanted,
    so nothing was refused — which is how an agent asks what a call will carry.
    """
    path = memory_path(settings.data_dir)
    owner = settings.owner_label
    if compose_memory(owner, facts) == compose_memory(owner, []):
        echo(f"nothing to write down, so {path} is left as it is.")
        return "unchanged"
    existing = read_memory(settings.data_dir)
    if existing and not force:
        echo(f"Jarvis already remembers something ({len(existing)} characters in {path}).")
        echo("`jarvis memory` shows it; `jarvis memory seed --force` replaces it.")
        return "exists"
    try:
        written = seed_memory(settings.data_dir, owner=owner, facts=facts, force=force)
    except ValueError as exc:
        echo(f"{exc}: shorten it and try again.")
        return "too_long"
    if not written:
        echo(f"{path} was written while this ran, so it was left alone.")
        return "raced"
    echo(f"wrote {path} (readable by you alone).")
    return "written"


# --- the wizard section ------------------------------------------------------------------


def run_section(ctx: SetupContext) -> None:
    """What Jarvis should know about its owner from the first call, as a first memory."""
    ui, settings = ctx.ui, ctx.settings
    existing = read_memory(settings.data_dir)
    if existing:
        ui.success(f"Jarvis already remembers {len(existing)} characters about you")
        if not ui.confirm("Replace that memory with a new one?", default=False):
            return
    ui.note(
        "What you say here is sent to the realtime provider at the top of every call. "
        "Nothing you would not send there."
    )
    uses = ui.checkbox(
        "What will you mostly use Jarvis for?",
        [Choice(key, label.replace("their ", "your ")) for key, label in USES.items()],
    )
    other = ui.text("Anything else you will use it for? (optional)")
    facts = []
    if uses or other:
        named = [USES[key] for key in uses] + ([other] if other else [])
        facts.append(f"Mostly uses Jarvis for {', '.join(named)}.")
    ui.note(
        "A few things Jarvis should know about you, one per line — what you do, what keeps "
        "coming up, how you like to be answered. A blank line finishes."
    )
    while line := ui.text("fact"):
        facts.append(line)
    if not facts:
        ui.note("Nothing to write down; the first call will introduce itself instead.")
        return
    text = compose_memory(settings.owner_label, facts)
    if len(text) > MAX_MEMORY_CHARS:
        ui.error(f"That comes to {len(text)} characters, and a call reads {MAX_MEMORY_CHARS}.")
        return
    ui.panel(str(memory_path(settings.data_dir)), text)
    if not ui.confirm("Write it?", default=True):
        return
    if seed_memory(settings.data_dir, owner=settings.owner_label, facts=facts, force=True):
        ui.success("memory written (readable by you alone)")


# --- what a call carries ------------------------------------------------------------------


@dataclass(frozen=True)
class _Inventory:
    """Everything both renderings of the report are counting, discovered once."""

    settings: Settings
    projects: dict[str, Path]
    briefs: list[ProjectBrief]
    memory: str
    skills: list[Skill]

    @property
    def brief_chars(self) -> int:
        return sum(len(brief.text) for brief in self.briefs)

    @property
    def per_call_chars(self) -> int:
        """What the realtime provider is sent at the top of every call, on this machine."""
        return self.brief_chars + len(self.memory)


def _inventory(settings: Settings) -> _Inventory:
    projects = discover_projects(settings)
    return _Inventory(
        settings=settings,
        projects=projects,
        briefs=discover_briefs(projects, summaries=summaries_dir(settings)),
        memory=read_memory(settings.data_dir),
        skills=discover_skills_in(skill_dirs(settings)),
    )


def setup_report(settings: Settings) -> list[str]:
    """What a call will carry, and what a subagent reads instead, as printable lines."""
    found = _inventory(settings)
    root = settings.projects_root
    briefs = found.briefs
    where = (
        ""
        if root.is_dir()
        else f" does not exist, so a task with no project starts in "
        f"{settings.data_dir / 'workspace'} (set PROJECTS_ROOT)"
    )
    return [
        f"projects root: {root}{where}",
        f"projects: {len(found.projects)}, and {len(briefs)} with a brief"
        + (f": {', '.join(brief.name for brief in briefs)}" if briefs else ""),
        f"sent to the realtime provider on every call: {found.per_call_chars} characters "
        f"({found.brief_chars} of briefs, {len(found.memory)} of memory)",
        f"skills: {len(found.skills)} in {', '.join(map(str, skill_dirs(settings)))}",
        f"subagents read {' and '.join(subagent_memories(settings).values())}, so their map "
        "of your world belongs there.",
    ]


def setup_summary(settings: Settings, *, status: str = "unchanged") -> dict:
    """The same report as a JSON-shaped document, for whatever ran `memory seed --json`.

    `brief_chars` is what a call actually carries of that project, so a brief left out for
    being past `MAX_BRIEFS_CHARS` in total counts 0 — the cap is the point of the number.
    `pin` says whether there is one and where it came from, and never what it is.
    """
    found = _inventory(settings)
    written = {brief.name: len(brief.text) for brief in found.briefs}
    memories = subagent_memories(settings)
    return {
        "status": status,
        "owner_name": settings.owner_name or None,
        "owner_name_set": bool(settings.owner_name),
        "pin": {
            "set": bool(settings.pin),
            "source": settings.pin_source,
            "enrolment_open": settings.pin_enrolment_open,
            "path": str(pin_file(settings.data_dir)),
            "note": pin_note(settings),
        },
        "memory": {
            "path": str(memory_path(settings.data_dir)),
            "chars": len(found.memory),
            "max_chars": MAX_MEMORY_CHARS,
            "written": status == "written",
        },
        "projects_root": {
            "path": str(settings.projects_root),
            "exists": settings.projects_root.is_dir(),
        },
        "projects": [
            {"name": name, "brief_chars": written.get(name, 0)} for name in found.projects
        ],
        "briefs": {
            "count": len(found.briefs),
            "chars": found.brief_chars,
            "max_chars": MAX_BRIEFS_CHARS,
            "file": BRIEF_FILE,
            "summaries_dir": str(summaries_dir(settings)),
        },
        "per_call_chars": found.per_call_chars,
        "skills": [
            {"name": skill.name, "description": skill.description} for skill in found.skills
        ],
        "skills_dirs": [str(path) for path in skill_dirs(settings)],
        # The default agent's, under the key it has always had; every agent's beside it.
        "subagent_memory": memories[settings.agent_backend],
        "subagent_memories": memories,
        "owner_fallback": OWNER_FALLBACK,
    }
