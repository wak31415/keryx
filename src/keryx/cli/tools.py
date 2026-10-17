"""`keryx tools`: the owner's own voice tools."""

import json
from typing import Annotated

import typer

from keryx.cli.apps import app
from keryx.cli.common import _configure_readonly
from keryx.tools.builtin import BUILTIN_TOOL_NAMES
from keryx.tools.custom import load_custom_tools


@app.command("tools")
def tools_command(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """List your own voice tools (DATA_DIR/tools) and any file the next call would refuse.

    The same load a call makes, against every built-in tool's name, so a name clash or a
    file that will not import shows up here first. Exits 1 when anything was refused. A
    new or edited tool is offered from the next call, with no restart.

    Nothing of the service's is built here: a subagent runs this inside the live service's
    environment, and the application's own objects own its socket and its records.
    """
    settings = _configure_readonly()
    directory = settings.custom_tools_dir
    loaded = load_custom_tools(directory, taken=BUILTIN_TOOL_NAMES)
    if as_json:
        typer.echo(json.dumps({
            "directory": str(directory),
            "tools": [
                {"name": tool.name, "needs_pin": tool.needs_pin, "file": path.name,
                 "description": tool.description}
                for path, tool in loaded.tools
            ],
            "refused": [{"file": path.name, "reason": why} for path, why in loaded.errors],
        }, indent=2))
    else:
        typer.echo(f"# {directory}")
        if not loaded.tools and not loaded.errors:
            typer.echo("no custom tools")
        for path, tool in loaded.tools:
            pin = "needs PIN" if tool.needs_pin else "no PIN"
            typer.echo(f"{tool.name:<24} {pin:<10} {path.name}")
        for path, why in loaded.errors:
            typer.echo(f"refused {path.name}: {why}")
    if loaded.errors:
        raise typer.Exit(1)
