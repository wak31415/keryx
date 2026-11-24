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

Slack's step asks first what Keryx may do there, because that decides the app: sending
reports and files is always on, and under it is a box to tick for each of the three
things that may be added, each saying what it lets Keryx see (`setup/slack.py`, the same
list `keryx auth login slack` shows). Then it makes the app from the link for exactly
that — or, for an app there already is, shows the manifest that changes it — and checks
each pasted token with Slack; its calls are `Probes` fields too. Ticking a box is also
the yes to the voice tool that goes with it (`slack_read`, `slack_message`), so neither
is in the list of plugins and neither is asked about again. Unticking a box is saved
the moment the boxes are answered, as the command saves it, and a walk that only
unticks asks nothing more. Otherwise it asks the same questions whatever it has already
saved, because going back replays it; the one that comes and goes is the user token,
which is asked for only with reading or sending ticked or while nobody is on file as
the owner, and a replay passes over a recorded answer the step no longer asks for. It
sets up the first workspace; a further one is `keryx auth login slack --workspace NAME`.

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
from keryx.integrations.slack import (
    APP,
    BOT,
    USER,
    Allowed,
    Permission,
    Workspace,
    as_owner,
    manifest_json,
    new_app_url,
    permissions,
    token_kinds,
)
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
    # One that follows another's sign-in (Slack's two that act as the owner) is turned
    # on and off by what is ticked there, so it is not in this list.
    listed = {name: spec for name, spec in plugins.PLUGINS.items() if not spec.follows}
    on = {name: _is_on(ctx, name) for name in listed}
    choices = []
    for name, spec in listed.items():
        disabled = None
        if spec.claude and not claude_ready:
            disabled = f"needs Claude Code: {registry.install_command('claude')}"
        wanted = spec.default or (
            spec.default_when_connected and GOOGLE_READY[name](ctx.settings)
        )
        hint = spec.summary + (" (recommended)" if wanted and not on[name] else "")
        checked = on[name] or (first and wanted and disabled is None)
        choices.append(Choice(name, f"{spec.title} ({name})", hint=hint, checked=checked,
                              disabled=disabled))
    picked = ui.checkbox("Which plugins should Keryx have?", choices)
    # Asked at most once in a walk: three plugins waiting on the same sign-in are one
    # question, not three.
    sign_in_offered = False
    for name, spec in listed.items():
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
    if name == slack.PLUGIN:
        # The two that act as the owner go with it: left on, each would be refused at
        # every call. The same function `keryx plugins remove` calls.
        slack.retire(ctx.settings, ui.note)
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
    """What Keryx may do; each token that takes, checked with Slack; one test message.

    **The boxes come first** (`slack.QUESTION`, under the line that says what is always
    on), because what is ticked decides the app, and each box says what it lets Keryx see
    before any link. After it the questions are always the same ones in the same order,
    whatever is already saved: the app-level token with `requests`, the user token,
    always, and the bot token. That is the order Slack's console shows them in and the
    order the steps have them copied (`integrations.slack.token_kinds`). Nothing is asked
    about where to send, and nothing more about the voice tools: ticking reading or
    sending is the yes to `slack_read` or `slack_message`, turned on and off here with
    the box (`slack.sync_tools`).

    **Asked in the console's order, checked in the order the checks need each other.**
    The bot token says which workspace this is and the user token is held to it, so the
    answers are taken first and then shown to Slack as they always were: the bot token,
    the app-level one, the user's. One Slack does not take is asked for again there.

    **Nothing that is added is saved until every answer is in**, as on the command line:
    what is ticked is all of its tokens or none of it, so a token Slack refuses, or a
    walk abandoned half-way, leaves what was there exactly as it was. **Taking a
    permission away is the one exception, and it is saved the moment the boxes are
    answered** (`_untick_now`, which is `slack.untick`, the command's own): it needs no
    token and nothing from Slack, and somebody who unticks a box and then leaves the
    walk has still said no. A walk that only unticks ends there, with the manifest that
    takes it away in Slack too, and asks for no token.

    Going back runs the step again from a record of its answers (`rewind`), which has to
    find the questions it recorded: so which are asked follows from this run's answers
    and what is on file when the boxes have been answered, and what is saved after that
    changes only what each *offers* — the boxes already ticked, a token to keep, the
    test as a yes or a no. The untick fits that because it is done before any other
    question and cannot be done twice: a replay finds the box already off, so there is
    nothing to take away again, and a walk that only unticked is remembered for the walk
    (`ctx.memo`), so the same boxes end the step the same way and never go on to
    questions the record does not have. Ticking the box again after going back is an
    addition like any other, which waits for its tokens: nothing un-unticks. A token
    typed again after going back is not shown to Slack a second time (`ctx.memo`). The
    other exception is the house rule: the test is not offered on a plain walk that
    changed nothing, so an owner already set up answers what they always did, and a
    review asks it, as a review asks about everything that is set.

    A token that is not the one on file brings its destination with it (`slack.keeps`). A
    direct message's id belongs to the app that opened it, so it is not kept beside
    another token; and beside a named MCP server, a new token never posts to the server's
    channel. Ticking more for an app there is shows its new manifest, never the link
    that would make a second app. With anything ticked no token is saved unchecked: what
    a message is held to, and whose Slack is read, is what Slack said of the token.

    **One owner, fixed at connect, and Slack says who.** The owner is the member the user
    token belongs to (`auth.test`, in the bot token's own workspace), whatever is ticked,
    and reports go to that member unless a destination on file still goes with the bot
    token. An owner on file stands (`slack.standing_owner`), a user token that is
    somebody else's is asked for again, and no question here can change who the owner
    is. **The user token is asked for, and kept, only with reading or sending ticked.**
    With neither it is asked for only while nobody is on file as the owner, shown to
    Slack once, and not saved; a workspace with its owner on file is asked for none.
    **An install from before user tokens walks through untouched**: with the bot token
    kept and nothing ticked, the user token may be left blank, and everything stays as
    it was.
    """
    ui = ctx.ui
    slack.mend(ctx.settings, ui.note)
    values = current_values(ctx, slack.PLUGIN)
    before = ctx.current(slack.TOKEN_KEY)
    # What is ticked now: nothing, for a machine with no app of Keryx's own yet.
    was: Allowed = permissions(values["allow"]) if before else frozenset()
    # What the app is called: the assistant's name, fitted for Slack.
    assistant = ctx.settings.assistant_name
    app = slack.app_of(ctx.settings)
    ui.note(slack.ALWAYS.format(app=app))
    allowed = permissions(ui.checkbox(slack.QUESTION, slack.choices(app, was)))
    if before and was - allowed:
        # Saved here and now, whatever is answered or left after this.
        if not _untick_now(ctx, allowed):
            return
        values = current_values(ctx, slack.PLUGIN)
        was = permissions(values["allow"])
        if allowed == was:
            return
    elif before and allowed == was and ctx.memo.get(_UNTICKED) == allowed:
        # A replay of a walk that only unticked: it is off already, and ends as it did.
        _show_trimmed(ctx, allowed)
        return
    server, on_file = values["mcp_server"], values["channel_id"]
    added = allowed - was
    on_file_now = slack.on_file(ctx.settings, slack.DEFAULT, values)
    # Whose messages count is settled once: a workspace with its owner on file is asked
    # for a user token only when reading or sending as them is ticked.
    owner_known = bool(before) and bool(on_file_now.owner_id)
    acts = as_owner(allowed)
    needed = token_kinds(allowed, owner_known=owner_known)
    # With `requests` every message is held to the workspace and the app Slack names;
    # with anything ticked at all, no token is taken unchecked.
    listens = Permission.REQUESTS in allowed
    strict = bool(allowed)
    for line in slack.meanings(added, app=app, whole=allowed):
        ui.note(line)
    held_user = ctx.current(slack.TOKEN_KEYS[USER])
    if not before:
        ui.link(new_app_url(allowed, assistant=assistant))
        ui.markdown(slack.guide(allowed, app=app))
        if server and not allowed:
            ui.note(f"Leave both tokens blank to go on using the {server} MCP server's "
                    "token and channel.")
    elif added or (USER in needed and not held_user):
        have = tuple(kind for kind in needed if ctx.current(slack.TOKEN_KEYS[kind]))
        ui.note("Slack is connected, so the app you have is changed, not made again."
                if added else
                "Slack is connected, and has no user token on file, which what is ticked "
                "takes. The app you have is updated, not made again."
                if owner_known else
                "Slack is connected, and has no owner on file: it was connected before "
                "Keryx asked for a user token, which is how Slack says which member you "
                "are. The app you have is updated, not made again.")
        ui.markdown(slack.guide(allowed, app=app, raising=True, have=have,
                                owner_known=owner_known))
        ui.panel("The manifest", manifest_json(allowed, assistant=assistant))
        ui.note(slack.RENAME_NOTE.format(app=app))
    # The questions, in the order the console shows the tokens. What the user token is
    # held to is only known once the bot token is in, so both are answered before either
    # is checked. Which are asked follows from what is on file and what is ticked alone.
    typed_app = _ask_slack_app_token(ctx) if APP in needed else None
    typed_user: str | None = None
    if USER in needed:
        # An install from before user tokens, with nothing ticked, may leave it blank.
        may_stay = (bool(before) and not owner_known and not added and not held_user
                    and not strict)
        typed_user = _ask_slack_user_token(
            ctx, blank=may_stay or (bool(server) and not strict), untouched=may_stay)
    token, team = _slack_token(ctx, before, server, strict=strict)
    if team is None:
        # A token that was kept, or one saved unchecked: the workspace is the one on file.
        team = Workspace(team=values["team"], team_id=values["team_id"],
                         user_id=values["bot_user_id"])
        if token and listens and not (team.team_id and team.user_id):
            # An install from before requests never wrote down which workspace it is or
            # which member the app is, and every message typed to it is held to both.
            with ui.spinner("Asking Slack which workspace this is…"):
                asked = ctx.probes.slack_auth_test(token)
            team = asked if asked.ok else team
    if listens and not (team.team_id and team.user_id):
        ui.error("Slack did not say which workspace this is, or which member the app is "
                 "there, and every message you type to it is held to both. Nothing was "
                 "saved: try again when Slack answers.")
        return
    if token != before and (problem := slack.another_workspace(on_file_now, team)):
        # The command line's own check and its own sentence: a token from another
        # workspace would bring another owner with it, whatever is answered after this.
        ui.error(problem[0].upper() + problem[1:] + ".")
        return
    standing = slack.standing_owner(on_file_now, team, same_app=token == before)
    tokens = {BOT: token}
    owner = standing
    if typed_app is not None:
        tokens[APP] = _slack_app_token(ctx, typed_app)
    # With a token and neither of these, the owner on file stands and nothing ticked acts
    # as them: no user token is asked for, and one an earlier build kept is left out of
    # `tokens`, which is what forgets it.
    if token and (acts or not standing):
        # Whose it is has to be asked of Slack: it will read or send as them, or nobody
        # is the owner yet. An install from before user tokens, walked with nothing
        # ticked and nothing changed, may leave it blank and stay exactly as it was.
        untouched = token == before and not added and not held_user and not strict
        shown, owner, team = _slack_user_token(
            ctx, team, standing, allowed, raised=bool(added), optional=untouched,
            bot_token=token, typed=typed_user)
        if shown and acts:
            tokens[USER] = shown
        elif shown:
            # It said who they are, which is all it is for with neither box ticked.
            ui.note(slack.USED_ONCE)
    elif not token and typed_user and typed_user.strip() != held_user:
        ui.note("The user token is left out: it goes with a bot token of Keryx's own app, "
                "and none was given.")
    # Where reports go: where they went, while that still goes with this token; else to
    # the owner themselves. Blank beside a named server's own token: the server's.
    kept = on_file if slack.keeps(on_file, new_token=token != before) else ""
    if on_file and not kept:
        ui.note(f"The channel on file ({on_file}) is a direct message with the app the old "
                "token came from, so it is not kept: this token cannot post to it.")
    channel = kept or (owner if token else "")
    # Blank stays the named server's channel only while the token is the one that has
    # been posting there all along; a new token never posts to the server's channel.
    servers = bool(server) and token == before and not allowed
    if token and not channel and not servers:
        ui.error("Nothing says where reports would go: there is no destination on file "
                 "and no user token to say which member you are. Nothing was saved.")
        return
    if token and channel:
        ui.note((f"It keeps sending to {channel}. " if kept else
                 f"Reports go to you ({channel}). ") + slack.ELSEWHERE)
    if owner:
        ui.note(slack.owner_line(owner, allowed, app) if listens
                else slack.OWNER_NOTED.format(who=owner))
    keys = {kind: ctx.current(key) for kind, key in slack.TOKEN_KEYS.items()}
    changed = (allowed != was or channel != on_file or owner != values["owner_id"]
               or any(tokens[kind] != keys[kind] for kind in tokens))
    if not _save_slack_tokens(ctx, tokens, keys):
        return
    saved = {"channel_id": channel, "allow": [str(each) for each in allowed],
             "owner_id": owner, "team": team.team, "team_id": team.team_id,
             "bot_user_id": team.user_id}
    if not _finish(ctx, slack.PLUGIN, saved):
        return
    if allowed != was:
        # The voice tools follow the ticks: on with the box, off with it, nothing asked.
        slack.sync_tools(ctx.settings, allowed, ui.success)
    # It is on now. From here it either sends a test or says why it did not.
    if not (token and channel):
        whose = "channel is" if token else "token and the channel are"
        ui.note(f"No test message was sent: the {whose} the {server} MCP server's.")
        return
    if server and changed:
        ui.note(f"{slack.PLUGIN} now posts with this token to {channel}, not with the "
                f"{server} MCP server's token and channel, and subagents are told to send "
                "with `keryx slack report`, no longer through that server.")
    if listens and changed:
        ui.note(slack.LISTEN_NOTE.format(app=app))
        if not (slack.is_member(channel) or channel.startswith("D")):
            ui.note(slack.CHANNEL_NOTE.format(channel=channel))
    if not before:
        ui.note("A second workspace has an app of its own: `keryx auth login slack "
                "--workspace NAME`.")
    if not (changed or ctx.review):
        ui.note("Nothing changed, so no test message was sent.")
        return
    if not ui.confirm("Send a test message to Slack now?", default=changed):
        return
    with ui.spinner("Sending…"):
        sent = ctx.probes.slack_post(token, channel, slack.test_message(allowed, app))
    if sent.ok:
        ui.success("sent: it is in Slack, under Apps")
    else:
        ui.warn(f"The test message did not go through: {sent.problem}. "
                "What you entered is saved.")


