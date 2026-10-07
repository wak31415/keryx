"""`keryx setup plan`: the steps to a first working phone call, which the Mac app's Setup
Assistant renders one at a time, and `keryx setup mark`, which records one as walked.

It decides nothing of its own. Each step's status is the wizard's (`section_statuses`, over
the same doctor checks `keryx setup` reads), so the app and the terminal cannot disagree
about what is left. Where a step is not one wizard section, it is cut from the same checks:
`pin` is the owner section's PIN check, `owner` the rest of it, and `migrate` the wizard's
`import`, present only while the storage refusal stands. `service` adds one check doctor
does not make, whether the installed unit is running, because a first call needs it up.

The states, for the app's decoder:

- **done**: nothing missing or failed, and nothing it waits for left — or a step the owner
  has walked and chose to leave: an optional one skipped, or `pin` left to the first call
  (the wizard's "Owner and PIN" walked counts, since that is where it asks).
- **failed**: a check failed. Said even before the step's turn, since something configured
  is broken.
- **blocked**: a step in `blocked_by` is not done. It wins over done, needed and optional:
  a voice whose own checks pass is not working while the local server it runs on is not.
- **needed**: a required step with something missing; **optional**: one that is not.

`local` is required while the voice or the default agent runs on it, since the first call
then cannot happen without it; otherwise optional. While `migrate` is present every other
step waits for it, as the wizard does: whatever setup saved would land where the migration
is about to move things.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keryx.agents.registry import BACKENDS
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.doctor import Check, check_title
from keryx.setup.wizard import DONE as WIZARD_DONE
from keryx.setup.wizard import FAILED as WIZARD_FAILED
from keryx.setup.wizard import from_checks, section_statuses

#: The document's version; the Mac app's decoders are written against this one.
SCHEMA = 1

DONE, NEEDED, OPTIONAL, BLOCKED, FAILED = "done", "needed", "optional", "blocked", "failed"
MARKS = {DONE: "✓", NEEDED: "○", FAILED: "✗", BLOCKED: "…", OPTIONAL: "·"}

#: The PIN check's name in `doctor`, which the `pin` step takes from the owner section.
PIN_CHECK = "PIN"
#: The check `service` adds: is the installed unit running?
RUNNING_CHECK = "service running"


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    #: The wizard section whose status and checks this step reads.
    section: str
    required: bool = False
    #: The steps that must be done first.
    after: tuple[str, ...] = ()
    #: Whether a soft check that is merely missing holds the step back. Not for the voice
    #: (an OpenAI key beside a voice of your own buys web search alone) nor the agents (one
    #: that is not the default only narrows what can be asked for by name).
    soft_missing_counts: bool = True
    #: Why it is not done, when the wizard holds it back with no check to say so.
    offer: str = ""


STEPS: tuple[Step, ...] = (
    Step("migrate", "Migration", "import", required=True),
    Step("voice", "Voice", "voice", required=True, soft_missing_counts=False),
    Step("agents", "Coding agents", "agents", required=True, soft_missing_counts=False),
    Step("owner", "Owner", "owner", required=True),
    Step("pin", "PIN", "owner", required=True),
    Step("phone", "Phone", "phone", required=True, after=("owner",)),
    Step("service", "Background service", "service", required=True,
         after=("phone", "voice", "agents")),
    Step("local", "Local models", "local",
         offer="Optional: the voice and the coding agent can run on this Mac."),
    Step("google", "Google", "google"),
    Step("plugins", "Plugins", "plugins"),
    Step("issues", "Issue reports", "issues",
         offer="Off until you choose: Keryx can file its own bug reports and your ideas."),
    Step("profile", "About you", "profile"),
    Step("projects", "Project context", "projects",
         offer="Optional: an agent can write a short brief of each of your projects."),
)
STEP_IDS = tuple(step.id for step in STEPS)
_BY_ID = {step.id: step for step in STEPS}


def _service_active(settings: Settings) -> bool:
    """Whether the installed unit is running, asked from outside as `keryx status` asks."""
    from keryx.restart.service import is_active, resolve_target

    target = resolve_target(settings, from_outside=True)
    return target is not None and is_active(target)


def build_plan(
    settings: Settings,
    store: ConfigStore,
    checks: list[Check],
    *,
    service_active: Callable[[Settings], bool] = _service_active,
) -> dict[str, Any]:
    """The whole document `keryx setup plan --json` prints, from `keryx doctor`'s checks."""
    found = section_statuses(settings, store, checks)
    walked = set(store.walked_sections())
    local_needed = bool(settings.voice_base_url) or BACKENDS[settings.agent_backend].harness
    rows = []
    for step in STEPS:
        if step.id == "migrate" and "import" not in found:
            continue
        own = _checks(step, checks)
        if step.id == "service" and _all_ok(own):
            active = service_active(settings)
            own.append(Check(RUNNING_CHECK, active, "running" if active else
                             "installed, but not running", section="service"))
        required = step.required or (step.id == "local" and bool(local_needed))
        rows.append((step, own, required, _status(step, own, found), _walked(step, walked)))
    present = {step.id for step, *_ in rows}
    settled = {
        step.id
        for step, _own, required, status, was_walked in rows
        if status == WIZARD_DONE
        or (status != WIZARD_FAILED and was_walked and (not required or step.id == "pin"))
    }
    blocked_by = {step.id: _blockers(step, settings, present) for step, *_ in rows}
    done: dict[str, bool] = {}

    def is_done(key: str) -> bool:
        # Settled by its own checks, and everything it waits for done too (acyclic).
        if key not in done:
            done[key] = key in settled and all(is_done(b) for b in blocked_by[key])
        return done[key]

    steps = []
    for step, own, required, status, was_walked in rows:
        blockers = blocked_by[step.id]
        waiting = [_BY_ID[key] for key in STEP_IDS if key in blockers and not is_done(key)]
        if status == WIZARD_FAILED:
            state, why = FAILED, _why(own, "failed", step)
        elif waiting:
            state, why = BLOCKED, f"Waits for {_and([s.title for s in waiting])}."
        elif is_done(step.id):
            state, why = DONE, None
        else:
            state = NEEDED if required else OPTIONAL
            why = _why(own, "missing", step)
        steps.append({
            "id": step.id,
            "title": step.title,
            "required": required,
            "state": state,
            "why": why,
            "blocked_by": blockers,
            "walked": was_walked,
            "checks": [
                {"title": check_title(check.name), "state": check.state, "detail": check.detail}
                for check in own
            ],
        })
    ready = all(step["state"] == DONE for step in steps if step["required"])
    return {"schema": SCHEMA, "ready": ready, "steps": steps}


