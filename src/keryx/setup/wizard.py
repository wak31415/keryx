"""`keryx setup`: the sections in order, which of them are left, and the closing summary.

It opens on an overview of every section's status, read from `keryx doctor` — ✓ done,
○ missing, ✗ failed — grouped into not yet configured and configured, any of which can be
walked on its own; "set up what is left" walks only what is left: a section that failed, one
that is required and missing (the voice key, a coding agent), and one that is missing and
has not been walked before. Walking a section is remembered (`ConfigStore.mark_walked`), so
Google left for later is not asked about on every run; `--all`, or choosing "Review
everything", walks them all and asks again about what is set.

Nothing here assumes what the machine lacks: every section looks first and asks only about
what it did not find.

Esc at any question goes back one (`run_walk`, through `keryx.setup.rewind`): to the one
before in the same section, to the last one a section before it asked, or to the overview.
Tab skips forward past everything already answered.
"""

from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources

from keryx.agents.registry import BACKENDS
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.doctor import Check, format_check, run_doctor_checks
from keryx.projects import MAX_BRIEF_CHARS, MAX_BRIEFS_CHARS, summaries_dir
from keryx.setup import (
    agents,
    google,
    issues,
    phone,
    plugins,
    profile,
    project_context,
    sections,
)
from keryx.setup.context import SetupContext
from keryx.setup.rewind import ASK, Entry, Recorder, rewind
from keryx.setup.ui import Back, Choice, Forward, heading

DONE, MISSING, FAILED = "done", "missing", "failed"
MARKS = {DONE: "✓", MISSING: "○", FAILED: "✗"}


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    run: Callable[[SetupContext], None]
    #: Walked until done, however often: Keryx does not start without it.
    required: bool = False


