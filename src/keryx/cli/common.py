"""What more than one command module needs: loading the settings, refusing the service what
only the owner may do, reading a secret, the doctor's checks, installing units, and the
wording helpers."""

import json
import logging
import logging.handlers
import sys
from collections.abc import Callable, Mapping
from typing import Any

import typer
from pydantic import ValidationError

from keryx import phone, units
from keryx.child_env import redact_known
from keryx.config import (
    PLACEHOLDER_KEY,
    Settings,
    env_var_name,
    field_for,
    is_secret,
    load_settings,
)
from keryx.config.permissions import SERVICE, current_actor
from keryx.config.settings import ConfigFileError, tolerate_broken_files
from keryx.config.store import FROM_ENV, ConfigError, ConfigStore
from keryx.doctor import Check, EndpointReach, run_doctor_checks
from keryx.localmodels import server
from keryx.notify.twilio_out import TwilioAdmin
from keryx.restart.store import RECORD_NAME, RestartStore
from keryx.setup.sections import repo_root

#: What the old command says, for one release (`pyproject.toml`'s `jarvis` script).
RENAMED_MESSAGE = (
    "Jarvis is now Keryx: run `keryx migrate`, then `keryx` wherever you ran `jarvis`."
)


def renamed() -> None:
    """The `jarvis` command: whatever it was asked, say what happened and fail.

    It fails rather than forwarding, so a unit installed before the rename stops in a
    restart loop that names the fix, instead of starting a service from the old directories.
    """
    typer.echo(RENAMED_MESSAGE, err=True)
    raise SystemExit(1)


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
#: How much of a task description fits in the `tasks list` table.
MAX_DESCRIPTION_CHARS = 60


#: What `doctor` puts in place of a field whose configured value does not validate, so a
#: broken install still gets a report rather than a traceback. A field not listed here is
#: one `doctor` cannot work around, and it re-raises.
DOCTOR_FALLBACKS: dict[str, object] = {"openai_api_key": PLACEHOLDER_KEY, "pin": None}
#: Every name a refused value can arrive under, mapped back to its field. `pydantic`
#: reports a field's *alias* when it has one — a bad `KERYX_PIN` comes back as
#: `KERYX_PIN`, where a bad `OPENAI_API_KEY` comes back as `openai_api_key` — so matching
#: on the field name alone quietly stopped `doctor` working in the one state it is for.
DOCTOR_FALLBACK_NAMES: dict[str, str] = {
    name: field
    for field in DOCTOR_FALLBACKS
    for name in (field, env_var_name(field))
}


def _validation_message(detail: Mapping[str, Any]) -> str:
    """One pydantic error as a sentence, without its framing."""
    return str(detail.get("msg", "is not valid")).removeprefix("Value error, ")


def _config_summary(error: ValidationError) -> str:
    """A `ValidationError` in the env-var names whoever set them would recognise."""
    parts = []
    for detail in error.errors():
        field = str(detail["loc"][0]) if detail.get("loc") else ""
        name = env_var_name(field) if field else "the configuration"
        parts.append(f"{name} {_validation_message(detail)}")
    return "; ".join(parts) or "the configuration is not valid"


def _configure(**overrides: object) -> Settings:
    """Load settings (with any command-line overrides), make the data dirs, set up logging."""
    try:
        settings = load_settings()
    except ConfigFileError as error:
        typer.echo(f"keryx cannot start: {error}", err=True)
        raise typer.Exit(2) from None
    except ValidationError as error:
        # This is the path `keryx serve` takes, so this message is what somebody reads in
        # `journalctl` after a restart failed to bring the service back. A traceback would
        # tell them nothing they could act on, and pydantic's own rendering of a rejected
        # `KERYX_PIN` is not something to put in a log at all.
        typer.echo(f"keryx cannot start: {_config_summary(error)}", err=True)
        typer.echo("run `keryx doctor` for the full picture.", err=True)
        raise typer.Exit(2) from error
    if overrides:
        settings = settings.model_copy(update=overrides)
    settings.ensure_dirs()
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT)
    return settings