#: Where a walk remembers that its Slack step only unticked, and down to what.
_UNTICKED = ("slack", "unticked")


def _untick_now(ctx: SetupContext, allowed: Allowed) -> bool:
    """Take away what the boxes just unticked, saved before anything else is asked:
    `slack.untick`, exactly as `keryx auth login slack` does it. False (and said) when
    it could not be saved.

    What is being added still waits for its tokens, and its steps show the manifest, so
    with something added the manifest is left to them. A walk that only unticks is shown
    it here, and is remembered (`_UNTICKED`) so that going back to the boxes and
    answering the same ends the step the same way.
    """
    ui = ctx.ui
    try:
        done = slack.untick(
            ctx.settings, ctx.store, workspace=slack.DEFAULT, allowed=allowed,
            echo=lambda line: ui.note(line.rstrip("\n")), steps_follow=True,
            show=lambda manifest: ui.panel("The manifest", manifest))
    except slack.SlackSetupError as error:
        ui.error(f"What you unticked could not be saved: {error}. `keryx doctor` says how "
                 "Slack stands now.")
        ctx.refresh()
        return False
    ctx.refresh()
    ctx.memo[_UNTICKED] = None if done.added else allowed
    return True


def _show_trimmed(ctx: SetupContext, allowed: Allowed) -> None:
    """What a walk that only unticked is shown when its boxes are answered the same
    again: that it is off already, and the manifest that takes it away in Slack."""
    ui = ctx.ui
    ui.note("That is off on this machine already, from when you unticked it.")
    ui.note(slack.STILL_HOLDS.format(url=slack.APPS_URL))
    ui.panel("The manifest", manifest_json(allowed, assistant=ctx.settings.assistant_name))


