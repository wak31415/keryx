"""The smaller `jarvis setup` sections: import, voice, settings, owner and PIN, Slack,
billing and the background service. The larger ones have modules of their own (`agents`,
`phone`, `google`, `profile`, `project_context`).

Each is a function of a `SetupContext`. None asks again for something already set unless
the wizard is reviewing (`ctx.review`), and each saves as it goes.

The PIN is the one thing here with a door of its own (`jarvis.config.pin`): it is written
once, with `O_CREAT | O_EXCL`, and this is the only place outside a first call that writes
it. Replacing one is the owner at the keyboard — only on two explicit yeses, only after the
new PIN has been typed twice, and by an atomic rename, so that stopping or failing half way
never leaves the machine with no PIN and an open door.
"""

import json
import sys
import typing
from pathlib import Path
from typing import Any

import jarvis
from jarvis.config import (
    GROUPS,
    PIN_FROM_ENV,
    PIN_FROM_FILE,
    PIN_PATTERN,
    PIN_RULE,
    Settings,
    env_var_name,
    field_group,
    is_secret,
    is_trivial_pin,
    pin_file,
    replace_pin_at_keyboard,
    write_enrolled_pin,
)
from jarvis.config.permissions import is_protected, service_writable, writable_keys
from jarvis.config.store import ConfigError, legacy_env_file, validate
from jarvis.doctor import service_manager_check
from jarvis.setup.context import SetupContext
from jarvis.setup.phone import numbers_problem
from jarvis.setup.ui import Choice

#: Settings the manual walk does not offer: each has a section, or a door, of its own.
NOT_MANUAL = frozenset({"JARVIS_PIN", "ALLOWED_CALLERS", "OWNER_NUMBER", "OWNER_NAME"})


# --- import ----------------------------------------------------------------------------


def run_import(ctx: SetupContext) -> None:
    """Move a legacy `.env` into the store, when there is one."""
    ui = ctx.ui
    legacy = legacy_env_file()
    if legacy is None or not legacy.is_file():
        return
    ui.note(
        f"{legacy.resolve()} is a settings file from before the store. It is still read, "
        f"but nothing writes it; importing moves every value into {ctx.store.home} and "
        "renames the file out of the way."
    )
    if not ui.confirm("Import it now?", default=True):
        return
    try:
        report = ctx.store.import_env(legacy)
    except ConfigError as error:
        ui.error(str(error))
        return
    ctx.refresh()
    ui.success(f"imported {len(report.imported)} settings; {len(report.defaults)} were defaults")
    if report.pin:
        ui.success(f"the PIN: {report.pin}")
    if report.client_file:
        ui.success(f"the Google client file: copied to {report.client_file}")
    if report.kept:
        ui.note(f"already set here, kept as they were: {', '.join(report.kept)}")
    if report.unknown:
        ui.warn(f"not settings, left behind: {', '.join(report.unknown)}")
    ui.note(f"the old file is now {report.renamed_to}; delete it once Jarvis runs.")


# --- voice -----------------------------------------------------------------------------


def run_voice(ctx: SetupContext) -> None:
    """The one key Jarvis cannot start without, checked with OpenAI before it is kept."""
    ui, settings = ctx.ui, ctx.settings
    ui.note(
        "Jarvis talks through OpenAI's Realtime API. Calls are billed to this key, roughly "
        "$0.06 to $0.11 a minute."
    )
    if ctx.warn_if_overridden("OPENAI_API_KEY"):
        return
    if settings.openai_api_key not in ("", "unset") and not ctx.review:
        ui.success("OPENAI_API_KEY is set")
        return
    for _ in range(3):
        key = ui.secret("OpenAI API key", validate=lambda v: None if v.strip() else "Required.")
        with ui.spinner("Checking the key with OpenAI…"):
            problem = ctx.probes.openai_key_problem(key)
        if problem is None:
            ui.success("OpenAI accepted the key")
            ctx.save({"OPENAI_API_KEY": key})
            return
        ui.error(problem)
        if ui.confirm("Keep it anyway?", default=False):
            ctx.save({"OPENAI_API_KEY": key})
            return


# --- settings --------------------------------------------------------------------------


def run_settings(ctx: SetupContext) -> None:
    """The defaults, or a walk through the groups; then what the service may change."""
    ui = ctx.ui
    how = ui.select(
        "Everything else has a sensible default.",
        [
            Choice("recommended", "Recommended", hint="keep the defaults"),
            Choice("manual", "Configure manually", hint="walk the settings by group"),
        ],
        default="recommended",
    )
    if how == "recommended":
        writable = ", ".join(writable_keys(ctx.store.overrides()))
        ui.note(f"Jarvis may change these itself when you ask it on a call: {writable}.")
        return
    groups = ui.checkbox(
        "Which groups?", [Choice(group, title) for group, title in GROUPS.items()]
    )
    changes: dict[str, Any] = {}
    for group in groups:
        ui.note(GROUPS[group])
        for name in Settings.model_fields:
            if field_group(name) != group or is_secret(name):
                continue
            key = env_var_name(name)
            if key in NOT_MANUAL:
                continue
            value = _ask_setting(ctx, name)
            if value is not UNCHANGED:
                changes[key] = value
    if changes:
        ctx.save(changes)
    _service_permissions(ctx)