SECTIONS: tuple[Section, ...] = (
    Section("import", "Move to the XDG directories", sections.run_import),
    Section("voice", "Voice", sections.run_voice, required=True),
    Section("agents", "Coding agents", agents.run_section, required=True),
    Section("settings", "Settings", sections.run_settings),
    Section("owner", "Owner and PIN", sections.run_owner),
    Section("phone", "Phone", phone.run_section),
    Section("google", "Google", google.run_section),
    Section("plugins", "Plugins", plugins.run_section),
    Section("issues", "Issue reports", issues.run_section),
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
        elif key == "issues" and key not in ctx.store.walked_sections():
            # Off passes `doctor`, but it is a choice the owner has to have made.
            found[key] = MISSING
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
    """The whole of `keryx setup`; returns the exit code (0 unless a hard check fails).

    The home screen is the overview: every section, not yet configured ones first, each of
    which can be walked on its own and comes back here; "set up what is left" and "review
    everything" walk several and end with the summary. Esc at the first question of a walk
    comes back here too.
    """
    ui = ctx.ui
    ui.intro(
        "Keryx setup",
        f"Saved in {ctx.store.home} as you go. Ctrl-C stops at any question.",
    )
    if (refusal := ctx.settings.storage_refusal()) is not None:
        # Before anything is saved: what setup would write lands where `keryx migrate`
        # is about to move the old files, and would stand in its way.
        ui.error(refusal)
        ui.outro("Run `keryx migrate` first (`--dry-run` shows what it would move), then "
                 "`keryx setup` again.")
        return 1
    records: dict[str, list[Entry]] = {}
    walked = False
    if review_all:
        ctx.review, walked = True, True
        if run_walk(ctx, _visible(ctx), records):
            return summary(ctx)
    while True:
        ctx.refresh()
        found = statuses(ctx, run_doctor_checks(ctx.settings, store=ctx.store))
        left = pending(ctx, found)
        ui.section(HOME)
        choice = _home(ctx, found, left)
        if choice == "exit":
            if walked:
                return summary(ctx)
            ui.outro("Nothing changed.")
            return 0
        walked = True
        if choice in ("left", "review"):
            ctx.review = choice == "review"
            if run_walk(ctx, left if choice == "left" else _visible(ctx), records):
                return summary(ctx)
            continue
        [section] = [section for section in SECTIONS if section.key == choice]
        # One already configured is walked to change it: ask again what is set.
        ctx.review = found[section.key] == DONE
        run_walk(ctx, [section], records)


#: The home screen's header.
HOME = "Overview"


def _visible(ctx: SetupContext) -> list[Section]:
    found = statuses(ctx, run_doctor_checks(ctx.settings, store=ctx.store))
    return [section for section in SECTIONS if section.key in found]


def home_choices(found: dict[str, str], left: list[Section]) -> list[Choice]:
    """The overview: what is left, then every section grouped by whether it is configured."""
    options = []
    if left:
        options.append(Choice("left", f"Set up what is left ({len(left)})"))
    shown = [section for section in SECTIONS if section.key in found]
    for title, wanted in (("Not yet configured", (MISSING, FAILED)), ("Configured", (DONE,))):
        group = [section for section in shown if found[section.key] in wanted]
        if group:
            options.append(heading(title))
        for section in group:
            status = found[section.key]
            options.append(Choice(section.key, f"{MARKS[status]}  {section.title}",
                                  hint="failed" if status == FAILED else ""))
    options += [heading(""), Choice("review", "Review everything"), Choice("exit", "Exit")]
    return options


def _home(ctx: SetupContext, found: dict[str, str], left: list[Section]) -> str:
    message = "What next?" if left else "Everything is set up. What next?"
    options = home_choices(found, left)
    while True:
        try:
            return ctx.ui.select(message, options, default=options[0].value)
        except (Back, Forward):
            continue  # there is nothing before it, and nowhere to skip to


def run_walk(ctx: SetupContext, walk: list[Section], records: dict[str, list[Entry]]) -> bool:
    """Run `walk` in order, going back a question on Esc and skipping forward on Tab.

    Each section runs behind a `Recorder`, and what it answered is kept in `records`, by
    section. Going back runs the section it lands in again, in review mode, replaying its
    record up to that question; skipping forward replays whole records, from this section
    on, until a question comes up that none of them answers. False when Esc was pressed at
    the walk's first question, which leaves it.
    """
    keys = [section.key for section in walk]
    review, ui, probes = ctx.review, ctx.ui, ctx.probes
    cuts: dict[str, int] = {}
    forwarding = False

    def asked_live() -> None:
        nonlocal forwarding
        forwarding = False

    index = 0
    while index < len(walk):
        section = walk[index]
        record = records.get(section.key, [])
        if section.key in cuts:
            cut = cuts.pop(section.key)
            recorder = Recorder(ui, record[:cut], record[cut:], on_live=asked_live)
            # Going back to a question means asking it again, even about what is set now.
            ctx.review = True
        elif forwarding:
            recorder = Recorder(ui, record, on_live=asked_live)
        else:
            recorder = Recorder(ui, (), record, on_live=asked_live)
        ui.section(section.title, step=(index + 1, len(walk)))
        ctx.ui, ctx.probes = recorder, recorder.probes(probes)
        try:
            section.run(ctx)
        except Back:
            records[section.key] = recorder.record()
            target = rewind(keys, index, records, before=len(recorder.log))
            if target is None:
                return False
            index, cut = target
            cuts[keys[index]] = cut
            continue
        except Forward:
            records[section.key] = recorder.record()
            forwarding = True
            continue
        finally:
            ctx.ui, ctx.probes, ctx.review = ui, probes, review
        recorder.stop_replaying()
        # A run that asked nothing (all of it set already) keeps the record it had, so Esc
        # from the next section still has its answers to go back to.
        if any(entry.what == ASK for entry in recorder.log) or section.key not in records:
            records[section.key] = recorder.log
        ctx.store.mark_walked(section.key)
        index += 1
    return True


def summary(ctx: SetupContext) -> int:
    """The closing panel: what is still not right, and how to start Keryx.

    A machine with no phone at all has left that section for later, so the phone's checks
    are not counted against it — but the phone is the only way to talk to Keryx, so the
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
    ui.panel("keryx doctor", "\n".join(lines))
    if hard:
        ui.outro(f"{len(hard)} thing(s) still stop Keryx from starting — `keryx setup` again.")
        return 1
    agent = BACKENDS[settings.agent_backend].label
    if not phone:
        ui.outro(f"Everything but the phone is set up, and the phone is how you talk to "
                 f"{settings.assistant_name}: run `keryx setup` again to set up calls; "
                 f"{agent} will do the work.")
        return 0
    ui.outro(f"Ready. Start Keryx with `scripts/dev.sh`; {agent} will do the work.")
    return 0


def _phone_only(check: Check) -> bool:
    return check.section == "phone" or check.name == "allowed callers"


def agent_instructions(settings: Settings, store: ConfigStore) -> str:
    """What `keryx setup --agent-instructions` prints, with this machine's paths in it."""
    template = resources.files("keryx.setup").joinpath("agent_instructions.md")
    guide = resources.files("keryx.setup.guides").joinpath(google.GUIDE)
    return template.read_text(encoding="utf-8").format(
        home=store.home,
        google_guide=guide,
        projects=summaries_dir(settings),
        max_brief=MAX_BRIEF_CHARS,
        max_total=MAX_BRIEFS_CHARS,
    )