def _save_slack_tokens(
    ctx: SetupContext, tokens: dict[str, str], keys: dict[str, str]
) -> bool:
    """Save every token that is new and forget every one that what is ticked does not
    use, in one write; False (and said) when the store refused it."""
    ui = ctx.ui
    new = {slack.TOKEN_KEYS[kind]: token for kind, token in tokens.items()
           if token and token != keys[kind]}
    unused = [slack.TOKEN_KEYS[kind] for kind in keys if kind not in tokens and keys[kind]]
    if not (new or unused):
        return True
    if not ctx.save({**new, **dict.fromkeys(unused)}):
        return False
    if keys[BOT] and slack.TOKEN_KEY in new:
        ui.note(slack.RESTART_NOTE)
    if unused:
        ui.note(f"{' and '.join(unused)} {'are' if len(unused) > 1 else 'is'} forgotten: "
                f"what is ticked does not use {'them' if len(unused) > 1 else 'it'}.")
    return True


def _asked_slack(ctx: SetupContext, what: str, token: str, ask: Callable[[str], Any]) -> Any:
    """What Slack says of `token`, asked once in a walk.

    Nothing is saved until the step's end, so a token typed again after going back is
    not on file to be recognised: this is what recognises it.
    """
    key = (what, token)
    if key not in ctx.memo:
        with ctx.ui.spinner("Checking the token with Slack…"):
            ctx.memo[key] = ask(token)
    return ctx.memo[key]


