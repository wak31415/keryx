"""The Plugins section of `keryx setup`: which optional voice tools Keryx has, and each one's
important settings.

One checklist of the four (`keryx.plugins.PLUGINS`). A plugin that is on is ticked, and on
a first walk so are the ones most owners want (Slack and email); each ticked plugin then
runs its step, which asks only its `important` keys and its secret, starting from what its
file holds now — so walking this section again is also how a plugin's settings are edited.
Every step ends by naming the file the rest of its settings are in. A plugin that is on and
left unticked is turned off, after a confirm.

Slack's step makes the app from a link (`setup/slack.py`), checks the pasted token with
Slack, and asks where to send in one question that takes the owner's address (looked up)
as readily as a channel or member id; its three calls are `Probes` fields too. It asks the
same questions whatever it has already saved, because it saves as it goes and going back
replays it.

`cluster_stats` is the one with a step of its own: it reads `~/.ssh/config`, shows the hosts
with a ControlMaster (a host without one is never offered: Keryx rides a login the owner
already has open), asks each chosen host's partition — from the list Slurm gives, over a
master that is up — and then installs it, writes it as a template to finish by hand, or
does nothing. Everything that touches ssh is a `Probes` field, so going back replays it.

Values the old settings held (before these were plugins) are the defaults, and they leave
the store once the plugin that replaced them is on.
"""

from typing import Any

from keryx import plugins
from keryx.agents import registry
from keryx.integrations.gmail import token_path
from keryx.integrations.slack import new_app_url
from keryx.setup import google, slack
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
        if name == "check_email" and not claude_ready:
            disabled = f"needs Claude Code: {registry.install_command('claude')}"
        hint = spec.summary + (" · recommended" if spec.default and not on[name] else "")
        checked = on[name] or (first and spec.default and disabled is None)
        choices.append(Choice(name, f"{spec.title} ({name})", hint=hint, checked=checked,
                              disabled=disabled))
    picked = ui.checkbox("Which plugins should Keryx have?", choices)
    for name in plugins.PLUGINS:
        if name in picked:
            STEPS[name](ctx)
        elif on[name] and not (name == "check_email" and not claude_ready):
            _turn_off(ctx, name)


def _turn_off(ctx: SetupContext, name: str) -> None:
    ui, spec = ctx.ui, plugins.PLUGINS[name]
    if not ui.confirm(f"Turn off {spec.title} ({name})?", default=False):
        return
    plugins.remove(ctx.settings, name)
    ui.success(f"{name} is off from the next call; its settings are kept for next time")
    if name == "check_email" and token_path(ctx.settings).exists():
        if ui.confirm("Also delete the Gmail sign-in?", default=False):
            token_path(ctx.settings).unlink(missing_ok=True)
            ui.success("the Gmail sign-in is deleted")


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


#: The Slack step's one question about a destination. Its wording never changes, whatever
#: is saved, so a replay of the step finds the question it recorded.
SLACK_WHERE = "Where to send: the address you use in Slack, or a channel or member id"


def run_slack(ctx: SetupContext) -> None:
    """The token, checked with Slack; where to send, found by address; one test message.

    Three questions, always these and in this order, whatever is already saved. The step
    saves as it goes, and going back runs it again from a record of its answers
    (`rewind`), which has to find the questions it recorded: so what is saved changes only
    what each question *offers* — a token to keep, the destination on file, the test as a
    yes or a no — and never which are asked. The one exception is the house rule: the test
    is not offered on a plain walk that changed nothing, so an owner already set up
    answers the two questions they always did, and a review asks it, as a review asks
    about everything that is set.

    A token that is not the one on file brings its destination with it (`slack.keeps`). A
    direct message's id belongs to the app that opened it, so it is not offered beside
    another token; and beside a named MCP server, a new token may not be left to post to
    the server's channel.
    """
    ui = ctx.ui
    values = current_values(ctx, slack.PLUGIN)
    before = ctx.current(slack.TOKEN_KEY)
    server, on_file = values["mcp_server"], values["channel_id"]
    if not before:
        ui.link(new_app_url())
        ui.markdown(slack.guide())
        if server:
            ui.note(f"Leave the token blank to go on using the {server} MCP server's token "
                    "and channel.")
    token = _slack_token(ctx, before, server)
    channel = _slack_where(ctx, token, new=token != before, on_file=on_file, server=server)
    changed = token != before or channel != on_file
    if not _finish(ctx, slack.PLUGIN, {"channel_id": channel}):
        return
    # It is on now. From here it either sends a test or says why it did not.
    if not (token and channel):
        whose = "channel is" if token else "token and the channel are"
        ui.note(f"No test message was sent: the {whose} the {server} MCP server's.")
        return
    if server and changed:
        ui.note(f"{slack.PLUGIN} now posts with this token to {channel}, not with the "
                f"{server} MCP server's token and channel; subagents still use that server.")
    if not (changed or ctx.review):
        ui.note("Nothing changed, so no test message was sent.")
        return
    if not ui.confirm("Send a test message to Slack now?", default=changed):
        return
    with ui.spinner("Sending…"):
        sent = ctx.probes.slack_post(token, channel, slack.TEST_MESSAGE)
    if sent.ok:
        ui.success("sent: it is in Slack, under Apps")
    else:
        ui.warn(f"The test message did not go through: {sent.problem}. "
                "What you entered is saved.")


