"""The Plugins section of `keryx setup`: which optional voice tools Keryx has, and each one's
important settings.

One checklist of them all (`keryx.plugins.PLUGINS`). A plugin that is on is ticked, and on
a first walk so are the ones most owners want: Slack and email, and reading the calendar
and documents once their Google service is connected (neither is worth a sign-in of its
own to somebody who has none, and `add_calendar_event` writes, so it is never ticked for
anybody). Each ticked plugin then
runs its step, which asks only its `important` keys and its secret, starting from what its
file holds now — so walking this section again is also how a plugin's settings are edited.
Every step ends by naming the file the rest of its settings are in. A plugin that is on and
left unticked is turned off, after a confirm.

The plugins that use the Google sign-in (`Plugin.google`: email, the calendar pair,
documents) share one step, `run_google`. One whose service is connected is turned on and
asked nothing. One whose service is not offers the sign-in (`setup.google.connect`), at
most once in a walk however many of them were ticked, and is left off, with the two
commands that finish it, when the service is still not connected afterwards. A service
that Keryx's own Google app does not offer is not something a sign-in can add, so that
one is neither offered a sign-in nor told to try one: it is told what would, a Google
client of the owner's own.

`cluster_stats` is the one with a step of its own: it reads `~/.ssh/config`, shows the hosts
with a ControlMaster (a host without one is never offered: Keryx rides a login the owner
already has open), asks each chosen host's partition — from the list Slurm gives, over a
master that is up — and then installs it, writes it as a template to finish by hand, or
does nothing. Everything that touches ssh is a `Probes` field, so going back replays it.

Values the old settings held (before these were plugins) are the defaults, and they leave
the store once the plugin that replaced them is on.
"""

from collections.abc import Callable
from typing import Any

from keryx import plugins
from keryx.agents import registry
from keryx.config import Settings
from keryx.integrations import calendar, drive, gmail, google_api
from keryx.integrations import google as account
from keryx.setup import google
from keryx.setup.context import SetupContext
from keryx.setup.ui import Choice

INSTALL, TEMPLATE, CANCEL = "install", "template", "cancel"
#: The option under a partition list that asks for one by name instead.
TYPE_ONE = "\0type"


def _is_on(ctx: SetupContext, name: str) -> bool:
    return plugins.tool_path(ctx.settings, name).is_file()


def current_values(ctx: SetupContext, name: str) -> dict[str, Any]:
    """What the plugin's file holds, else its defaults."""
    try:
        return plugins.read_config(ctx.settings, name)
    except plugins.PluginConfigError as error:
        ctx.ui.warn(str(error))
        return plugins.defaults(name)


def run_section(ctx: SetupContext) -> None:
    ui = ctx.ui
    ui.note(
        "Plugins are the voice tools only some people want. Each is two files in "
        f"{ctx.settings.custom_tools_dir}: a line of code and a settings file you can edit."
    )
    first = "plugins" not in ctx.store.walked_sections()
    claude_ready = registry.installed("claude")
    on = {name: _is_on(ctx, name) for name in plugins.PLUGINS}
    choices = []
    for name, spec in plugins.PLUGINS.items():
        disabled = None
        if spec.claude and not claude_ready:
            disabled = f"needs Claude Code: {registry.install_command('claude')}"
        wanted = spec.default or (
            spec.default_when_connected and GOOGLE_READY[name](ctx.settings)
        )
        hint = spec.summary + (" · recommended" if wanted and not on[name] else "")
        checked = on[name] or (first and wanted and disabled is None)
        choices.append(Choice(name, f"{spec.title} ({name})", hint=hint, checked=checked,
                              disabled=disabled))
    picked = ui.checkbox("Which plugins should Keryx have?", choices)
    # Asked at most once in a walk: three plugins waiting on the same sign-in are one
    # question, not three.
    sign_in_offered = False
    for name, spec in plugins.PLUGINS.items():
        if name in picked and spec.google:
            sign_in_offered = run_google(ctx, name, offer=not sign_in_offered) or sign_in_offered
        elif name in picked:
            STEPS[name](ctx)
        elif on[name] and not (spec.claude and not claude_ready):
            _turn_off(ctx, name)


def _turn_off(ctx: SetupContext, name: str) -> None:
    ui, spec = ctx.ui, plugins.PLUGINS[name]
    if not ui.confirm(f"Turn off {spec.title} ({name})?", default=False):
        return
    plugins.remove(ctx.settings, name)
    ui.success(f"{name} is off from the next call; its settings are kept for next time")
    # The Google sign-in is everything's, so turning email off never touches it. Only the
    # Gmail-only token an older sign-in left belongs to this plugin alone.
    older = gmail.legacy_token_path(ctx.settings)
    if name == "check_email" and older.exists():
        if ui.confirm("Also delete the older Gmail-only sign-in?", default=False):
            older.unlink(missing_ok=True)
            ui.success("the older Gmail-only sign-in is deleted")