def _slack_token(
    ctx: SetupContext, before: str, server: str, *, strict: bool
) -> tuple[str, Workspace | None]:
    """The bot token to use: the one kept, a new one Slack took, or none (the server's).

    With it, what Slack said of a new one: the workspace it belongs to. None for a token
    that was kept, or (only with nothing ticked, which is `strict` off) one taken
    unchecked. Nothing is saved here.
    """
    ui = ctx.ui
    above = strict

    def problem(value: str) -> str | None:
        if not value.strip():
            if server and not above:
                return None
            return ("Required: for what you ticked Keryx needs the token of its own app."
                    if server else "Required.")
        return slack.token_problem(value)

    while True:
        answer = ui.secret(slack.prompt_for(BOT, saved=False), validate=problem,
                           current=before).strip()
        if not answer or answer == before:
            return answer, None
        workspace = _asked_slack(ctx, BOT, answer, ctx.probes.slack_auth_test)
        if workspace.ok:
            ui.success(f"connected to the {workspace.team or 'Slack'} workspace")
            return answer, workspace
        ctx.memo.pop((BOT, answer), None)  # a failure is asked about again
        if workspace.refused:
            ui.error(f"Slack did not take that token: {workspace.problem}")
            continue
        ui.error(f"That token could not be checked: {workspace.problem}")
        if above:
            # With anything ticked, what is acted on or read is what Slack names for it.
            ui.note("For what you ticked it has to be checked: try again when Slack "
                    "answers.")
            continue
        # Slack never said no: an outage, a rate limit or a proxy must not trap them.
        if ui.confirm("Save it without checking?", default=False):
            return answer, None


