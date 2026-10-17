"""`keryx integrations`: the accounts Keryx is connected to."""

import json
from typing import Annotated

import typer

from keryx.cli.apps import app
from keryx.cli.common import _configure_readonly
from keryx.integrations import accounts


@app.command("integrations")
def integrations_command(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """The accounts Keryx connects to — Google, Gmail, Slack, GitHub, the admin keys — and
    whether each is connected. Only reads; a secret's value is never shown."""
    settings = _configure_readonly()
    found = accounts.integrations(settings)
    if as_json:
        typer.echo(json.dumps({"integrations": found}, indent=2))
        return
    width = max(len(row["id"]) for row in found)
    for row in found:
        state = "connected" if row["connected"] else "not connected"
        typer.echo(f"{row['id']:<{width}}  {state:<13}  {row['title']}")
        if row["why"]:
            typer.echo(f"{'':<{width}}  {row['why']}")
