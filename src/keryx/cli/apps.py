"""The `keryx` command and its groups, each a `typer.Typer` the command modules add to."""

import typer

# No locals in a crash: `pin set` holds the digits in one.
app = typer.Typer(help="Keryx voice agent.", pretty_exceptions_show_locals=False)
tasks_app = typer.Typer(help="Inspect the tasks handed to subagents.")
app.add_typer(tasks_app, name="tasks")
config_app = typer.Typer(
    help="Read and change Keryx's settings (KERYX_HOME/config.toml and secrets.toml)."
)
app.add_typer(config_app, name="config")
auth_app = typer.Typer(help="Sign in to what Keryx needs: the coding agents and Google.")
app.add_typer(auth_app, name="auth")
memory_app = typer.Typer(help="What Keryx remembers between calls.")
app.add_typer(memory_app, name="memory")
plugins_app = typer.Typer(
    help="The optional voice tools (Slack, email, billing, cluster stats): on, off, set up."
)
app.add_typer(plugins_app, name="plugins")
models_app = typer.Typer(
    help="Models on this machine: which fit, downloading one, and the servers that run them."
)
app.add_typer(models_app, name="models")
voice_app = typer.Typer(
    help="The voice on a call: OpenAI's Realtime API, speech-to-speech here, or a server "
    "elsewhere."
)
app.add_typer(voice_app, name="voice")
voice_use_app = typer.Typer(
    help="Make OpenAI, a voice server elsewhere, or this machine the voice."
)
voice_app.add_typer(voice_use_app, name="use")
voice_llm_app = typer.Typer(
    help="Where the words of a voice running here come from: the local agent's model, a "
    "model of the voice's own on this machine, or a server elsewhere."
)
voice_app.add_typer(voice_llm_app, name="llm")
service_app = typer.Typer(
    help="Keryx's services — itself, its tunnel, the model and voice servers: whether each is "
    "installed and running, and installing or removing them."
)
app.add_typer(service_app, name="service")
setup_app = typer.Typer(
    help="Set Keryx up: the guided walk, and the plan the Mac app's assistant follows."
)
app.add_typer(setup_app, name="setup")
pin_app = typer.Typer(
    help="The phone PIN: whether one is set, and setting or replacing it (the owner's alone)."
)
app.add_typer(pin_app, name="pin")
phone_app = typer.Typer(
    help="The phone: your Twilio numbers, where the number points, pointing it here, and the "
    "tunnel.",
    no_args_is_help=True,
)
app.add_typer(phone_app, name="phone")
