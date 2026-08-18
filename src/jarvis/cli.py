"""Jarvis command-line interface."""

import typer

from jarvis.config import load_settings

app = typer.Typer(help="Jarvis voice agent.")


@app.command("download-models")
def download_models() -> None:
    """Download the configured wake-word model via openwakeword."""
    import openwakeword.utils

    settings = load_settings()
    openwakeword.utils.download_models(model_names=[settings.wakeword_model])


@app.command()
def serve() -> None:
    """Start the Jarvis server (not implemented yet)."""
    typer.echo("not implemented yet")
    raise typer.Exit(code=1)