def _load_settings_reporting() -> tuple[Settings, dict[str, str]]:
    """Settings for the read-only commands, plus why any field had to be given up on.

    `doctor` has to run *because* the install is incomplete, and `tasks` never talks to
    OpenAI at all — so a field in `DOCTOR_FALLBACKS` is replaced rather than
    allowed to raise. Each replacement is recorded against its field name, because "not
    set" and "set to something unusable" are different problems and `doctor` has to be
    able to tell them apart. Anything else still raises: an unreportable error beats a
    report built on a guess.
    """
    problems: dict[str, str] = {}
    overrides: dict[str, object] = {}
    tolerant = None
    try:
        while True:
            try:
                return load_settings(**overrides), problems
            except ConfigFileError as error:
                if "config_file" in problems:
                    raise
                # Read on without the broken file; doctor's `config.toml` check says what
                # broke. Only for this load: the next command in the process reads it again.
                problems["config_file"] = str(error)
                tolerant = tolerate_broken_files.set(True)
            except ValidationError as error:
                fresh = {
                    field: _validation_message(detail)
                    for detail in error.errors()
                    if (
                        field := DOCTOR_FALLBACK_NAMES.get(
                            str(detail["loc"][0]) if detail.get("loc") else "", ""
                        )
                    )
                    and field not in overrides
                }
                if not fresh:
                    raise
                problems.update(fresh)
                overrides.update({field: DOCTOR_FALLBACKS[field] for field in fresh})
    finally:
        if tolerant is not None:
            tolerate_broken_files.reset(tolerant)


def _load_settings_optional() -> Settings:
    """`_load_settings_reporting` for the callers that only want the settings."""
    return _load_settings_reporting()[0]


#: What a stream says, for a person, when its settings file will not parse, or the files
#: from before the rename are still to be moved; the terminal's sentence is the `detail`.
SETTINGS_UNREADABLE = "Keryx's settings file could not be read."
MIGRATE_FIRST = "Keryx cannot do that until its files from before are moved: migrate first."


def _configure_readonly(
    *, quiet: bool = True, migrated: bool = True, refuse: Callable[..., None] | None = None
) -> Settings:
    """`_configure` for the read-only commands (no `OPENAI_API_KEY` required).

    `quiet` because these print an answer, and `LOG_LEVEL` is the service's setting: the
    INFO lines it wants in `keryx.log` (a schema created, a migration run) are noise above
    a table. Warnings still show, and `LOG_LEVEL=DEBUG` still means everything.

    `migrated` refuses, like `serve`, while `keryx migrate` has yet to run: before it, the
    data these commands read is still in the old place, so they would answer from an empty
    one — and `keryx tasks list` would create a new `tasks.db` for the migration to trip
    over. The restart watchdog, which must report whatever it finds, passes False, and so
    does `setup`, whose wizard says it in its own words.

    `refuse` is told each way this stops — the refusal above, or settings that will not
    load — in place of stderr, before the same exit code: a JSON-lines stream (`models pull
    --json`), whose reader has only stdout. It is told as `EventLines.error` takes it: a
    sentence for a person, and what a terminal reads as the `detail`. Without it, settings
    that will not load raise, as they always have.
    """
    try:
        settings = _load_settings_optional()
    except (ConfigFileError, ValidationError) as error:
        if refuse is None:
            raise
        if isinstance(error, ValidationError):
            refuse(_config_summary(error))
        else:
            refuse(SETTINGS_UNREADABLE, detail=redact_known(str(error)))
        raise typer.Exit(1) from None
    if migrated and (refusal := settings.storage_refusal()):
        message = f"keryx cannot do that yet: {refusal}"
        if refuse is None:
            typer.echo(message, err=True)
        else:
            refuse(MIGRATE_FIRST, detail=message)
        raise typer.Exit(2)
    settings.ensure_dirs()
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    if quiet and level != logging.DEBUG:
        level = max(level, logging.WARNING)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    return settings


def _restart_store(settings: Settings) -> RestartStore:
    return RestartStore(settings.state_dir / RECORD_NAME)


def _shorten(text: str, limit: int) -> str:
    """`text` clipped to `limit` characters, ellipsis included."""
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "…"


def _foreign_servers(settings: Settings, report: server.Report) -> bool:
    """Whether a model server's unit was rendered for another KERYX_HOME (`units.
    owner_mismatch`): told to `report` as the refusal, before any of them is installed,
    restarted or removed."""
    foreign = units.owner_mismatch(settings, units.SERVER_KINDS)
    if foreign is not None:
        report.error(foreign.message, detail=foreign.detail)
    return foreign is not None


