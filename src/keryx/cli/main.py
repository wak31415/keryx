"""The options every `keryx` command takes."""

import importlib.metadata
from typing import Annotated

import typer

from keryx.cli.apps import app


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"keryx {importlib.metadata.version('keryx-voice')}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the installed version and exit.",
        ),
    ] = False,
) -> None:
    """Keryx voice agent."""
