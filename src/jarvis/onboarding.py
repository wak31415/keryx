"""`jarvis init`: telling a new Jarvis whom it works for, before its first call.

Every call opens knowing only what is in `memory.md`, and on a fresh machine that is
nothing until an authorized call has ended and the memory update has written something.
`init` is the shortcut: a name, a few facts typed at the keyboard, shown back as the
document a call will read, and written through `continuity.memory.seed_memory`.

It never writes `.env`. Only scripts and people touch that file, so when the name is new
`init` prints the `OWNER_NAME=` line to add instead.

It ends with what a call will carry and what it will not: the projects a task can be pointed
at, which of them wrote a brief, how many characters the briefs and the memory add to
*every* call — both are sent to the realtime provider each time — and the skills. The
subagents' own map of the owner's world is `~/.claude/CLAUDE.md`, which `init` only names.

The terminal comes in as three callables (`echo`, `ask`, `confirm`), so `cli.py` stays
wiring and none of this needs a terminal to test.
"""

import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from jarvis.config import OWNER_FALLBACK, Settings, env_var_name
from jarvis.continuity.memory import (
    MAX_MEMORY_CHARS,
    compose_memory,
    memory_path,
    read_memory,
    seed_memory,
)
from jarvis.projects import BRIEF_FILE, discover_briefs, discover_projects
from jarvis.skills import discover_skills

Echo = Callable[[str], None]
Ask = Callable[[str], str]
Confirm = Callable[[str], bool]

#: Where a subagent learns about the owner's world: the Claude CLI's own user memory,
#: which every subagent reads because it runs the CLI with the user's settings.
SUBAGENT_MEMORY = "~/.claude/CLAUDE.md"

NAME_QUESTION = f'what should Jarvis call you? (blank for "{OWNER_FALLBACK}")'
FACTS_INTRO = (
    "a few things Jarvis should know about you, one per line: what you do, what keeps "
    "coming up, how you like to be answered. a blank line finishes."
)
FACT_PROMPT = "fact"


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


def run_init(
    settings: Settings,
    *,
    name: str | None,
    facts: list[str] | None,
    force: bool,
    yes: bool,
    echo: Echo,
    ask: Ask,
    confirm: Confirm,
) -> int:
    """The whole of `jarvis init`; returns the exit code.

    `facts` is None when none were given on the command line, which is what makes it ask.
    `yes` skips every question: the name falls back to `OWNER_NAME` or `OWNER_FALLBACK`,
    and what is shown is written without a confirmation. Non-zero only when a memory was
    wanted and not written: one is already there, or the facts do not fit in a call.
    """
    owner = _owner_name(settings, name, yes=yes, ask=ask)
    code = _write_memory(
        settings, owner, facts, force=force, yes=yes, echo=echo, ask=ask, confirm=confirm
    )
    if owner and owner != settings.owner_name:
        line = f"{env_var_name('owner_name')}={shlex.quote(owner)}"
        echo(f"\nadd this line to your .env (init never edits it):\n\n    {line}")
    echo("")
    for line in setup_report(settings):
        echo(line)
    return code


def _owner_name(settings: Settings, name: str | None, *, yes: bool, ask: Ask) -> str | None:
    """`--name`, else `OWNER_NAME`, else the answer to a question; None is no name."""
    if name is not None:
        return name.strip() or None
    if settings.owner_name or yes:
        return settings.owner_name
    return ask(NAME_QUESTION).strip() or None


def _write_memory(
    settings: Settings,
    owner: str | None,
    facts: list[str] | None,
    *,
    force: bool,
    yes: bool,
    echo: Echo,
    ask: Ask,
    confirm: Confirm,
) -> int:
    """Collect the facts, show the document, and seed it. Returns the exit code."""
    path = memory_path(settings.data_dir)
    existing = read_memory(settings.data_dir)
    if existing and not force:
        echo(f"Jarvis already remembers something ({len(existing)} characters in {path}).")
        echo("`jarvis memory` shows it; `jarvis init --force` replaces it.")
        return 1

    if facts is None:
        facts = [] if yes else _ask_for_facts(echo, ask)
    label = owner or OWNER_FALLBACK
    text = compose_memory(label, facts)
    if text == compose_memory(label, []):
        echo(f"nothing to write down, so {path} is left as it is.")
        return 0
    if len(text) > MAX_MEMORY_CHARS:
        echo(
            f"that comes to {len(text)} characters, and a call reads {MAX_MEMORY_CHARS}: "
            "shorten it and run init again."
        )
        return 1

    echo(
        f"\nthis is what {path} will say. it is sent to the realtime provider at the top of "
        "every call:\n"
    )
    echo(text)
    if not yes and not confirm("write it?"):
        echo("nothing written.")
        return 0
    if not seed_memory(settings.data_dir, owner=label, facts=facts, force=force):
        echo(f"{path} was written while you were typing, so it was left alone.")
        return 1
    echo(f"wrote {path} (readable by you alone).")
    return 0


def _ask_for_facts(echo: Echo, ask: Ask) -> list[str]:
    echo(FACTS_INTRO)
    facts: list[str] = []
    while line := ask(FACT_PROMPT).strip():
        facts.append(line)
    return facts


def setup_report(settings: Settings) -> list[str]:
    """What a call will carry, and what a subagent reads instead, as printable lines."""
    root = settings.projects_root
    projects = discover_projects(settings)
    briefs = discover_briefs(projects)
    memory = read_memory(settings.data_dir)
    brief_chars = sum(len(brief.text) for brief in briefs)
    skills = discover_skills(settings.skills_dir)

    where = "" if root.is_dir() else f" does not exist (set {env_var_name('projects_root')})"
    return [
        f"projects root: {root}{where}",
        f"projects: {len(projects)}, and {len(briefs)} with a {BRIEF_FILE}"
        + (f": {', '.join(brief.name for brief in briefs)}" if briefs else ""),
        f"sent to the realtime provider on every call: {brief_chars + len(memory)} characters "
        f"({brief_chars} of briefs, {len(memory)} of memory)",
        f"skills: {len(skills)} in {settings.skills_dir}",
        f"subagents read {SUBAGENT_MEMORY}, so their map of your world belongs there.",
    ]