def _ask_slack_app_token(ctx: SetupContext) -> str:
    """The app-level token as typed: held to how one starts, and not yet shown to Slack."""
    def problem(value: str) -> str | None:
        return "Required." if not value.strip() else slack.token_problem(value, APP)

    return ctx.ui.secret(slack.prompt_for(APP, saved=False), validate=problem,
                         current=ctx.current(slack.TOKEN_KEYS[APP])).strip()


def _slack_app_token(ctx: SetupContext, typed: str) -> str:
    """The app-level token Keryx listens with, checked by asking Slack for a connection.

    `typed` is the answer already given, in the console's order, before the bot token;
    one Slack does not take is asked for again here. Never taken unchecked: it is only
    asked for with `requests`. Nothing is saved here.
    """
    ui = ctx.ui
    before = ctx.current(slack.TOKEN_KEYS[APP])
    answer: str | None = typed
    while True:
        if answer is None:
            answer = _ask_slack_app_token(ctx)
        if answer == before:
            return answer
        opened = _asked_slack(ctx, APP, answer, ctx.probes.slack_connection)
        if opened.ok:
            ui.success("Slack takes it: Keryx can listen with it")
            return answer
        ctx.memo.pop((APP, answer), None)
        if opened.refused:
            ui.error(f"Slack did not take the app-level token: {opened.problem}. It needs "
                     "the connections:write scope, and Socket Mode on in the app.")
        else:
            ui.error(f"The app-level token could not be checked: {opened.problem}. For "
                     "what you ticked it has to be: try again when Slack answers.")
        answer = None


