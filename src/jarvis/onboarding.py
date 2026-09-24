"""`jarvis init`: telling a new Jarvis whom it works for, before its first call.

Every call opens knowing only what is in `memory.md`, and on a fresh machine that is
nothing until an authorized call has ended and the memory update has written something.
`init` is the shortcut: a name, a few facts typed at the keyboard, shown back as the
document a call will read, and written through `continuity.memory.seed_memory`.

It never writes `.env`. Only scripts and people touch that file, so when the name is new
`init` prints the `OWNER_NAME=` line to add instead — and, where the machine has no PIN,
a `JARVIS_PIN=` line with six random digits beside it. That one is a suggestion and not a
setting: `init` does not enrol a PIN, and the first call can key one in instead.

It ends with what a call will carry and what it will not: the projects a task can be pointed
at, which of them wrote a brief, how many characters the briefs and the memory add to
*every* call — both are sent to the realtime provider each time — and the skills. The
subagents' own map of the owner's world is `~/.claude/CLAUDE.md`, which `init` only names.

A new owner is as likely to point their own coding agent at the repository and say "set
this up", so the same facts come out as a document: `setup_summary` is `setup_report`'s
machine-readable twin, printed by `--json`, and both read one inventory so a person and an
agent are never told different numbers. For an agent the exit code is the contract, and
`STATUSES` says what each one means.

The terminal comes in as three callables (`echo`, `ask`, `confirm`), so `cli.py` stays
wiring and none of this needs a terminal to test.
"""

import json
import secrets
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis.config import (
    OWNER_FALLBACK,
    PIN_FROM_ENV,
    PIN_FROM_FILE,
    Settings,
    env_var_name,
    pin_file,
)
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
)
from jarvis.skills import Skill, discover_skills

Echo = Callable[[str], None]
Ask = Callable[[str], str]
Confirm = Callable[[str], bool]

#: Where a subagent learns about the owner's world: the Claude CLI's own user memory,
#: which every subagent reads because it runs the CLI with the user's settings.
SUBAGENT_MEMORY = "~/.claude/CLAUDE.md"

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

#: One line about the PIN per source, for `--json` (the human run prints the suggested
#: line and the alternative instead). Never the digits: the owner can read their own file,
#: and this output is pasted into terminals and logs. Each one stands on its own, because
#: a document has no "the line above" to point at.
#: The enrolled line says `.env` because the file is a bootstrap, not a home — a subagent
#: runs as the owner and so could delete it, which would re-open enrolment (SECURITY.md).
PIN_NOTES = {
    PIN_FROM_ENV: f"set from {env_var_name('pin')} in your environment",
    PIN_FROM_FILE: (
        f"enrolled on the phone; copy it from the file into .env as {env_var_name('pin')} "
        "to make it permanent"
    ),
    None: (
        f"not set — put {env_var_name('pin')} in your .env, or key one in on the first "
        "call; until a PIN exists, nothing of yours is read out on the phone and no task "
        "can be dispatched"
    ),
}
#: The fourth state, and the one only the keyboard clears: a `data_dir/pin` that is not
#: 6-8 digits is no PIN *and* no enrolment, because `O_EXCL` will not replace a file that
#: is there. Sending them to the first call instead would be sending them nowhere.
PIN_SEALED_NOTE = (
    "not set, and no call can set one — {path} is not 6 to 8 digits; delete that file, or "
    f"put {env_var_name('pin')} in your .env"
)
#: How many digits `init` suggests. Six is the shortest a PIN may be (`config.PIN_RULE`),
#: and the suggestion is there to be typed by a person who did not want to choose one.
SUGGESTED_PIN_DIGITS = 6

NAME_QUESTION = f'what should Jarvis call you? (blank for "{OWNER_FALLBACK}")'
FACTS_INTRO = (
    "a few things Jarvis should know about you, one per line: what you do, what keeps "
    "coming up, how you like to be answered. a blank line finishes."
)
FACT_PROMPT = "fact"


def pin_note(settings: Settings) -> str:
    """The one line about this machine's PIN: where it came from, or what to do about it.

    Four states, the same four `jarvis doctor` reports, and never the digits.
    """
    if settings.pin_source is not None:
        return PIN_NOTES[settings.pin_source]
    if settings.pin_enrolment_open:
        return PIN_NOTES[None]
    return PIN_SEALED_NOTE.format(path=pin_file(settings.data_dir))