def _checks(step: Step, checks: list[Check]) -> list[Check]:
    own = [check for check in checks if check.section == step.section]
    if step.id == "pin":
        return [check for check in own if check.name == PIN_CHECK]
    if step.id == "owner":
        return [check for check in own if check.name != PIN_CHECK]
    return own


def _all_ok(checks: list[Check]) -> bool:
    return bool(checks) and all(check.ok for check in checks)


def _status(step: Step, own: list[Check], found: dict[str, str]) -> str:
    """The wizard's status for the step, cut down to its own checks where it splits one."""
    if step.id in ("owner", "pin", "service"):
        return from_checks(own)
    if not step.soft_missing_counts:
        return from_checks([c for c in own if not (c.unset and c.severity == "soft")])
    return found[step.section]


def _walked(step: Step, walked: set[str]) -> bool:
    if step.id == "pin":  # the wizard asks it in "Owner and PIN"
        return bool({"pin", "owner"} & walked)
    return _mark_key(step) in walked


def _mark_key(step: Step) -> str:
    """What `[setup] walked` calls the step: the wizard's section where it is one."""
    return "pin" if step.id == "pin" else step.section


def _blockers(step: Step, settings: Settings, present: set[str]) -> list[str]:
    after = list(step.after)
    if step.id == "voice" and settings.voice_base_url:
        after.append("local")
    if step.id == "agents" and BACKENDS[settings.agent_backend].harness is not None:
        after.append("local")
    if step.id != "migrate" and "migrate" in present:
        after.insert(0, "migrate")
    return after


def _why(own: list[Check], state: str, step: Step) -> str:
    """One sentence for the app: the first check in `state`, by its title, and what is
    wrong with it in a few words — or the step's own offer. Never a command: what to run is
    the app's to offer, and doctor's whole sentence stays in `checks[].detail`."""
    for check in own:
        if check.state == state:
            return f"{check_title(check.name)}: {_phrase(check)}."
    return step.offer or f"{step.title} is not set up yet."


#: What gives a command away, in a detail written for a terminal.
SHELL_TEXT = ("`", "keryx ", "scripts/", "uv ", "brew ", "$ ")
#: Details whose first clause is too long for a sentence beside a title, by their start.
PHRASES = (("calls go to ", "points elsewhere"),)
#: The phrase for a check whose detail is nothing but terminal advice.
GENERIC = {"missing": "not set up", "failed": "not working"}


def _phrase(check: Check) -> str:
    """A check's detail as a short state phrase: its first clause, before the em dash that
    leads into terminal advice, without asides in brackets, and only the last of the
    `where; what` parts doctor joins. Anything still holding shell text is the generic
    phrase for its state."""
    text = check.detail.split(" — ", 1)[0]
    text = re.sub(r"\s*\([^)]*\)", "", text).split("; ")[-1].strip().rstrip(".")
    for start, phrase in PHRASES:
        if text.startswith(start):
            return phrase
    if not text or any(shell in text for shell in SHELL_TEXT):
        return GENERIC.get(check.state, "not working")
    return text


def _and(titles: list[str]) -> str:
    return titles[0] if len(titles) == 1 else f"{', '.join(titles[:-1])} and {titles[-1]}"


def mark_step(store: ConfigStore, step_id: str) -> dict[str, Any]:
    """Record `step_id` as walked: an optional step skipped, or the PIN left to the first
    call. Raises ValueError for a step that is not one."""
    step = _BY_ID.get(step_id)
    if step is None:
        raise ValueError(
            f"there is no setup step called {step_id}; the steps: {', '.join(STEP_IDS)}"
        )
    store.mark_walked(_mark_key(step))
    return {"id": step.id, "walked": True}


def format_plan(plan: dict[str, Any]) -> list[str]:
    """`keryx setup plan` without `--json`: a headline, then the steps as a checklist."""
    left = sum(1 for step in plan["steps"] if step["required"] and step["state"] != DONE)
    lines = ["Ready for a first call." if plan["ready"] else
             f"Not ready: {left} required step{'s' * (left != 1)} left before the first call."]
    for step in plan["steps"]:
        title = step["title"] + ("" if step["required"] else " (optional)")
        why = f" — {step['why']}" if step["why"] else ""
        lines.append(f"{MARKS[step['state']]}  {title}{why}")
    return lines