#: Said before an install from before user tokens is asked for one.
USER_TOKEN_MAY_STAY = (
    "This workspace was connected before Keryx asked for a user token, and goes on working "
    "without one. Leave it blank, and keep the bot token, to keep it exactly as it is."
)


def _ask_slack_user_token(ctx: SetupContext, *, blank: bool, untouched: bool = False) -> str:
    """The user token as typed: held to how one starts, and not yet shown to Slack.

    `blank` is whether it may be left empty: an install from before user tokens that may
    stay as it is (`untouched`, which is said first), or a bot token that may be left to
    a named MCP server.
    """
    def problem(value: str) -> str | None:
        if not value.strip():
            return None if blank else "Required."
        return slack.token_problem(value, USER)

    if untouched:
        ctx.ui.note(USER_TOKEN_MAY_STAY)
    return ctx.ui.secret(slack.prompt_for(USER, saved=False), validate=problem,
                         current=ctx.current(slack.TOKEN_KEYS[USER])).strip()


def _slack_user_token(ctx: SetupContext, team: Workspace, standing: str,
                      allowed: Allowed = frozenset(), *, raised: bool = False,
                      optional: bool = False, bot_token: str = "",
                      typed: str | None = None,
                      ) -> tuple[str, str, Workspace]:
    """The owner's own token, the member it says they are, and the workspace the bot
    token belongs to (asked of Slack here when the file never learned it, because the
    user token has to be one of the same workspace).

    Never taken unchecked: which member it belongs to is the whole of what it is asked,
    and only Slack can say. Whether it is then saved is the caller's: only with reading
    or sending ticked. A kept one stands for the owner on file, and is asked again
    only when there is none, or when more was ticked (`raised`), because what it may do
    has to have grown with it. One that belongs to another member than the owner on file is
    refused: a token is never how the owner changes. Nothing is saved here. `optional`
    is an install from before user tokens, walked with nothing changed: blank leaves it
    as it was, with the owner (or none) it had.

    `typed` is the answer already given, in the console's order, before the bot token
    was (None: it was not asked for then, and is now). It is checked here, once the bot
    token has said which workspace this is, and asked for again when it will not do.
    """
    ui = ctx.ui
    before = ctx.current(slack.TOKEN_KEYS[USER])
    answer = typed
    while True:
        if answer is None:
            answer = _ask_slack_user_token(ctx, blank=optional, untouched=optional)
        if not answer and optional:
            return "", standing, team
        if not answer:
            # Left blank while it still could be, and the bot token then changed that.
            ui.error("The user token is needed with this bot token: it is how Slack says "
                     "which member you are.")
            answer = None
            continue
        if answer == before and standing and not raised:
            return answer, standing, team
        if not team.team_id and bot_token:
            # Only now, when there is a user token to hold to it: a walk that changes
            # nothing asks Slack nothing.
            asked = _asked_slack(ctx, BOT, bot_token, ctx.probes.slack_auth_test)
            team = asked if asked.ok else team
            if not team.team_id and not allowed:
                ui.note("Slack could not say which workspace this is, so the user token, "
                        "which says which member you are, is asked for another time: "
                        "`keryx auth login slack`.")
                return "", standing, team
        checked = _asked_slack(ctx, USER, answer, ctx.probes.slack_auth_test)
        why = slack.user_token_problem(checked, team) or slack.scope_problem(checked, allowed)
        if why is None and standing and checked.user_id != standing:
            why = (f"it belongs to {checked.user_id}, and the member whose messages Keryx "
                   f"acts on is {standing}: paste your own (`keryx auth logout slack` and a "
                   "new login connects as somebody else)")
        if why:
            ctx.memo.pop((USER, answer), None)
            ui.error(f"That user token cannot be used: {why}")
            answer = None
            continue
        ui.success(f"it is yours: you are {checked.user_id} there")
        if more := slack.beyond_ticked(checked, allowed):
            ui.note(slack.MORE_THAN_TICKED.format(scopes=", ".join(more), named=""))
        return answer, checked.user_id, team


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
    "slack_report": run_slack,
    "check_billing": run_billing,
    "cluster_stats": run_cluster,
}
