"""`keryx doctor` and `keryx status`."""

import json
from typing import Annotated

import typer
from pydantic import ValidationError

from keryx.cli.apps import app
from keryx.cli.common import _config_summary, _doctor_checks, _load_settings_reporting
from keryx.config.settings import ConfigFileError
from keryx.config.store import ConfigStore
from keryx.doctor import fix_permissions, format_check, has_hard_failure
from keryx.status import build_status, failed_status, format_status


@app.command()
def doctor(
    as_json: Annotated[
        bool, typer.Option("--json", help="Print every check as JSON and nothing else.")
    ] = False,
    fix: Annotated[
        bool, typer.Option("--fix", help="Make every secret file owner-only first. Nothing else.")
    ] = False,
) -> None:
    """Check that this machine is set up to run Keryx; exits non-zero on a hard failure."""
    settings, problems = _load_settings_reporting()
    store = ConfigStore()
    if fix:
        changed = fix_permissions(settings, store)
        if not as_json:
            for line in changed or ["nothing to tighten"]:
                typer.echo(f"fix: {line}")
    checks = _doctor_checks(settings, problems, store)
    failed = sum(1 for check in checks if not check.ok and check.severity == "hard")
    if as_json:
        typer.echo(
            json.dumps({"ok": not failed, "checks": [c.as_dict() for c in checks]}, indent=2)
        )
        if failed:
            raise typer.Exit(1)
        return
    for check in checks:
        typer.echo(format_check(check))
    if has_hard_failure(checks):
        typer.echo(f"\n{failed} check(s) failed.")
        raise typer.Exit(1)
    typer.echo("\nall good.")


@app.command()
def status(
    as_json: Annotated[
        bool, typer.Option("--json", help="Print it as one JSON document.")
    ] = False,
) -> None:
    """Say whether Keryx is running, and what it has to tell you; a report, so it exits 0."""
    try:
        settings, problems = _load_settings_reporting()
    except ConfigFileError as error:
        report = failed_status(str(error))
    except ValidationError as error:
        report = failed_status(_config_summary(error))
    else:
        report = build_status(settings, config_problems=problems)
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    for line in format_status(report):
        typer.echo(line)
