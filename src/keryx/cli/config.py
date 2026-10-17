"""`keryx config`: reading and changing the settings."""

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Annotated

import typer

from keryx.cli.apps import config_app
from keryx.cli.common import (
    _load_settings_optional,
    _load_settings_reporting,
    _owner_only,
    _plain,
    _read_secret_input,
    _shorten,
    _store_values,
)
from keryx.config import GROUPS, env_var_name, field_for, is_secret
from keryx.config import verify as credentials
from keryx.config.files import HOME_ENV, XDG_HOMES, xdg_home
from keryx.config.options import has_options, options_for
from keryx.config.store import ConfigError, ConfigStore


def _key_or_exit(key: str) -> str:
    name = field_for(key)
    if name is None:
        typer.echo(f"there is no setting called {key.upper()} (`keryx config list`)", err=True)
        raise typer.Exit(2)
    return env_var_name(name)


@config_app.command("list")
def config_list(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
    group: Annotated[
        str | None, typer.Option("--group", help=f"Only one group: {', '.join(GROUPS)}.")
    ] = None,
) -> None:
    """Every setting: whether it is set, where from, and whether the service may change it.

    A secret's value is never shown — only whether it is set.
    """
    settings = _load_settings_optional()
    rows = ConfigStore().describe(settings)
    if group is not None:
        if group not in GROUPS:
            typer.echo(f"no group {group!r}; one of: {', '.join(GROUPS)}", err=True)
            raise typer.Exit(2)
        rows = [row for row in rows if row["group"] == group]
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for name, title in GROUPS.items():
        members = [row for row in rows if row["group"] == name]
        if not members:
            continue
        typer.echo(f"\n{title}")
        width = max(len(row["key"]) for row in members)
        for row in members:
            if row["secret"]:
                value = "(secret, set)" if row["set"] else "(secret)"
            else:
                value = _plain(row["value"])
            where = row["source"] if row["set"] else ""
            mark = "*" if row["service_writable"] else " "
            typer.echo(f"  {mark} {row['key']:<{width}}  {_shorten(value, 50):<50}  {where}")
    typer.echo("\n* the running service may change it (`keryx config lock KEY` to stop that)")


@config_app.command("get")
def config_get(
    keys: Annotated[list[str], typer.Argument(help="One or more settings, by name.")],
    shell: Annotated[
        bool, typer.Option("--shell", help="Print KEY='value' lines a shell can eval.")
    ] = False,
) -> None:
    """Print the value a setting has now, from wherever it comes. Never a secret's."""
    settings, problems = _load_settings_reporting()
    if "config_file" in problems:  # a script must not be handed the defaults instead
        typer.echo(problems["config_file"], err=True)
        raise typer.Exit(1)
    dumped = settings.model_dump(mode="json")
    for key in keys:
        name = field_for(_key_or_exit(key))
        assert name is not None
        if is_secret(name):
            typer.echo(
                f"{env_var_name(name)} is a secret and is never printed; "
                "`keryx config list` says whether it is set",
                err=True,
            )
            raise typer.Exit(1)
        value = _plain(dumped[name])
        typer.echo(f"{env_var_name(name)}={shlex.quote(value)}" if shell else value)