def _slack_token(ctx: SetupContext, before: str, server: str) -> str:
    """The bot token to use: the one kept, a new one Slack took, or none (the server's)."""
    ui = ctx.ui

    def problem(value: str) -> str | None:
        if not value.strip():
            return None if server else "Required."
        return slack.token_problem(value)

    while True:
        answer = ui.secret("Bot token (xoxb-…)", validate=problem, current=before).strip()
        if not answer or answer == before:
            return answer
        with ui.spinner("Checking the token with Slack…"):
            workspace = ctx.probes.slack_auth_test(answer)
        if workspace.ok:
            ui.success(f"connected to the {workspace.team or 'Slack'} workspace")
        elif workspace.refused:
            ui.error(f"Slack did not take that token: {workspace.problem}")
            continue
        else:
            # Slack never said no: an outage, a rate limit or a proxy must not trap them.
            ui.error(f"That token could not be checked: {workspace.problem}")
            if not ui.confirm("Save it without checking?", default=False):
                continue
        ctx.save({slack.TOKEN_KEY: answer})
        if before:
            ui.note(slack.RESTART_NOTE)
        return answer


def _slack_where(ctx: SetupContext, token: str, *, new: bool, on_file: str, server: str) -> str:
    """Where to send, as an id: one typed or kept, or the member behind an address.

    One question, asked again while an address is not found. `token` is "" when the named
    server's is used, and `new` says it is not the token that was on file.
    """
    ui = ctx.ui
    kept = on_file if slack.keeps(on_file, new_token=new) else ""
    if on_file and not kept:
        ui.note(f"The channel on file ({on_file}) is a direct message with the app the old "
                "token came from, so it is not offered: this token cannot post to it.")
    # Blank leaves the channel to the named server: right only while the token is the
    # server's too, or the one that has been posting there all along.
    blank = bool(server) and not (token and new)

    def problem(value: str) -> str | None:
        value = value.strip()
        if not value:
            if blank:
                return None
            return (f"A token of its own needs somewhere of its own to send, not the {server} "
                    "MCP server's channel." if server else "Required.")
        if "@" not in value:
            return _word(value)
        return None if token else "An id: there is no token of its own to look an address up with."

    default = kept or (ctx.current("USER_GOOGLE_EMAIL") if token else "")
    while True:
        answer = ui.text(SLACK_WHERE, default=default, validate=problem).strip()
        if "@" not in answer:
            return answer
        with ui.spinner("Finding you in Slack…"):
            member = ctx.probes.slack_member(token, answer)
        if member.ok:
            ui.success(f"found {member.name or answer}: messages go to you, from the Keryx app")
            return member.user_id
        ui.warn(f"Slack could not find you by that address: {member.problem}")
        ui.note(slack.MEMBER_ID_HINT)
        default = kept


def run_email(ctx: SetupContext) -> None:
    ui = ctx.ui
    if not google._ensure_client(ctx, calendar=False):
        return
    if token_path(ctx.settings).is_file() and not ctx.review:
        ui.success("Gmail: signed in")
    else:
        google._sign_in_email(ctx)
    if not token_path(ctx.settings).is_file():
        ui.note("Not signed in, so it is left off: `keryx setup` again, or "
                "`keryx auth login gmail` and then `keryx plugins install check_email`.")
        return
    _finish(ctx, "check_email", {})


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
    "check_email": run_email,
    "check_billing": run_billing,
    "cluster_stats": run_cluster,
}
