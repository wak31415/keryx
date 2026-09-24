"""Prompt templates, loaded from the installed package rather than the source tree.

The markdown files live next to this module and are read with `importlib.resources`, so
they work the same from a wheel, an editable install or a zip. Templates use plain
`str.format` placeholders, which means they must not contain any other curly braces.
"""

import logging
import re
from collections.abc import Collection, Sequence
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.notify.twilio_out import TwilioOut
from jarvis.projects import ProjectBrief, discover_briefs, discover_projects
from jarvis.skills import Skill, discover_skills_in
from jarvis.trust import TrustLevel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.config import Settings

log = logging.getLogger("jarvis.prompts")

VOICE_SYSTEM_PROMPT = "voice_system.md"
#: The "Your tools" paragraphs for tools only some machines offer, by tool name, and the
#: placeholder each fills. A paragraph is spliced in only when the session actually has the
#: tool: describing one it was not given is an invitation to call something that is not
#: there.
OPTIONAL_TOOL_PROMPTS = {"cluster_stats": ("cluster_stats_tool", "voice_tool_cluster_stats.md")}
#: The same rule inside a sentence: words that name a tool only some machines offer, by tool
#: name, as the placeholder and what fills it when the session has the tool.
OPTIONAL_TOOL_PHRASES = {"cluster_stats": ("cluster_phrase", "the cluster, ")}
#: How the voice prompt says a result reaches the owner when nobody is on the line, by
#: whether Jarvis can text (`TwilioOut.can_text`): the restart watchdog's alert, and where an
#: answer turns up for somebody who would rather not be called. With texting off, the alert
#: is the watchdog's spoken call and the answer is the digest at the top of the next call.
_DELIVERY = {
    True: {"restart_alert": "a text", "later_route": "a text"},
    False: {"restart_alert": "a call", "later_route": "at the top of the next call"},
}

#: With the zone, because the host's clock is the model's only clock and a server keeping
#: UTC would otherwise have it tell the owner the wrong hour with confidence.
_TIME_FORMAT = "%A %d %B %Y, %H:%M %Z"
_OPENING_HEADING = "## Why this session opened"
#: Both of these sections carry their own heading so that an empty one disappears from the
#: prompt entirely, rather than leaving a heading with nothing under it for the model to
#: wonder about.
_PENDING_HEADING = "## What the owner has not heard yet"
_MEMORY_HEADING = (
    "## What you remember\n\n"
    "Written down after earlier calls, because you keep no memory of them yourself. It is "
    "background: use it to understand what the owner means and what they are in the middle of. "
    "Do "
    "not read it out, and do not treat it as today's news — check before you assert "
    "anything from it as still true."
)
#: What a trusted session is told in place of a memory when there is none yet: that it
#: knows nothing about the owner — a model told nothing about whom it is talking to fills
#: the gap with an invented familiarity — and how to spend the call finding out. The
#: absence of `memory.md` is the only marker of a first call, so the section disappears by
#: itself once anything has been written down.
FIRST_CALL_PROMPT = "first_call.md"
_NO_SKILLS = "none installed"
_NO_BRIEFS = "nothing written down yet"
#: What a withheld prompt says in place of anything discovered from their machine. Only
#: reached with `BRIEFING_BEFORE_PIN` off: on, a call below `FULL` is handed the standing
#: context like any other (`jarvis.continuity.briefing`).
_WITHHELD = "held back until the PIN"

#: The "How much this call has proved" line, per level. A label, not a sentence: the
#: paragraph under it does the explaining.
_TRUST_LABEL = {
    TrustLevel.NONE: "nothing yet — they rang in, and caller id can be faked",
    TrustLevel.POSSESSION: "you rang them, on their own number",
    TrustLevel.FULL: "everything — the PIN is in, or this is their own microphone",
}
#: What the model may do at this level, and how the call gets to the next one. Two or
#: three lines each: a longer one is a paragraph the model skims past, and the tools say
#: the rest themselves when they refuse.
_TRUST_NOTE = {
    TrustLevel.NONE: (
        "You have everything you need to talk to them: what they have not heard yet, what "
        "you remember, what they are working on, and their tasks. Answer from it. What is "
        "still behind the PIN is everything that *does* something — handing work to "
        "Claude, searching their past calls, writing to Slack, cancelling, restarting — "
        "and those come back asking for the PIN, so call the tool and let it ask rather "
        "than predicting it."
    ),
    TrustLevel.POSSESSION: (
        "Whoever answered is holding their phone, so you can tell them what landed, answer "
        "Claude's question with send_followup, arrange a call back on this number, and put "
        "a waiting approval to them. An answering machine can be talked at and cannot press "
        "a key, so before you act on something they *said*, ask them to press one key — "
        "once, when a tool asks for it, not as a greeting. Starting new work, reading their "
        "past calls, and restarting still need the PIN."
    ),
    TrustLevel.FULL: (
        "Everything is open to you. Say nothing about the PIN or about being authorized: go "
        "straight to what they asked for."
    ),
}
#: The `NONE` note for a prompt that really was withheld (`BRIEFING_BEFORE_PIN` off). The
#: model must be told what it was actually handed, or it refuses things it can do and
#: apologizes for things it has.
_WITHHELD_TRUST_NOTE = (
    "You can tell them what they have not heard yet, look something up on the web, and "
    "say what a number is. Anything that hands work to Claude, reads something of "
    "theirs, or leaves something behind comes back asking for the PIN — call the tool "
    "and let it ask, rather than predicting it."
)
#: The paragraph under "The PIN" that only belongs in a withheld prompt, for the same
#: reason: a model told its instructions are incomplete will not say "there is nothing on
#: record" when the section is simply absent.
_WITHHELD_PIN_NOTE = (
    "**Until it is in, you have been told almost nothing of theirs.** What they have not "
    "heard\nyet is the exception and is above, to be led with. Everything else — what you "
    "remember,\ntheir projects, what the back office can do — is missing, and missing is "
    "not empty. Never\ntell them there is nothing on record before the PIN; call the tool "
    "and let it ask. Once the\nPIN is accepted, what was held back reaches your "
    "instructions."
)


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