@config_app.command("options")
def config_options(
    key: Annotated[str, typer.Argument(help="A setting with a list of values, by name.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """The values a setting can take, found now: the models a provider lists, the voices,
    the assistant's built-in names.

    A list that cannot be read falls back to the values Keryx knows, and says why; that is
    still a success. `keryx config list --json` marks the settings that have one.
    """
    key = _key_or_exit(key)
    if not has_options(key):
        typer.echo(f"{key} has no list of options", err=True)
        raise typer.Exit(2)
    found = options_for(key, _load_settings_optional())
    if as_json:
        typer.echo(json.dumps(found.as_dict(), indent=2))
        return
    if found.error:
        typer.echo(found.error)
    for option in found.options:
        value = option.value or '""'  # blank is a value too: OPENAI_VOICE's own voice
        detail = f" ({option.detail})" if option.detail else ""
        typer.echo(f"{value}  {option.label}{detail}")


@config_app.command("verify")
def config_verify(
    key: Annotated[str, typer.Argument(help="The key or credential to check, by name.")],
    stdin: Annotated[
        bool,
        typer.Option("--stdin", help="Check the value on standard input (one line), unsaved."),
    ] = False,
    pair: Annotated[
        bool,
        typer.Option(
            "--pair",
            help="TWILIO_AUTH_TOKEN with --stdin: two lines, the account SID then the token, "
            "checked together, neither saved.",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """Check that a key or credential works, and save nothing.

    OPENAI_API_KEY, CODEX_API_KEY (an OpenAI key) and ANTHROPIC_API_KEY are asked for their
    provider's list of models. TWILIO_AUTH_TOKEN is checked with the saved TWILIO_ACCOUNT_SID,
    and names the account: save the SID first, since it is not checked on its own — or give
    both with --stdin --pair. With --stdin the value given is checked and not saved (`keryx
    config set KEY --stdin` saves it); without, the saved one. A key that does not work is
    still a result (exit 0); a key that cannot be checked exits 2. No key is ever printed.
    """
    key = _key_or_exit(key)
    if not credentials.can_verify(key):
        hint = "; TWILIO_AUTH_TOKEN checks it with its token" if key == "TWILIO_ACCOUNT_SID" else ""
        supported = ", ".join(credentials.SUPPORTED)
        typer.echo(f"{key} can't be checked{hint} (only {supported})", err=True)
        raise typer.Exit(2)
    if pair and not (stdin and key == "TWILIO_AUTH_TOKEN"):
        typer.echo("--pair is TWILIO_AUTH_TOKEN with --stdin: the SID, then the token", err=True)
        raise typer.Exit(2)
    sid = None
    if pair:
        sid, value = _read_twilio_pair()
        if not ACCOUNT_SID.fullmatch(sid):
            typer.echo("The account SID is AC and 32 hexadecimal characters.", err=True)
            raise typer.Exit(2)
    else:
        value = _read_secret_line(key) if stdin else None
    result = credentials.verify(key, value, _load_settings_optional(), account_sid=sid)
    if as_json:
        typer.echo(json.dumps(result.as_dict(), indent=2))
    elif result.ok:
        typer.echo(f"{key} works" + (f" ({result.detail})" if result.detail else ""))
    else:
        typer.echo(f"{key} does not work: {result.problem}")


def _read_secret_line(key: str) -> str:
    """One line of standard input, without its newline (a hidden prompt at a terminal)."""
    if sys.stdin.isatty():
        return typer.prompt(key, hide_input=True).strip()
    return sys.stdin.readline().strip()


#: A Twilio account SID: checked before Twilio is asked, so a mistyped one is said as such.
ACCOUNT_SID = re.compile(r"AC[0-9a-fA-F]{32}")


def _read_twilio_pair() -> tuple[str, str]:
    """The account SID and the auth token, one line each (the token hidden at a terminal)."""
    if sys.stdin.isatty():
        sid = typer.prompt("TWILIO_ACCOUNT_SID").strip()
        return sid, typer.prompt("TWILIO_AUTH_TOKEN", hide_input=True).strip()
    return sys.stdin.readline().strip(), sys.stdin.readline().strip()


@config_app.command("set")
def config_set(
    pairs: Annotated[list[str], typer.Argument(help="KEY VALUE [KEY VALUE …], or one KEY.")],
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the one KEY's value from standard input.")
    ] = False,
    from_env: Annotated[
        str | None,
        typer.Option("--from-env", help="Take the one KEY's value from this variable."),
    ] = None,
) -> None:
    """Change settings. A secret's value is taken only from --stdin or --from-env.

    Never on the command line, where `ps` and the shell's history keep it. Run inside a
    Keryx task, this acts as the running service, and may change only what that is allowed
    to (`keryx config list`).
    """
    if stdin or from_env is not None:
        if len(pairs) != 1 or (stdin and from_env is not None):
            typer.echo("--stdin and --from-env set exactly one KEY, and only one of them", err=True)
            raise typer.Exit(2)
        key = _key_or_exit(pairs[0])
        if from_env is not None:
            if not os.environ.get(from_env):
                typer.echo(f"{from_env} is not set in this environment", err=True)
                raise typer.Exit(2)
            values = {key: os.environ[from_env]}
        else:
            values = {key: _read_secret_input(key)}
    else:
        if not pairs or len(pairs) % 2:
            typer.echo("give KEY VALUE pairs (or one KEY with --stdin / --from-env)", err=True)
            raise typer.Exit(2)
        values = {}
        for key, value in zip(pairs[::2], pairs[1::2], strict=True):
            key = _key_or_exit(key)
            name = field_for(key)
            if name is not None and is_secret(name):
                typer.echo(
                    f"{key} is a secret: never on the command line. "
                    f"`keryx config set {key} --stdin`, or --from-env VAR",
                    err=True,
                )
                raise typer.Exit(2)
            values[key] = value
    _store_values(values)


@config_app.command("unset")
def config_unset(
    keys: Annotated[list[str], typer.Argument(help="The settings to return to their default.")],
) -> None:
    """Remove settings from the store, so the default (or the environment) applies again."""
    _store_values({_key_or_exit(key): None for key in keys})


@config_app.command("path")
def config_path(
    shell: Annotated[
        bool,
        typer.Option("--shell", help="Print NAME='path' lines a shell can eval, and nothing else."),
    ] = False,
) -> None:
    """Print where the configuration, the data, the state and the cache live."""
    settings, problems = _load_settings_reporting()
    if "config_file" in problems:  # a script must not be handed the defaults instead
        typer.echo(problems["config_file"], err=True)
        raise typer.Exit(1)
    store = ConfigStore()
    if shell:
        # What the installers render into the service, so it resolves exactly these: the
        # four directories, and the XDG base directories they were derived from.
        paths = {
            HOME_ENV: store.home,
            "DATA_DIR": settings.data_dir,
            "STATE_DIR": settings.state_dir,
            "CACHE_DIR": settings.cache_dir,
            **{variable: xdg_home(kind) for kind, (variable, _) in XDG_HOMES.items()},
        }
        for name, path in paths.items():
            typer.echo(f"{name}={shlex.quote(str(path))}")
        return
    rows = [
        (HOME_ENV, store.home),
        ("config", store.config_path),
        ("secrets", store.secrets_path),
        ("data", settings.data_dir),
        ("state", settings.state_dir),
        ("cache", settings.cache_dir),
    ]
    for name, path in rows:
        typer.echo(f"{name:<12} {path}")


@config_app.command("import-env")
def config_import_env(
    path: Annotated[
        Path, typer.Argument(help="The legacy env file.", exists=True, dir_okay=False)
    ] = Path(".env"),
) -> None:
    """Move a legacy .env (and .secrets/client_secret.json) into the store, then rename it."""
    _owner_only("import settings")
    try:
        report = ConfigStore().import_env(path)
    except ConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"imported: {', '.join(report.imported) or 'nothing'}")
    if report.defaults:
        typer.echo(f"left at their defaults: {', '.join(report.defaults)}")
    if report.kept:
        typer.echo(f"already set in the store, kept as they were: {', '.join(report.kept)}")
    if report.pin:
        typer.echo(f"PIN: {report.pin}")
    if report.client_file:
        typer.echo(f"Google client file: {report.client_file}")
    if report.unknown:
        typer.echo(f"not settings, not imported: {', '.join(report.unknown)}")
    typer.echo(f"{path} is now {report.renamed_to}; delete it once Keryx runs from the store.")


@config_app.command("lock")
def config_lock(key: Annotated[str, typer.Argument(help="The setting.")]) -> None:
    """Stop the running service changing KEY."""
    _owner_only("lock a setting")
    try:
        ConfigStore().lock(_key_or_exit(key))
    except ConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{key.upper()}: the running service may no longer change it")


@config_app.command("unlock")
def config_unlock(key: Annotated[str, typer.Argument(help="The setting.")]) -> None:
    """Let the running service change KEY. Refused for credentials and protected settings."""
    _owner_only("unlock a setting")
    try:
        ConfigStore().unlock(_key_or_exit(key))
    except ConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{key.upper()}: the running service may change it")
