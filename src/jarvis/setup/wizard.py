"""`jarvis setup`: the sections in order, which of them are left, and the closing summary.

It opens with a table of every section's status, read from `jarvis doctor` — ✓ done,
○ missing, ✗ failed — and by default walks only what is left: a section that failed, one
that is required and missing (the voice key, a coding agent), and one that is missing and
has not been walked before. Walking a section is remembered (`ConfigStore.mark_walked`), so
Google left for later is not asked about on every run; `--all`, or choosing "Review
everything", walks them all and asks again about what is set.

Nothing here assumes what the machine lacks: every section looks first and asks only about
what it did not find.

Esc at any question goes back one (`walk`, through `jarvis.setup.rewind`): to the one
before in the same section, or to the last one a section before it asked.
"""

from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources

from jarvis.agents.registry import BACKENDS
from jarvis.config import Settings
from jarvis.config.store import ConfigStore
from jarvis.doctor import Check, format_check, run_doctor_checks
from jarvis.projects import MAX_BRIEF_CHARS, MAX_BRIEFS_CHARS, summaries_dir
from jarvis.setup import agents, google, phone, profile, project_context, sections
from jarvis.setup.context import SetupContext
from jarvis.setup.rewind import Entry, Recorder, rewind
from jarvis.setup.ui import Back, Choice

DONE, MISSING, FAILED = "done", "missing", "failed"
MARKS = {DONE: "✓", MISSING: "○", FAILED: "✗"}


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    run: Callable[[SetupContext], None]
    #: Walked until done, however often: Jarvis does not start without it.
    required: bool = False


SECTIONS: tuple[Section, ...] = (
    Section("import", "Move to the XDG directories", sections.run_import),
    Section("voice", "Voice", sections.run_voice, required=True),
    Section("agents", "Coding agents", agents.run_section, required=True),
    Section("settings", "Settings", sections.run_settings),
    Section("owner", "Owner and PIN", sections.run_owner),
    Section("phone", "Phone", phone.run_section),
    Section("google", "Google", google.run_section),
    Section("slack", "Slack", sections.run_slack),
    Section("billing", "Billing", sections.run_billing),
    Section("profile", "About you", profile.run_section),
    Section("projects", "Project context", project_context.run_section),
    Section("service", "Background service", sections.run_service),
)


def statuses(ctx: SetupContext, checks: list[Check]) -> dict[str, str]:
    """Every visible section's status; the import section only while there is a `.env`."""
    settings = ctx.settings
    found: dict[str, str] = {}
    for section in SECTIONS:
        key = section.key
        if key == "import":
            if settings.storage_refusal() is not None:
                found[key] = MISSING
            continue
        if key == "settings":
            found[key] = DONE if key in ctx.store.walked_sections() else MISSING
        elif key == "slack":
            found[key] = DONE if settings.slack_bot_token and settings.slack_channel_id else MISSING
        elif key == "billing":
            configured = settings.openai_admin_key or settings.anthropic_admin_key
            found[key] = DONE if configured else MISSING
        elif key == "projects":
            written = summaries_dir(settings)
            has = written.is_dir() and any(written.glob("*.md"))
            found[key] = DONE if has else MISSING
        else:
            found[key] = _from_checks([check for check in checks if check.section == key])
    return found


def _from_checks(checks: list[Check]) -> str:
    if any(check.state == "failed" for check in checks):
        return FAILED
    if any(check.state == "missing" for check in checks):
        return MISSING
    return DONE


def pending(ctx: SetupContext, found: dict[str, str]) -> list[Section]:
    """What is left: failed, required and missing, or missing and never walked."""
    walked = set(ctx.store.walked_sections())
    left = []
    for section in SECTIONS:
        status = found.get(section.key)
        if status is None or status == DONE:
            continue
        if status == FAILED or section.required or section.key not in walked:
            left.append(section)
    return left