def _finish(ctx: SetupContext, name: str, values: dict[str, Any]) -> bool:
    """Write the file, turn the plugin on, and say whether it loads and where the rest is."""
    ui, settings = ctx.ui, ctx.settings
    try:
        plugins.write_config(settings, name, values)
        plugins.install(settings, name)
    except plugins.PluginConfigError as error:
        ui.error(str(error))
        return False
    status = next(one for one in plugins.status(settings) if one.name == name)
    if status.on:
        ui.success(f"{name} is on, from the next call")
    else:
        ui.warn(f"{name} is installed but not offered yet: {status.refused}")
    ui.note(f"The rest of its settings are in {plugins.config_path(settings, name)}.")
    return True


# --- the steps ---------------------------------------------------------------------------


def _word(value: str) -> str | None:
    if not value or plugins.WORD.fullmatch(value):
        return None
    return "A bare word: letters, digits, . _ -"


def run_slack(ctx: SetupContext) -> None:
    ui = ctx.ui
    values = current_values(ctx, "send_to_slack")
    token = ctx.current("SLACK_BOT_TOKEN")
    server = values["mcp_server"]
    if not token:
        ui.markdown(
            "1. Create an app at <https://api.slack.com/apps> (from scratch, in your workspace).\n"
            "2. Under **OAuth & Permissions**, add the bot scope `chat:write`, then **Install**.\n"
            "3. Copy the **Bot User OAuth Token** (`xoxb-…`).\n"
            "4. Open a DM with the app in Slack; its channel id is in the conversation's details."
        )

    def token_problem(value: str) -> str | None:
        if not value.strip():
            return None if server else "Required."
        return None if value.strip().startswith("xox") else "It starts xox."

    label = "Bot token (xoxb-…)" + (f" — blank uses {server}'s" if server and not token else "")
    answer = ui.secret(label, validate=token_problem, current=token)
    if answer and answer != token:
        ctx.save({"SLACK_BOT_TOKEN": answer})

    def channel_problem(value: str) -> str | None:
        if not value.strip():
            return None if server else "Required."
        return _word(value.strip())

    channel = ui.text("DM channel id", default=values["channel_id"], validate=channel_problem)
    _finish(ctx, "send_to_slack", {"channel_id": channel})


#: What each Google plugin needs of the sign-in before it can answer: the question its own
#: loader asks, so the wizard never turns on a plugin the next call would refuse.
GOOGLE_READY: dict[str, Callable[[Settings], bool]] = {
    "check_email": gmail.signed_in,
    "check_calendar": lambda settings: calendar.read_problem(settings) is None,
    "add_calendar_event": lambda settings: calendar.write_problem(settings) is None,
    "find_document": lambda settings: drive.read_problem(settings) is None,
}


def run_google(ctx: SetupContext, name: str, *, offer: bool = True) -> bool:
    """A plugin that uses the one Google sign-in: asked nothing when its service is there.

    Returns whether the sign-in was offered, so the section offers it once. `offer=False`
    is a plugin after one that already did: it is turned on if that sign-in connected its
    service, and left off with the same note otherwise.
    """
    ui, spec = ctx.ui, plugins.PLUGINS[name]
    label = account.SERVICES[spec.google].label
    ready = GOOGLE_READY[name]
    offered = False
    if ready(ctx.settings):
        ui.success(f"{label}: connected")
    elif not google_api.offered(ctx.settings, spec.google):
        # Signing in again asks for what the client offers, and this is not among it.
        ui.note(f"{label} is not offered by Keryx's Google app yet, so {name} is left off: "
                f"it needs your own Google client ({google.own_client_hint()}), and then "
                f"`keryx plugins install {name}`.")
        return offered
    elif offer:
        ui.note(f"{spec.title} uses {label} through your Google sign-in.")
        google.connect(ctx)
        offered = True
    if not ready(ctx.settings):
        ui.note(f"{label} is not connected, so {name} is left off: `keryx setup` again, or "
                f"`keryx auth login google` and then `keryx plugins install {name}`.")
        return offered
    _finish(ctx, name, {})
    return offered


