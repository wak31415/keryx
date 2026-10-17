"""`keryx phone`: the Twilio numbers, the webhooks and the tunnel."""

import json
from typing import Annotated

import typer

from keryx import phone
from keryx.cli.apps import phone_app
from keryx.cli.common import _configure_readonly, _owner_only


@phone_app.command("numbers")
def phone_numbers(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """Your Twilio account's numbers, and which of them rings Keryx here. Only reads; a
    report, so a refusal from Twilio is said and still exits 0."""
    report = phone.numbers_report(_configure_readonly())
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    if report["error"]:
        typer.echo(report["error"])
        return
    typer.echo(f"Twilio account: {report['account']}")
    for number in report["numbers"]:
        name = f"  {number['friendly_name']}" if number["friendly_name"] else ""
        here = "  (points here)" if number["points_here"] else ""
        typer.echo(f"{number['number']}{name}{here}")


@phone_app.command("webhooks")
def phone_webhooks(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Point TWILIO_NUMBER at this machine (the owner's alone; the one write "
            "Keryx makes to Twilio).",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """Where TWILIO_NUMBER's calls go now, and where they should: https://PUBLIC_HOST.

    With --apply, both webhooks are pointed here — show the owner both addresses first, as
    `keryx setup` does: exit 1 when Twilio refuses, 2 when there is nothing to apply yet.
    """
    if apply:
        _owner_only("point the phone number at this machine")
    settings = _configure_readonly()
    if apply:
        try:
            done = phone.apply_webhooks(settings)
        except phone.Refused as why:
            typer.echo(str(why), err=True)
            raise typer.Exit(2) from None
        except phone.Failed as why:
            typer.echo(str(why), err=True)
            raise typer.Exit(1) from None
        if as_json:
            typer.echo(json.dumps(done, indent=2))
            return
        voice, _ = phone.webhook_urls(settings.public_host or "")
        typer.echo(f"{done['number']} now rings Keryx at {voice}")
        return
    report = phone.webhooks_report(settings)
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    if report["error"]:
        typer.echo(report["error"])
    current = report["current"] or {}
    for label, field in (("Incoming call", "voice_url"), ("Call status", "status_url")):
        now = current.get(field) or "-"
        typer.echo(f"{label:<14} now {now}, wanted {report['expected'][field] or '-'}")
    if report["matches"]:
        typer.echo(f"{report['number']} already rings Keryx here")
    elif report["current"] is not None and report["expected"]["voice_url"]:
        typer.echo("`keryx phone webhooks --apply` points it here")


@phone_app.command("tunnel")
def phone_tunnel(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON and nothing else.")] = False,
) -> None:
    """The tunnel that puts this machine in front of Twilio: its program, its service,
    whether Keryx answers at https://PUBLIC_HOST/health, and the hostname the tunnel's own
    config names. Only reads."""
    report = phone.tunnel_report(_configure_readonly())
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    typer.echo(f"tunnel: {report['binary'] or 'no tunnel on PATH (ngrok or cloudflared)'}")
    if report["install_url"]:
        typer.echo(f"get cloudflared from {report['install_url']} (or `brew install cloudflared`)")
    typer.echo(f"its service: {'installed' if report['unit_installed'] else 'not installed'}")
    if report["hostname"]:
        typer.echo(f"its config names: {report['hostname']}")
    host = report["public_host"]
    if report["error"]:
        typer.echo(report["error"])
    elif host is None:
        typer.echo("PUBLIC_HOST: not set")
    else:
        answers = "answers as Keryx" if report["reachable"] else "does not answer as Keryx"
        typer.echo(f"PUBLIC_HOST: {host} {answers}")