def run_wizard(ctx: SetupContext, *, review_all: bool = False) -> int:
    """The whole of `jarvis setup`; returns the exit code (0 unless a hard check fails)."""
    ui = ctx.ui
    ui.intro(
        "Jarvis setup",
        f"Saved in {ctx.store.home} as you go. Ctrl-C stops at any question.",
    )
    if (refusal := ctx.settings.storage_refusal()) is not None:
        # Before anything is saved: what setup would write lands where `jarvis migrate`
        # is about to move the old files, and would stand in its way.
        ui.error(refusal)
        ui.outro("Run `jarvis migrate` first (`--dry-run` shows what it would move), then "
                 "`jarvis setup` again.")
        return 1
    checks = run_doctor_checks(ctx.settings, store=ctx.store)
    found = statuses(ctx, checks)
    ui.table(
        ("", "Section", "Status"),
        [
            (MARKS[status], section.title, status)
            for section in SECTIONS
            if (status := found.get(section.key)) is not None
        ],
    )
    left = pending(ctx, found)
    visible = [section for section in SECTIONS if section.key in found]
    if review_all:
        walk, ctx.review = visible, True
    else:
        options = [Choice("review", "Review everything"), Choice("exit", "Exit")]
        if left:
            names = ", ".join(section.title for section in left)
            options.insert(0, Choice("left", f"Set up what is left ({len(left)})", hint=names))
        message = "What next?" if left else "Everything is set up."
        choice = _first_question(ctx, message, options)
        if choice == "exit":
            ui.outro("Nothing changed.")
            return 0
        walk = left if choice == "left" else visible
        ctx.review = choice == "review"
    run_walk(ctx, walk)
    return summary(ctx)


def _first_question(ctx: SetupContext, message: str, options: list[Choice]) -> str:
    while True:
        try:
            return ctx.ui.select(message, options, default=options[0].value)
        except Back:
            continue  # there is nothing before it


def run_walk(ctx: SetupContext, walk: list[Section]) -> None:
    """Run `walk` in order, going back a question on every Esc.

    Each section runs behind a `Recorder`, and what it answered is kept by section. Going
    back runs the section it lands in again, in review mode, from its record.
    """
    records: dict[str, list[Entry]] = {}
    hints: dict[str, Entry] = {}
    keys = [section.key for section in walk]
    review, ui, probes = ctx.review, ctx.ui, ctx.probes
    index = 0
    while index < len(walk):
        section = walk[index]
        hint = hints.pop(section.key, None)
        recorder = Recorder(ui, records.pop(section.key, []), hint)
        ui.section(section.title, step=(index + 1, len(walk)))
        ctx.ui, ctx.probes = recorder, recorder.probes(probes)
        # Going back to a question means asking it again, even about what is set now.
        ctx.review = review or hint is not None
        try:
            section.run(ctx)
        except Back:
            records[section.key] = recorder.log
            index = rewind(keys, index, records, hints)
            continue
        finally:
            ctx.ui, ctx.probes, ctx.review = ui, probes, review
        recorder.stop_replaying()
        records[section.key] = recorder.log
        ctx.store.mark_walked(section.key)
        index += 1


def summary(ctx: SetupContext) -> int:
    """The closing panel: what is still not right, and how to start Jarvis.

    A machine with no phone at all has left that section for later, so the phone's checks
    are not counted against it — but the phone is the only way to talk to Jarvis, so the
    closing line says that rather than how to start it. Half a phone is still counted.
    """
    ui = ctx.ui
    settings = ctx.refresh()
    checks = run_doctor_checks(settings, store=ctx.store)
    phone = bool(
        settings.twilio_account_sid or settings.twilio_number or settings.public_host
    )
    problems = [check for check in checks if not check.ok]
    hard = [
        check
        for check in problems
        if check.severity == "hard" and (phone or not _phone_only(check))
    ]
    lines = [format_check(check) for check in problems] or ["Every check passes."]
    ui.panel("jarvis doctor", "\n".join(lines))
    if hard:
        ui.outro(f"{len(hard)} thing(s) still stop Jarvis from starting — `jarvis setup` again.")
        return 1
    agent = BACKENDS[settings.agent_backend].label
    if not phone:
        ui.outro(f"Everything but the phone is set up, and the phone is how you talk to "
                 f"Jarvis: run `jarvis setup` again to set up calls; {agent} will do the work.")
        return 0
    ui.outro(f"Ready. Start Jarvis with `scripts/dev.sh`; {agent} will do the work.")
    return 0


def _phone_only(check: Check) -> bool:
    return check.section == "phone" or check.name == "allowed callers"


def agent_instructions(settings: Settings, store: ConfigStore) -> str:
    """What `jarvis setup --agent-instructions` prints, with this machine's paths in it."""
    template = resources.files("jarvis.setup").joinpath("agent_instructions.md")
    guide = resources.files("jarvis.setup.guides").joinpath(google.GUIDE)
    return template.read_text(encoding="utf-8").format(
        home=store.home,
        google_guide=guide,
        projects=summaries_dir(settings),
        max_brief=MAX_BRIEF_CHARS,
        max_total=MAX_BRIEFS_CHARS,
    )