def run_billing(ctx: SetupContext) -> None:
    ui = ctx.ui
    values = current_values(ctx, "check_billing")
    ui.note('"What am I spending this month?" reads your provider\'s billing API, which needs '
            "an admin key rather than the one calls run on.")
    provider = ui.select(
        "Whose bill should it report by default?",
        [
            Choice("auto", "OpenAI", hint="the account calls run on"),
            Choice("anthropic", "Anthropic", hint="what Claude and the subagents cost"),
        ],
        default="anthropic" if values["provider"] == "anthropic" else "auto",
    )
    which = "ANTHROPIC" if provider == "anthropic" else "OPENAI"
    where = ("console.anthropic.com → Admin keys" if provider == "anthropic"
             else "platform.openai.com/settings/organization/admin-keys")
    current = ctx.current(f"{which}_ADMIN_KEY")
    label = "Anthropic" if which == "ANTHROPIC" else "OpenAI"
    key = ui.secret(f"{label} admin key ({where})",
                    validate=lambda v: None if v.strip() else "Required.", current=current)
    if key and key != current:
        ctx.save({f"{which}_ADMIN_KEY": key})

    def number(value: str) -> str | None:
        try:
            return None if float(value or 0) >= 0 else "0 or more."
        except ValueError:
            return "A number of dollars."

    budget = ui.text("Your monthly budget in dollars (0 for none)",
                     default=f"{values['monthly_budget']:g}", validate=number)
    _finish(ctx, "check_billing", {"provider": provider, "monthly_budget": budget or 0})


def run_cluster(ctx: SetupContext) -> None:
    ui, probes = ctx.ui, ctx.probes
    values = current_values(ctx, "cluster_stats")
    current: dict[str, str] = values["clusters"]
    ui.note("It asks Slurm over an ssh login you already have open (a ControlMaster), and "
            "never opens one of its own.")
    with ui.spinner("Reading ~/.ssh/config…"):
        hosts = probes.ssh_hosts()
    alive = {host.alias: probes.ssh_master_alive(host.alias)
             for host in hosts if host.control_master}
    if hosts:
        ui.table(
            ["Host", "Address", "User", "ControlMaster", "Logged in now"],
            [[host.alias, host.hostname, host.user or "—",
              "yes" if host.control_master else "no",
              ("yes" if alive[host.alias] else "no") if host.control_master else "—"]
             for host in hosts],
        )
    clusters: dict[str, str] = {}
    if not alive:
        ui.warn("No host in ~/.ssh/config has a ControlMaster, so there is nothing it could ask "
                "yet. Add one (ControlMaster auto, ControlPath, ControlPersist) and come back, "
                "or write the file by hand.")
    else:
        picked = ui.checkbox(
            "Which hosts should cluster_stats ask?",
            [Choice(host.alias, host.alias, hint=host.hostname,
                    checked=host.control_master and host.alias in current,
                    disabled=None if host.control_master else "no ControlMaster in ~/.ssh/config")
             for host in hosts],
        )
        for alias in picked:
            clusters[alias] = _partition(ctx, alias, current.get(alias, ""), alive[alias])
    options = [Choice(TEMPLATE, "Write it as a template to edit", hint="not turned on"),
               Choice(CANCEL, "Cancel", hint="write nothing")]
    if clusters:
        ui.panel("cluster_stats.toml",
                 "[clusters]\n" + "\n".join(f'{a} = "{p}"' for a, p in clusters.items()))
        options.insert(0, Choice(INSTALL, "Install this", hint="offered from the next call"))
    action = ui.select("Install this?" if clusters else "What now?", options,
                       default=options[0].value)
    if action == CANCEL:
        ui.note("Nothing was written.")
        return
    if action == TEMPLATE:
        path = plugins.write_template(ctx.settings, "cluster_stats",
                                      {"clusters": clusters} if clusters else {})
        ui.success(f"written to {path}")
        ui.note("Edit it, then `keryx plugins install cluster_stats` turns it on.")
        return
    _finish(ctx, "cluster_stats", {"clusters": clusters})


def _partition(ctx: SetupContext, alias: str, default: str, alive: bool) -> str:
    """The partition on `alias` its GPUs are in: from Slurm's list when the master is up."""
    ui = ctx.ui

    def required_word(value: str) -> str | None:
        return "Required." if not value.strip() else _word(value.strip())

    if alive:
        with ui.spinner(f"Asking {alias} for its partitions…"):
            found = ctx.probes.cluster_partitions(alias)
        if found:
            choice = ui.select(
                f"Which partition on {alias} has the GPUs?",
                [*(Choice(name, name) for name in found), Choice(TYPE_ONE, "Type one")],
                default=default if default in found else found[0],
            )
            if choice != TYPE_ONE:
                return choice
    return ui.text(f"The GPU partition on {alias}", default=default, validate=required_word)


STEPS = {
    "send_to_slack": run_slack,
    "check_billing": run_billing,
    "cluster_stats": run_cluster,
}