def suggested_pin_line() -> str:
    """A `JARVIS_PIN=` line with six cryptographically random digits, to paste or ignore.

    `secrets`, not `random`: this is the one thing between somebody who has spoofed a
    caller id and a subagent running with the owner's full access. `init` prints it and
    nothing else — it never writes `.env`, and it never sets a PIN itself.
    """
    digits = "".join(str(secrets.randbelow(10)) for _ in range(SUGGESTED_PIN_DIGITS))
    return f"{env_var_name('pin')}={digits}"


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
    as_json: bool = False,
) -> int:
    """The whole of `jarvis init`; returns the exit code.

    `facts` is None when none were given on the command line, which is what makes it ask.
    `yes` skips every question: the name falls back to `OWNER_NAME` or `OWNER_FALLBACK`,
    and what is shown is written without a confirmation. Non-zero only when a memory was
    wanted and not written: one is already there, or the facts do not fit in a call.

    `as_json` prints the same facts as one JSON document and nothing else, for the agent
    that was pointed at this repository and told to set it up. The narration is not
    rerouted, it is dropped: the status says what became of the memory, and half a report
    on stdout would only break the parse.
    """
    owner = _owner_name(settings, name, yes=yes, ask=ask)
    status = _write_memory(
        settings,
        owner,
        facts,
        force=force,
        yes=yes,
        echo=(lambda _line: None) if as_json else echo,
        ask=ask,
        confirm=confirm,
    )
    line = (
        f"{env_var_name('owner_name')}={shlex.quote(owner)}"
        if owner and owner != settings.owner_name
        else None
    )
    if as_json:
        echo(json.dumps(setup_summary(settings, status=status, env_line=line), indent=2))
        return exit_code(status)
    # The PIN is the one setup step the phone cannot do for them, so it is offered here
    # first: a suggestion to paste, and — only where the phone could still do it — the
    # alternative in one line.
    pin_line = None if settings.pin else suggested_pin_line()
    if lines := [one for one in (line, pin_line) if one]:
        label = "this line" if len(lines) == 1 else "these lines"
        echo(f"\nadd {label} to your .env (init never edits it):\n")
        for one in lines:
            echo(f"    {one}")
        if settings.pin_enrolment_open:
            echo(
                "\nor key one in on the first call, which can set the PIN while there is "
                "none — nothing of yours is read out until there is."
            )
    echo("")
    for report_line in setup_report(settings):
        echo(report_line)
    return exit_code(status)


def exit_code(status: str) -> int:
    """1 when a memory was wanted and not written, 0 otherwise. See `STATUSES`."""
    return 1 if status in _REFUSED else 0


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
) -> str:
    """Collect the facts, show the document, and seed it. Returns a `STATUSES` key.

    A run that was never going to write anything — `--yes` with no facts, which is how an
    agent asks what a call will carry — is `unchanged` even when a memory is already there:
    nothing was wanted, so nothing was refused.
    """
    path = memory_path(settings.data_dir)
    existing = read_memory(settings.data_dir)
    asking = facts is None and not yes
    if existing and not force and (asking or facts):
        echo(f"Jarvis already remembers something ({len(existing)} characters in {path}).")
        echo("`jarvis memory` shows it; `jarvis init --force` replaces it.")
        return "exists"

    if facts is None:
        facts = _ask_for_facts(echo, ask) if asking else []
    label = owner or OWNER_FALLBACK
    text = compose_memory(label, facts)
    if text == compose_memory(label, []):
        echo(f"nothing to write down, so {path} is left as it is.")
        return "unchanged"
    if len(text) > MAX_MEMORY_CHARS:
        echo(
            f"that comes to {len(text)} characters, and a call reads {MAX_MEMORY_CHARS}: "
            "shorten it and run init again."
        )
        return "too_long"

    echo(
        f"\nthis is what {path} will say. it is sent to the realtime provider at the top of "
        "every call:\n"
    )
    echo(text)
    if not yes and not confirm("write it?"):
        echo("nothing written.")
        return "declined"
    if not seed_memory(settings.data_dir, owner=label, facts=facts, force=force):
        echo(f"{path} was written while you were typing, so it was left alone.")
        return "raced"
    echo(f"wrote {path} (readable by you alone).")
    return "written"


def _ask_for_facts(echo: Echo, ask: Ask) -> list[str]:
    echo(FACTS_INTRO)
    facts: list[str] = []
    while line := ask(FACT_PROMPT).strip():
        facts.append(line)
    return facts


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
        briefs=discover_briefs(projects),
        memory=read_memory(settings.data_dir),
        skills=discover_skills(settings.skills_dir),
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
        f"{settings.data_dir / 'workspace'} (set {env_var_name('projects_root')})"
    )
    return [
        f"projects root: {root}{where}",
        f"projects: {len(found.projects)}, and {len(briefs)} with a {BRIEF_FILE}"
        + (f": {', '.join(brief.name for brief in briefs)}" if briefs else ""),
        f"sent to the realtime provider on every call: {found.per_call_chars} characters "
        f"({found.brief_chars} of briefs, {len(found.memory)} of memory)",
        f"skills: {len(found.skills)} in {settings.skills_dir}",
        f"subagents read {SUBAGENT_MEMORY}, so their map of your world belongs there.",
    ]


def setup_summary(
    settings: Settings, *, status: str = "unchanged", env_line: str | None = None
) -> dict:
    """The same report as a JSON-shaped document, for whatever ran `init --json`.

    `brief_chars` is what a call actually carries of that project, so a brief left out for
    being past `MAX_BRIEFS_CHARS` in total counts 0 — the cap is the point of the number.

    `pin` says whether there is one and where it came from, and never what it is: this
    document is printed into terminals and logs by whatever ran `init`.
    """
    found = _inventory(settings)
    written = {brief.name: len(brief.text) for brief in found.briefs}
    return {
        "status": status,
        "owner_name": settings.owner_name or None,
        "owner_name_set": bool(settings.owner_name),
        "env_line": env_line,
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
        },
        "per_call_chars": found.per_call_chars,
        "skills": [
            {"name": skill.name, "description": skill.description} for skill in found.skills
        ],
        "skills_dir": str(settings.skills_dir),
        "subagent_memory": SUBAGENT_MEMORY,
    }