#: The paragraph that tells the voice model it has a choice of agent. Only rendered when
#: `dispatch_task` actually offers one: a single-agent install is told nothing about it.
_AGENTS_NOTE = (
    "You can hand work to {names}; {default} takes it unless they name another. When they "
    "do — \"have Codex do it\", \"ask Claude\" — pass agent on the dispatch, and say nothing "
    "more about it. A follow-up goes back to whichever agent ran the task by itself, so "
    "never name an agent for send_followup."
)
#: A whole word "Claude" that is not "Claude Code" (which is the approval bridge's, and
#: stays Claude's whoever does the dispatched work).
_CLAUDE_WORD = re.compile(r"\bClaude\b(?! Code)")


def _name_the_agent(text: str, spoken: str) -> str:
    """Our own wording with the default agent's name where it says Claude.

    The templates say "Claude" rather than carrying a placeholder on purpose. Prompts are
    re-read on every call, so a merged template is live under whatever build is running,
    and an older build blanks a placeholder it does not know — "hand real work to , which
    runs". Substituting here keeps every build's rendering whole. Only our wording is ever
    passed through: the memory, the briefs and the skills are the owner's text.
    """
    return text if spoken == "Claude" else _CLAUDE_WORD.sub(spoken, text)


def _agents_note(agents: Sequence[str]) -> str:
    """The choice-of-agent paragraph, or nothing when there is no choice."""
    if len(agents) < 2:
        return ""
    from jarvis.agents.registry import BACKENDS  # noqa: PLC0415 - registry imports prompts

    names = [BACKENDS[name].spoken_name if name in BACKENDS else name for name in agents]
    listed = ", ".join(names[:-1]) + f" or {names[-1]}"
    return _AGENTS_NOTE.format(names=listed, default=names[0])


def _format_skills(skills: list[Skill]) -> str:
    """The skill catalog as prompt lines, one per skill."""
    if not skills:
        return _NO_SKILLS
    return "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)


def _nest_headings(text: str) -> str:
    """Every markdown heading in `text` pushed one level deeper.

    The memory document is written to be read on its own (`jarvis memory`), so its sections
    are `##`. Dropped into the prompt unchanged they would sit at the same level as the
    prompt's own sections, and "Standing facts" would read as an instruction to Jarvis
    rather than as part of what it remembers.
    """
    return "\n".join(
        f"#{line}" if line.startswith("#") else line for line in text.splitlines()
    )


def _format_briefs(briefs: list[ProjectBrief]) -> str:
    """Each project's own words about itself, under its name."""
    if not briefs:
        return _NO_BRIEFS
    return "\n\n".join(f"### {brief.name}\n\n{brief.text}" for brief in briefs)