def _current(settings: Settings, name: str) -> Any:
    return settings.model_dump(mode="json")[name]


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(map(str, value))
    if isinstance(value, dict):
        return json.dumps(value) if value else ""
    return str(value).lower() if isinstance(value, bool) else str(value)


#: What `_ask_setting` answers for a setting left as it was (None is "cleared").
UNCHANGED = object()


def _ask_setting(ctx: SetupContext, name: str) -> Any:
    """One setting, asked by type: its new raw value, None to clear it, or `UNCHANGED`."""
    ui = ctx.ui
    info = Settings.model_fields[name]
    key = env_var_name(name)
    current = _current(ctx.settings, name)
    message = f"{key} — {info.description}"
    annotation = info.annotation
    choices = _literal_choices(annotation)
    if annotation is bool:
        answer: Any = ui.confirm(message, default=bool(current))
    elif choices:
        answer = ui.select(message, [Choice(value, value) for value in choices], default=current)
    else:

        def check(value: str) -> str | None:
            try:
                validate({key: value})
            except ConfigError as error:
                return str(error)
            return None

        answer = ui.text(message, default=_display(current), validate=check)
    if _display(answer) == _display(current):
        return UNCHANGED
    return answer if answer != "" else None


def _literal_choices(annotation: Any) -> list[str]:
    if typing.get_origin(annotation) is typing.Literal:
        return [str(value) for value in typing.get_args(annotation)]
    return []


def _service_permissions(ctx: SetupContext) -> None:
    """The checklist: which settings the running service may change."""
    overrides = ctx.store.overrides()
    keys = [
        env_var_name(name)
        for name in Settings.model_fields
        if not is_protected(env_var_name(name)) and env_var_name(name) != "JARVIS_PIN"
    ]
    picked = set(
        ctx.ui.checkbox(
            "Which of these may the running service change (asked on a call, or by an agent)?",
            [Choice(key, key, checked=service_writable(key, overrides)) for key in keys],
        )
    )
    for key in keys:
        now = service_writable(key, overrides)
        if key in picked and not now:
            ctx.store.unlock(key)
        elif key not in picked and now:
            ctx.store.lock(key)
    ctx.ui.note("Credentials and every line of defence stay protected whatever is ticked.")


# --- owner and PIN ---------------------------------------------------------------------