def _saver(
    store: ConfigStore, settings: Settings, report: server.Report
) -> Callable[[dict[str, Any]], bool]:
    """Store values as whoever runs this; a refusal told to `report`, and False."""
    def save(values: dict[str, Any]) -> bool:
        try:
            store.set(values, actor=current_actor(), settings=settings)
        except ConfigError as error:  # its own sentence names keys and rules: the detail
            report.error("Keryx could not save the settings this needs.",
                         detail=redact_known(str(error), settings))
            return False
        return True

    return save


#: What `service install|uninstall` is, refused to the service.
INSTALL_THE_SERVICES = "install or remove Keryx's services"


def _install_units(
    *, llm_unit: bool = False, voice_unit: bool = False, voice_llm_unit: bool = False,
    uninstall: bool = False, as_json: bool, verb: str = INSTALL_THE_SERVICES,
) -> None:
    _owner_only(verb)
    settings = _configure_readonly()
    outcome = units.install(settings=settings, llm=llm_unit, voice=voice_unit,
                            voice_llm=voice_llm_unit, uninstall=uninstall, root=repo_root(),
                            run=units.run_captured)
    if outcome.code != 0:
        if as_json and outcome.detail is not None:  # units another KERYX_HOME rendered
            typer.echo(json.dumps({"ok": False, "message": outcome.message,
                                   "detail": outcome.detail}, indent=2))
        else:
            typer.echo(outcome.message, err=True)
            if outcome.detail:
                typer.echo(outcome.detail, err=True)
        raise typer.Exit(outcome.code)
    if as_json:
        typer.echo(json.dumps({"ok": True, "message": outcome.message}, indent=2))
    else:
        typer.echo(outcome.message)


def _twilio_admin(settings: Settings) -> TwilioAdmin | None:
    """A Twilio client for doctor's webhook check, when there is anything to check."""
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.public_host):
        return None
    return phone.rest_admin(settings.twilio_account_sid, settings.twilio_auth_token)


def _doctor_checks(
    settings: Settings, problems: Mapping[str, str], store: ConfigStore
) -> list[Check]:
    """Every check `keryx doctor` makes, Twilio's webhook and the model servers included."""
    return run_doctor_checks(
        settings,
        config_problems=problems,
        store=store,
        twilio=_twilio_admin(settings),
        reach=EndpointReach(),
    )


def _interactive() -> bool:
    """A person at a terminal: both ends of it."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _plain(value: Any) -> str:
    """A setting's value as a script reads it: lists comma-joined, maps as JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return ",".join(map(str, value))
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _read_secret_input(key: str) -> str:
    if sys.stdin.isatty():
        return typer.prompt(key, hide_input=True).strip()
    return sys.stdin.read().strip()


def _store_values(values: dict[str, Any], *, echo: bool = True) -> dict[str, Any]:
    """Save `values` as whoever is running this; what was stored. A refusal is said on
    stderr and exits 1. `echo` says, line by line, where each one went."""
    store = ConfigStore()
    settings = _load_settings_optional()
    actor = current_actor()
    try:
        stored = store.set(values, actor=actor, settings=settings)
    except ConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    if not echo:
        return stored
    for key, value in stored.items():
        name = field_for(key)
        where = "secrets.toml" if name is not None and is_secret(name) else "config.toml"
        typer.echo(f"{key} unset" if value is None else f"{key} saved to {where}")
        if store.source_of(key, settings) == FROM_ENV:
            typer.echo(f"note: {key} is also set in the environment, which wins")
    if actor == SERVICE:
        typer.echo("set; takes effect after a restart")
    return stored


def _owner_refusal(verb: str) -> str | None:
    """Why this command is refused inside anything `keryx serve` started; None outside."""
    if current_actor() != SERVICE:
        return None
    return f"only the owner may {verb}, at their own terminal"


def _owner_only(verb: str) -> None:
    """Refuse inside anything `keryx serve` started: this command is the owner's alone.

    Each of these writes what the running service may not — protected settings, the PIN,
    the memory whole — so the service-actor rule would mean nothing if they skipped it.
    """
    if refusal := _owner_refusal(verb):
        typer.echo(refusal, err=True)
        raise typer.Exit(1)