def render_voice_prompt(
    settings: "Settings",
    *,
    channel: str,
    caller: str | None,
    trust: TrustLevel = TrustLevel.FULL,
    projects: list[str] | None = None,
    skills: list[Skill] | None = None,
    briefs: list[ProjectBrief] | None = None,
    opening_context: str | None = None,
    pending: str | None = None,
    memory: str | None = None,
    tool_names: Collection[str] = (),
    can_text: bool | None = None,
    agents: Sequence[str] = (),
) -> str:
    """Render the voice system prompt for one session.

    `projects` defaults to every project the `TaskManager` can resolve — the configured
    ones plus the subdirectories of `projects_root` — so the model offers names that
    actually dispatch. `skills` defaults to the skills installed for every enabled coding
    agent, so it can recognise work the back office is good at without being told they exist.
    `briefs` defaults to the `.jarvis-brief.md` of every project that wrote one.
    `opening_context` is the reason the session was opened (a task summary on a call-back,
    say) and is dropped from the prompt when there is none. `pending` and `memory` come
    from a `Briefing` (see `jarvis.continuity.briefing`) and are dropped the same way: a
    first call on a fresh machine renders neither section, rather than an empty heading.
    `tool_names` is what the session was actually given, and decides which of the
    `OPTIONAL_TOOL_PROMPTS` paragraphs and `OPTIONAL_TOOL_PHRASES` appear. `can_text`
    defaults to `TwilioOut.can_text`, and decides whether the prompt may promise a text.
    A `FULL` session with no memory is told it knows nothing about the owner yet.
    `agents` is what `dispatch_task` offers, the default first; with more than one, the
    prompt says there is a choice. Where our own wording says Claude, it says the default
    agent's name instead (`_name_the_agent`).

    `trust` is what this call has proved (`jarvis.trust`), and since 2026-09-19 it decides
    this only together with `BRIEFING_BEFORE_PIN`. On (the default), a call below `FULL`
    is handed the same standing context as any other: the PIN is the line between reading
    and acting, not between private and not. Off, the prompt carries no map of the owner's
    world — the project names, their briefs and the skill catalog say `_WITHHELD` instead
    of being discovered at all, `memory` is dropped whatever was passed, and the two notes
    that tell the model so are swapped in, because a model told it has what it has not is
    a model that apologizes for things it is holding. `pending` is never withheld here;
    whether a call below `FULL` has any is the session's decision, not this one. The
    session re-renders once the PIN is in.

    The first-call introduction is the one thing `FULL` still buys outright: possession
    says whose phone answered, not that an interview is wanted.
    """
    withheld = trust is not TrustLevel.FULL and not settings.briefing_before_pin
    if withheld:
        project_names = skill_lines = brief_blocks = _WITHHELD
        memory = None
    else:
        known = discover_projects(settings)
        names = list(known) if projects is None else projects
        catalog = discover_skills_in(_skill_dirs(settings)) if skills is None else skills
        written = discover_briefs(known) if briefs is None else briefs
        project_names = ", ".join(names) if names else "none configured"
        skill_lines = _format_skills(catalog)
        brief_blocks = _format_briefs(written)
    spoken = _spoken_name(settings.agent_backend)
    optional = {
        placeholder: _name_the_agent(load_prompt(template).strip(), spoken)
        if tool in tool_names
        else ""
        for tool, (placeholder, template) in OPTIONAL_TOOL_PROMPTS.items()
    }
    optional |= {
        placeholder: phrase if tool in tool_names else ""
        for tool, (placeholder, phrase) in OPTIONAL_TOOL_PHRASES.items()
    }
    texting = TwilioOut(settings).can_text if can_text is None else can_text
    if memory:
        remembered = f"{_MEMORY_HEADING}\n\n{_nest_headings(memory)}"
    elif trust is TrustLevel.FULL:
        remembered = _name_the_agent(load_prompt(FIRST_CALL_PROMPT), spoken).format_map(
            _Defaulting(owner=settings.owner_label)
        )
    else:
        remembered = ""
    template = _name_the_agent(load_prompt(VOICE_SYSTEM_PROMPT), spoken)
    values = dict(
        owner=settings.owner_label,
        now=datetime.now().astimezone().strftime(_TIME_FORMAT),
        channel=channel,
        caller=caller or "unknown",
        trust=_TRUST_LABEL[trust],
        trust_note=_name_the_agent(
            _WITHHELD_TRUST_NOTE if withheld else _TRUST_NOTE[trust], spoken
        ),
        withheld_note=_WITHHELD_PIN_NOTE if withheld else "",
        projects=project_names,
        skills=skill_lines,
        project_briefs=brief_blocks,
        opening_context=f"{_OPENING_HEADING}\n\n{opening_context}" if opening_context else "",
        pending_tasks=f"{_PENDING_HEADING}\n\n{pending}" if pending else "",
        memory=remembered,
        agents=_agents_note(agents),
        **_DELIVERY[texting],
        **optional,
    )
    return template.format_map(_Defaulting(values))


def _spoken_name(agent: str) -> str:
    """What the voice model calls `agent` out loud."""
    from jarvis.agents.registry import BACKENDS  # noqa: PLC0415 - registry imports prompts

    return BACKENDS[agent].spoken_name if agent in BACKENDS else agent


def _skill_dirs(settings: "Settings") -> list[Path]:
    """Every enabled agent's skills directory."""
    from jarvis.agents.registry import skill_dirs  # noqa: PLC0415 - registry imports prompts

    return skill_dirs(settings)