def run_owner(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    values: dict[str, Any] = {}
    if not settings.owner_name or ctx.review:
        name = ui.text('What should Jarvis call you? (blank for "the owner")',
                       default=settings.owner_name or "")
        if name != (settings.owner_name or ""):
            values["OWNER_NAME"] = name or None
    if not settings.allowed_callers or ctx.review:
        ui.note(
            "Your own phone numbers, in E.164 (+15551234567). Only these may call Jarvis, and "
            "the first is the one it rings. Leave it blank if you will not use the phone."
        )
        raw = ui.text("Your mobile numbers, comma-separated",
                      default=",".join(settings.allowed_callers), validate=numbers_problem)
        numbers = [part.strip().replace(" ", "") for part in raw.split(",") if part.strip()]
        if numbers != list(settings.allowed_callers):
            values["ALLOWED_CALLERS"] = ",".join(numbers)
            values["OWNER_NUMBER"] = numbers[0] if numbers else None
    if values:
        ctx.save(values)
    run_pin(ctx)


def pin_problem(digits: str) -> str | None:
    if not PIN_PATTERN.fullmatch(digits):
        return f"It {PIN_RULE}."
    if is_trivial_pin(digits):
        return "Pick something less guessable than a run or a repeated digit."
    return None


def run_pin(ctx: SetupContext) -> None:
    """Set the PIN here, or leave it to the first call; replace one only on two yeses."""
    ui, settings = ctx.ui, ctx.settings
    path = pin_file(settings.data_dir)
    if settings.pin_source == PIN_FROM_ENV:
        ui.success("PIN: set by JARVIS_PIN in your environment, which wins over anything here")
        return
    replacing = False
    if settings.pin_source == PIN_FROM_FILE:
        ui.success(f"PIN: set, kept in {path}")
        if not ctx.review or not ui.confirm("Choose a new PIN?", default=False):
            return
        replacing = True
    elif path.exists():
        ui.error(f"{path} is not a usable PIN, so there is none — and no call can set one.")
        replacing = True
    else:
        ui.note(
            "The PIN is what a caller says or keys before Jarvis will do anything on the "
            "phone. 6 to 8 digits."
        )
        how = ui.select(
            "Set the PIN",
            [
                Choice("now", "Choose one now", hint="recommended"),
                Choice("call", "Let the first call set it",
                       hint="whoever calls first chooses it; the window closes on first use"),
            ],
            default="now",
        )
        if how == "call":
            ui.note("Until then nothing of yours is read out on the phone.")
            return
    digits = _new_pin(ctx)
    if digits is None:
        return
    if replacing:
        if not ui.confirm(f"Replace the PIN in {path} with the new one?", default=False):
            return
        replace_pin_at_keyboard(settings.data_dir, digits)
    elif not write_enrolled_pin(settings.data_dir, digits):
        ui.error(f"A PIN appeared in {path} meanwhile; it was left alone.")
        return
    ctx.refresh()
    ui.success(f"PIN set, kept in {path} (readable by you alone)")
    ui.note("A Jarvis already running keeps the PIN it started with; `jarvis restart` moves it on.")


def _new_pin(ctx: SetupContext) -> str | None:
    for _ in range(3):
        first = ctx.ui.secret("New PIN", validate=pin_problem)
        if ctx.ui.secret("The same PIN again") == first:
            return first
        ctx.ui.error("Those were not the same.")
    return None


# --- slack and billing -------------------------------------------------------------------


def run_slack(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    ui.note('"Send me that on Slack" posts a copy of what Jarvis said to your DM.')
    if settings.slack_bot_token and settings.slack_channel_id and not ctx.review:
        ui.success("Slack is set up")
        return
    if ui.select("Set up Slack?", [Choice("skip", "Skip for now"), Choice("setup", "Set up")],
                 default="skip") == "skip":
        return
    ui.markdown(
        "1. Create an app at <https://api.slack.com/apps> (from scratch, in your workspace).\n"
        "2. Under **OAuth & Permissions**, add the bot scope `chat:write`, then **Install**.\n"
        "3. Copy the **Bot User OAuth Token** (`xoxb-…`).\n"
        "4. Open a DM with the app in Slack; its channel id is in the conversation's details."
    )
    token = ui.secret("Bot token (xoxb-…)",
                      validate=lambda v: None if v.strip().startswith("xox") else "It starts xox.")
    channel = ui.text("DM channel id", validate=lambda v: None if v.strip() else "Required.")
    ctx.save({"SLACK_BOT_TOKEN": token, "SLACK_CHANNEL_ID": channel})


def run_billing(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    ui.note('"What am I spending this month?" reads your provider\'s billing API, which needs '
            "an admin key rather than the one calls run on.")
    if (settings.openai_admin_key or settings.anthropic_admin_key) and not ctx.review:
        ui.success("a billing admin key is set")
        return
    which = ui.select(
        "Set up spending questions?",
        [
            Choice("skip", "Skip for now"),
            Choice("openai", "OpenAI", hint="platform.openai.com/settings/organization/admin-keys"),
            Choice("anthropic", "Anthropic", hint="console.anthropic.com → Admin keys"),
        ],
        default="skip",
    )
    if which == "skip":
        return
    key = ui.secret(f"{which.capitalize()} admin key", validate=_required)
    budget = ui.text("Your monthly budget in dollars (optional)",
                     default=_display(settings.billing_monthly_budget))
    values: dict[str, Any] = {f"{which.upper()}_ADMIN_KEY": key, "BILLING_PROVIDER": which}
    if budget:
        values["BILLING_MONTHLY_BUDGET"] = budget
    ctx.save(values)


def _required(value: str) -> str | None:
    return None if value.strip() else "Required."


# --- the background service ---------------------------------------------------------------


def repo_root() -> Path:
    """The checkout Jarvis runs from, where `scripts/` is."""
    return Path(jarvis.__file__).resolve().parents[2]


def run_service(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    check = service_manager_check(settings)
    if check.ok:
        ui.success(f"installed: {check.detail}")
        return
    ui.note("A background service starts Jarvis at boot, restarts it if it dies, and lets it "
            "restart itself after a change to its own code.")
    name = "install-launchd.sh" if sys.platform == "darwin" else "install-systemd.sh"
    script = repo_root() / "scripts" / name
    if not script.is_file():
        ui.note(f"Run scripts/{name} from a clone of the repository.")
        return
    if not settings.public_host:
        ui.note("It runs the phone channel and its tunnel, so it needs the phone set up first.")
        return
    if not ui.confirm(f"Install it now ({name})?", default=True):
        return
    code = ctx.probes.run_script([str(script)])
    if code == 0:
        ui.success("the service is installed and running")
    else:
        ui.error(f"{name} exited with {code}")
