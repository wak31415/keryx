"""Connecting Slack: what the wizard's Slack step and `keryx auth login slack` share.

Each owner makes a Slack app of their own, from the manifest in `integrations.slack`, and
pastes its tokens once. **The access level comes first** (`integrations.slack.Access`),
because it decides the manifest: what each level lets Keryx see is said in plain words
(`MEANING`) before any link is shown. Everything after the tokens Keryx does: `auth.test`
says whose each is, `apps.connections.open` shows the app-level one is taken,
`users.lookupByEmail` turns the address they use in Slack into the member to send to
(stored as `channel_id`, so nobody copies a channel id out of Slack), and one test message
shows it arriving.

The wizard asks its questions in `setup/plugins.py::run_slack`, through `Probes`. This
module holds the wording both use and `connect`, the command-line path, which is the same
steps with nothing to ask: the tokens on standard input, one to a line and told apart by
how each starts, the address as `--email`. A token is never an argument, never echoed and
never in an error; what Slack refused is its error code and nothing else it said.

Four rules both paths keep. **A token brings its destination with it** (`keeps`): a direct
message's id (`D…`) belongs to the app that opened it, so a token that is not the one on
file is never paired with one — not the one in the plugin's file, and not a named MCP
server's — and the owner is looked up again instead. A member's id or a channel's is the
workspace's and is kept. **Nothing is called connected that was not shown to arrive**:
either the test message went, or the last line says none was sent and why. **A level is
all of its tokens or none of it**: every token the level needs is checked before anything
is saved, one the level does not use is refused by name, and lowering the level forgets
the tokens it no longer uses. **An app is never made twice**: raising the level of a
workspace that is connected prints the new manifest to paste into the app there is.

**What sets Keryx to work is a mention, from the owner.** The app's own member id
(`bot_user_id`, what `auth.test` says of the bot token) is stored at connect, because a
message is acted on only when it mentions it; a workspace above `announce` without it
hears nothing.

**One owner, fixed at connect** (the owner's ruling of 2026-11-23). The owner is whoever
set the connection up, and `owner_id` (the one member whose messages may start work with
no PIN) is established once: at `read` and above from the user token's own member, and
at `converse` from the address given at connect, or a destination already on file that
is a member's id, said aloud. After that nothing changes it in place: not `--email` or
the wizard's "where to send", which only ever choose where reports go; not a user token,
which is refused when it is somebody else's; not a bot token from another workspace,
which is refused too. There is no option for it. `keryx auth logout slack` and a new
login is the one way to connect as somebody else. `OWNER_LINE` names the member on every
connect at `converse` or above.

A second workspace is the same flow with `--workspace NAME`: its own app from the same
manifest, its own level, tokens, member id and destination (`plugins.slack`), and
`keryx auth logout slack [--workspace NAME]` (`logout`) forgets one.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from keryx import plugins
from keryx.config import Settings
from keryx.config.permissions import OWNER
from keryx.config.settings import SLACK_WORKSPACE_NAME
from keryx.config.store import FROM_ENV, ConfigError, ConfigStore
from keryx.integrations import slack
from keryx.integrations.slack import (
    APP,
    BOT,
    TOKEN_PREFIXES,
    USER,
    Access,
    Connection,
    Member,
    SlackResult,
    Workspace,
    token_kinds,
)
from keryx.plugins import slack as plugin
from keryx.plugins.slack import DEFAULT, SlackWorkspace

PLUGIN = "slack_report"
TOKEN_KEY = "SLACK_BOT_TOKEN"
#: Where the first workspace's tokens are stored, by kind.
TOKEN_KEYS = {BOT: TOKEN_KEY, APP: "SLACK_APP_TOKEN", USER: "SLACK_USER_TOKEN"}
#: And every further workspace's, in one table.
WORKSPACE_TOKENS_KEY = "SLACK_WORKSPACE_TOKENS"
#: What each token is called when it is asked for or refused.
TOKEN_NAMES = {BOT: "bot token", APP: "app-level token", USER: "user token"}
APPS_URL = "https://api.slack.com/apps"

#: What Keryx can see at each level, and where it can be set to work from, in plain
#: words: said before the link is shown.
MEANING = {
    Access.ANNOUNCE: "Keryx posts to you on Slack and reads nothing there.",
    Access.CONVERSE: (
        "Keryx posts to you, and is sent the direct messages you type to the Keryx app "
        "itself: nothing else in Slack. It acts on one only when it is yours and mentions "
        "the app (@Keryx): a new message starts work, and a reply under a task's report "
        "resumes that task, both without the phone PIN."
    ),
    Access.READ: (
        "Everything in converse. You can also mention the app (@Keryx) in any conversation "
        "it has been added to: every message that mentions it there is sent to Keryx, "
        "anyone's but yours is dropped, and yours is answered in that thread, where the "
        "others in the conversation read the answer too. And Keryx can read Slack as you, "
        "only when you ask it about messages: every channel, private channel, direct "
        "message and group message you are in, their lists, the member list, and search."
    ),
    Access.WRITE: (
        "Everything in read. Keryx can also send messages as you, in your name, to other "
        "people and to channels you are in: a new message or a reply in a thread, only "
        "when you ask for that message, and on a call only after it has said who it goes "
        "to and the words, and you have said yes. A coding agent working on a request of "
        "yours can send one too."
    ),
}
#: The same in a few words, beside each option of the wizard's question.
SHORT = {
    Access.ANNOUNCE: "Keryx posts to you and reads nothing",
    Access.CONVERSE: "also acts on a message you send the Keryx app that mentions it "
                     "(@Keryx): it can resume or start work without the PIN",
    Access.READ: "also answers your mention in a conversation it was added to, there, "
                 "and reads Slack as you when you ask it about messages",
    Access.WRITE: "also sends a message as you, to a person or a channel, when you ask "
                  "for that message",
}
#: Said at `read`: when Slack is read as the owner, and what turns that on.
READ_NOTE = (
    "Keryx reads Slack as you only when you ask about messages: on a call once "
    "`keryx plugins install slack_read` has turned that on, and in a task through "
    "`keryx slack`, which a coding agent runs when your request is about Slack."
)

#: Said at `write`: what sending as the owner is, and what turns it on.
WRITE_NOTE = (
    "A message Keryx sends at this level is yours: other people read it as written by "
    "you. On a call that is `keryx plugins install slack_message`, which always asks "
    "before it sends; in a task it is `keryx slack message`, which a coding agent runs "
    "only when your request asks for that message to that person or channel."
)

#: The steps in Slack, after the link. Markdown; `plain` is the same without the marks.
STEPS = (
    "Open the link: it is Slack's page for a new app, with Keryx's already filled in. "
    "Pick your workspace and click through to **Create**.",
    "Click **Install to Workspace**, then **Allow**.",
    "Under **OAuth & Permissions**, copy the **Bot User OAuth Token** (`xoxb-…`).",
)
#: The steps that raise the level of the app there is, in place of the first two above.
RAISE_STEPS = (
    f"Open {APPS_URL} and pick the Keryx app you already have. Do not create a second one.",
    "Open **App Manifest**, replace what is there with the manifest below, and click "
    "**Save Changes**.",
    "Open **Install App** and click **Reinstall to Workspace**, then **Allow**: the new "
    "permissions count only after that.",
)
#: Where each token is found in the app's settings.
TOKEN_STEPS = {
    BOT: STEPS[2],
    APP: "Under **Basic Information**, **App-Level Tokens**, click **Generate Token and "
         "Scopes**, add the `connections:write` scope, and copy the token (`xapp-…`).",
    USER: "Under **OAuth & Permissions**, copy the **User OAuth Token** (`xoxp-…`).",
}

#: Said with the question that asks for an id by hand, when Slack could not find them.
MEMBER_ID_HINT = (
    "A member id works as well as a channel id: in Slack, open your profile, click More "
    "(⋮), then Copy member ID."
)

TEST_MESSAGE = (
    "Keryx is connected. What you ask for in writing on a call arrives here, and so does "
    "each finished task's report."
)
#: Added to it where a message typed back is acted on.
CONVERSE_TEST_MESSAGE = (
    " Mention the app (@Keryx) to set it to work: in a reply under a report to resume "
    "that task, or in a new message here to start one."
)

BY_HAND = f"`keryx plugins install {PLUGIN} --set channel_id=ID` with a channel or member id"

#: Said when a token replaces another: `plugins.secret` prefers the settings a process
#: started with, so a running service goes on using the old one.
RESTART_NOTE = "A Keryx that started with the old token keeps it until `keryx restart`."
#: Said when a workspace starts being listened to, or its listening changes.
LISTEN_NOTE = "Keryx starts listening for your messages there at its next start: `keryx restart`."
#: Said at `converse` and above, every time: whose messages may start work with no PIN.
OWNER_LINE = (
    "Only messages from {who} that mention the app (@Keryx) are acted on there: that "
    "member id is taken to be you, for as long as this workspace is connected."
)
#: Said when the destination is a channel: only the app's direct messages are heard.
CHANNEL_NOTE = (
    "Reports go to {channel}, which is not your direct messages with the app: a reply "
    "typed there is not seen. Messages you send the app directly are."
)

_MEMBER = re.compile(r"[UW][A-Z0-9]{2,31}")


class SlackSetupError(RuntimeError):
    """Connecting Slack could not go on, in a sentence that never holds a token."""


@dataclass(frozen=True)
class SlackApi:
    """The calls `connect` makes, replaced in the tests."""

    auth_test: Callable[[str], Workspace] = slack.auth_test
    lookup_by_email: Callable[[str, str], Member] = slack.lookup_by_email
    post_text: Callable[[str, str, str], SlackResult] = slack.post_text
    open_connection: Callable[[str], Connection] = slack.open_connection


def token_problem(value: str, kind: str = BOT) -> str | None:
    """Why `value` cannot be that token, before Slack is asked; None when it could be.

    A bot token is held to `xox`, as it always was: an older one may not start `xoxb-`.
    """
    prefix = "xox" if kind == BOT else TOKEN_PREFIXES[kind]
    if value.strip().startswith(prefix):
        return None
    return f"{_a(TOKEN_NAMES[kind]).capitalize()} starts {TOKEN_PREFIXES[kind]}"


def is_member(value: str) -> bool:
    """Whether `value` is a member's id, as opposed to a conversation's."""
    return _MEMBER.fullmatch(value) is not None


def parse_access(value: str | None) -> Access | None:
    """`--access` as a level, or None when none was given."""
    if value is None:
        return None
    try:
        return Access(value.strip().lower())
    except ValueError:
        levels = ", ".join(slack.LEVELS)
        raise SlackSetupError(f"there is no access level {value!r}: one of {levels}") from None


def workspace_name(value: str | None) -> str:
    """`--workspace` as the name everything is stored under; none is the first workspace."""
    name = (value or DEFAULT).strip().lower()
    if name != DEFAULT and not SLACK_WORKSPACE_NAME.fullmatch(name):
        raise SlackSetupError(
            "a workspace's name is a short word of your own (lower-case letters, digits, "
            "_ and -), like work"
        )
    return name


def steps(access: Access, *, raising: bool = False, have: tuple[str, ...] = ()) -> list[str]:
    """What to do in Slack for `access`, as Markdown lines without their numbers.

    `raising` is the app there is, changed in place; `have` is the tokens already on
    file, which that leaves as they are and which are not asked for again.
    """
    lines = list(RAISE_STEPS if raising else STEPS[:2])
    lines += [TOKEN_STEPS[kind] for kind in token_kinds(access) if kind not in have]
    return lines


def guide(access: Access = Access.ANNOUNCE, *, raising: bool = False,
          have: tuple[str, ...] = ()) -> str:
    """The steps, as Markdown for the wizard, which shows the link above them."""
    return "\n".join(
        f"{number}. {step}"
        for number, step in enumerate(steps(access, raising=raising, have=have), 1)
    )


def plain(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def keeps(channel: str, *, new_token: bool) -> bool:
    """Whether the destination on file still stands for the token about to be used.

    A direct message's id belongs to the app that opened it, so a different token is
    presumed to be another app's and may not be paired with it. A member's id or a
    channel's is the workspace's, and stands whatever the token.
    """
    return bool(channel) and not (new_token and slack.is_dm(channel))


def _config(settings: Settings) -> dict[str, Any]:
    try:
        return plugins.read_config(settings, PLUGIN)
    except plugins.PluginConfigError as error:
        raise SlackSetupError(str(error)) from None


def on_file(
    settings: Settings, name: str, values: Mapping[str, Any] | None = None
) -> SlackWorkspace:
    """Workspace `name` as it is saved now: `plugins.slack.on_file`, which is where a
    workspace's settings and its tokens are joined, refused in a sentence here."""
    return plugin.on_file(settings, name, _config(settings) if values is None else values)


def without_email(settings: Settings, name: str = DEFAULT) -> str:
    """What `connect` does when no `--email` is given, in a sentence, as things stand."""
    try:
        channel = on_file(settings, name).channel
    except SlackSetupError:
        channel = ""
    address = settings.user_google_email
    looks = (f"without --email it looks for {address}" if address
             else "without --email there is no address to look for, and it stops")
    if not channel:
        return looks[0].upper() + looks[1:] + "."
    if slack.is_dm(channel):
        return (f"The channel on file ({channel}) is a direct message with the app its token "
                f"came from, so a different token does not keep it: {looks}.")
    return f"Without --email it keeps sending to {channel}."


def _flags(name: str, access: Access, current: Access) -> str:
    """The options that name this workspace and level again on the next command."""
    flags = "" if name == DEFAULT else f" --workspace {name}"
    return flags + ("" if access is current else f" --access {access}")


def start(
    settings: Settings,
    echo: Callable[[str], None],
    *,
    workspace: str = DEFAULT,
    access: Access | None = None,
) -> None:
    """`keryx auth login slack` with no token: what the level means, then the link (or
    the manifest, for an app there already is), the steps, and the next command. A
    workspace that is connected at that level already is told so, and never shown the
    link that would make a second app."""
    try:
        values = _config(settings)
        saved = on_file(settings, workspace, values)
        first_missing = plugins.plugin(PLUGIN).problem(dict(values)) is not None
    except SlackSetupError:
        saved, first_missing = SlackWorkspace(workspace), True
    connected = bool(saved.token(BOT))
    if workspace != DEFAULT and first_missing:
        echo(f"{workspace} would be added beside your first workspace, and there is none "
             "yet: run this without --workspace first.\n")
    current = saved.access
    chosen = access is not None or connected
    access = access or current
    if connected and access is current:
        _already(saved, echo)
        return
    raising = connected and access.reaches(current)
    have = tuple(kind for kind in token_kinds(access) if saved.token(kind)) if raising else ()
    if chosen:
        echo(f"Access level {access}: {MEANING[access]}")
    else:
        # Nothing is connected and no level was named: say all three before the link,
        # because the level decides the app that link creates.
        echo("What Keryx may see in your Slack is decided now, because it decides the app:\n")
        for level in slack.LEVELS:
            echo(f"  {level}: {MEANING[level]}")
        others = " or ".join(f"--access {level}" for level in slack.LEVELS if level is not access)
        echo(f"\nWhat follows is {access}; {others} chooses another.")
    if access.reaches(Access.READ):
        echo(READ_NOTE)
    if access.reaches(Access.WRITE):
        echo(WRITE_NOTE)
    echo("")
    if raising:
        echo(f"{saved.label} is connected at the {current} level, so the app you have is "
             "changed, not made again:\n")
    else:
        echo("Create your Slack app from this link:\n")
        echo(slack.new_app_url(access))
        echo("")
    for number, step in enumerate(steps(access, raising=raising, have=have), 1):
        echo(f"{number}. {plain(step)}")
    if raising:
        echo("\nThe manifest:\n")
        echo(slack.manifest_json(access))
        if BOT in have:
            echo("\nThe bot token stays as it is.")
    wanted = [kind for kind in token_kinds(access) if kind not in have]
    many = len(wanted) > 1
    address = "" if access.reaches(Access.READ) else " --email ADDRESS"
    command = f"keryx auth login slack --stdin{_flags(workspace, access, current)}{address}"
    if wanted:
        what = "the tokens" if many else f"the {TOKEN_NAMES[wanted[0]]}"
        echo(
            f"\nThen hand {what} over on standard input, never on the command line"
            + (", one to a line in any order" if many else "")
            + ("" if access.reaches(Access.READ) else ", with the address you use in Slack")
            + f":\n  {command}"
        )
    else:
        echo("\nEvery token this level needs is saved already, so there is nothing to "
             f"hand over. Once the app is reinstalled:\n  {command} < /dev/null")
        if USER in have:
            echo("If Slack shows a different User OAuth Token after the reinstall, hand "
                 "that one over instead of nothing.")
    if access.reaches(Access.READ):
        echo("The user token says which member you are, so no address is needed.")
    else:
        echo(without_email(settings, workspace))


def _already(saved: SlackWorkspace, echo: Callable[[str], None]) -> None:
    """What a workspace that is connected at the level asked for is told: how it stands,
    and the three things that change it. Never the link that makes a second app."""
    named = "" if saved.name == DEFAULT else f" --workspace {saved.name}"
    echo(f"{saved.label} is connected at the {saved.access} level: {MEANING[saved.access]}")
    if saved.missing:
        echo(f"It is without {' or '.join(saved.missing)}.")
    if saved.access.reaches(Access.CONVERSE) and saved.owner_id:
        echo(OWNER_LINE.format(who=saved.owner_id))
        echo(f"To connect as somebody else: keryx auth logout slack{named}, then log in "
             "again.")
    others = [level for level in slack.LEVELS if level is not saved.access]
    echo(
        f"\nTo change what Keryx may see there: keryx auth login slack{named} --access "
        f"{' or --access '.join(others)}"
        f"\nTo replace a token: keryx auth login slack --stdin{named}, with the new one "
        "on standard input"
        f"\nTo disconnect it: keryx auth logout slack{named}"
    )


def lower(
    settings: Settings,
    store: ConfigStore,
    *,
    workspace: str,
    access: Access,
    echo: Callable[[str], None],
) -> bool:
    """Lower a connected workspace to `access`, forgetting the tokens it no longer uses.

    True when that is what was asked and it was done; False when this is not a lowering
    (not connected, or not a lower level), and nothing was touched. Slack is not asked
    anything: taking access away needs nothing from it.
    """
    values = _config(settings)
    saved = on_file(settings, workspace, values)
    if not saved.token(BOT) or access is saved.access or access.reaches(saved.access):
        return False
    kept = {kind: saved.tokens[kind] for kind in token_kinds(access) if saved.token(kind)}
    dropped = [kind for kind in saved.tokens if kind not in kept]
    _save_tokens(store, settings, workspace, kept)
    _write_workspace(settings, values, workspace, {"access": str(access)})
    echo(f"{saved.label} is now at the {access} level: {MEANING[access]}")
    if dropped:
        names = " and ".join(_stored_as(workspace, kind) for kind in dropped)
        echo(f"{names} {'are' if len(dropped) > 1 else 'is'} forgotten.")
    # Said of what really stops: at converse Keryx goes on acting on their messages.
    stops = ("A running Keryx stops acting on your messages there at once."
             if access is Access.ANNOUNCE else
             "Keryx no longer holds your user token, so nothing reads Slack as you; it "
             "still acts on the messages you type to the app.")
    echo(f"{stops} The app in Slack keeps the permissions it was made with: to take those "
         "away too, paste this manifest into its App Manifest page and reinstall it:\n")
    echo(slack.manifest_json(access))
    return True


def logout(
    settings: Settings, store: ConfigStore, *, workspace: str, echo: Callable[[str], None]
) -> None:
    """Disconnect a workspace: forget its tokens and its settings. Slack is not asked.

    A further workspace loses its table and its entry among the tokens. The first loses
    its three tokens and everything Slack said of it, and `slack_report` is turned off
    unless a named MCP server still carries it. Nothing is revoked at Slack: the app and
    its tokens are the owner's own, and removing the app there is what ends them.
    """
    values = _config(settings)
    saved = on_file(settings, workspace, values)
    others = [name for name in values["workspaces"] if name != DEFAULT]
    held = bool(saved.tokens) or (workspace in others)
    if workspace != DEFAULT:
        if not held:
            raise SlackSetupError(
                f"there is no workspace called {workspace}"
                + (f" (there is {', '.join(others)})" if others else "")
            )
        _save_tokens(store, settings, workspace, None)
        tables = {name: table for name, table in values["workspaces"].items()
                  if name != workspace}
        try:
            plugins.write_config(settings, PLUGIN, {"workspaces": tables})
        except plugins.PluginConfigError as error:
            raise SlackSetupError(f"its tokens are removed, but {error}") from None
        echo(f"{saved.label} is disconnected: its tokens and its settings are removed.")
    else:
        if others:
            raise SlackSetupError(
                "your first workspace is the one everything is sent to, and "
                f"{', '.join(others)} would be left with none: disconnect "
                f"{'them' if len(others) > 1 else 'it'} first, with --workspace NAME"
            )
        if not (held or saved.channel):
            echo("Slack is not connected: there is nothing to remove.")
            return
        _save_tokens(store, settings, workspace, {})
        server = values["mcp_server"]
        try:
            plugins.write_config(settings, PLUGIN, {
                "channel_id": "", "access": str(Access.ANNOUNCE), "owner_id": "", "team": "",
                "team_id": "", "bot_user_id": "",
            })
        except plugins.PluginConfigError as error:
            raise SlackSetupError(f"its tokens are removed, but {error}") from None
        echo(f"{saved.label} is disconnected: {', '.join(TOKEN_KEYS.values())} and what "
             "Slack said of it are removed.")
        if server:
            echo(f"{PLUGIN} stays on with the {server} MCP server's token and channel.")
        else:
            plugins.remove(settings, PLUGIN)
            echo(f"{PLUGIN} is off.")
    echo("A running Keryx stops using it at once. The app itself is still installed in "
         f"Slack, and its tokens work until you remove it there: {APPS_URL}")


def _stored_as(workspace: str, kind: str) -> str:
    """Where one token is kept, by the name the owner would unset it under."""
    if workspace == DEFAULT:
        return TOKEN_KEYS[kind]
    return f"{workspace}'s {TOKEN_NAMES[kind]}"


def _stored_table(store: ConfigStore) -> dict[str, dict[str, str]]:
    """Every further workspace's tokens as the store holds them: one line of JSON."""
    held = store.stored().get(WORKSPACE_TOKENS_KEY) or {}
    if isinstance(held, str):
        try:
            held = json.loads(held)
        except ValueError:
            held = {}
    return {str(name): dict(tokens) for name, tokens in held.items()} if isinstance(
        held, dict) else {}


def _save_tokens(
    store: ConfigStore, settings: Settings, workspace: str, tokens: Mapping[str, str] | None
) -> None:
    """Store exactly `tokens` for `workspace`, in one write: what is not there is unset.

    None removes a further workspace's entry altogether.
    """
    if workspace == DEFAULT:
        tokens = tokens or {}
        values: dict[str, Any] = {key: tokens.get(kind) for kind, key in TOKEN_KEYS.items()}
    else:
        table = _stored_table(store)
        table.pop(workspace, None)
        if tokens is not None:
            table[workspace] = dict(tokens)
        values = {WORKSPACE_TOKENS_KEY: table}
    try:
        store.set(values, actor=OWNER, settings=settings)
    except ConfigError as error:
        raise SlackSetupError(str(error)) from None


def _write_workspace(
    settings: Settings, values: Mapping[str, Any], workspace: str, changes: Mapping[str, Any]
) -> None:
    """Write `changes` into the workspace's part of the plugin's file."""
    if workspace == DEFAULT:
        plugins.write_config(settings, PLUGIN, changes)
        return
    tables = {name: dict(table) for name, table in values["workspaces"].items()}
    tables[workspace] = {**tables.get(workspace, {}), **changes}
    plugins.write_config(settings, PLUGIN, {"workspaces": tables})


def wanted_tokens(
    settings: Settings, workspace: str | None, access: str | None
) -> list[tuple[str, bool]]:
    """Each token the level needs, as a terminal asks for it, and whether one is saved."""
    saved = on_file(settings, workspace_name(workspace))
    level = parse_access(access) or saved.access
    return [(f"{TOKEN_NAMES[kind].capitalize()} ({TOKEN_PREFIXES[kind]}…)",
             bool(saved.token(kind)))
            for kind in token_kinds(level)]


def read_tokens(text: str) -> dict[str, str]:
    """The tokens on standard input, by kind: one to a line, in any order.

    Raises `SlackSetupError` for a line that is none of the three, or a kind given twice.
    A bot token is told by `xox` alone when it is the only line, as it always was.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found: dict[str, str] = {}
    for line in lines:
        kind = slack.token_kind(line)
        if kind is None:
            if len(lines) == 1 and line.startswith("xox"):
                kind = BOT
            else:
                raise SlackSetupError(
                    "that is not a Slack token: a bot token starts xoxb-, an app-level "
                    "token xapp- and a user token xoxp-"
                )
        if kind in found:
            raise SlackSetupError(f"two {TOKEN_NAMES[kind]}s on standard input; one of each")
        found[kind] = line
    return found


def standing_owner(saved: SlackWorkspace, team: Workspace, *, same_app: bool) -> str:
    """The owner on file, when it is this workspace's: it stands, whatever is looked up.

    Whose messages may start work with no PIN is never changed by changing where reports
    go. An owner on file for the same team (or, where the file never learned the team,
    for the same app) is the owner until the workspace is logged out.
    """
    if not saved.owner_id:
        return ""
    same_team = bool(team.team_id) and saved.team_id == team.team_id
    return saved.owner_id if same_team or (not saved.team_id and same_app) else ""


def connect(
    settings: Settings,
    store: ConfigStore,
    *,
    tokens: str,
    email: str | None,
    test: bool,
    echo: Callable[[str], None],
    api: SlackApi | None = None,
    workspace: str = DEFAULT,
    access: Access | None = None,
) -> None:
    """Check the tokens, find where to send, save it all, turn the plugin on, and prove it.

    `tokens` is standard input: one token to a line, told apart by how each starts. A
    token the level needs and that is on file already need not be given again. Raises
    `SlackSetupError` saying how far it got. Nothing is saved until every token the level
    needs is one Slack takes *and* there is a destination that goes with the bot token, so
    a token Slack refuses or an address it cannot find leaves a working install exactly as
    it was. A token that is not the one on file brings its destination with it (`keeps`):
    an address is looked up, never a direct message's old id kept. After the save, only
    the test message can fail, and that says so and undoes nothing.

    **Where reports go and whose messages are acted on are two things.** `email` is the
    first, and the second only the first time, when nobody is on file: after that the
    owner stands (`standing_owner`) until the workspace is logged out.
    """
    api = api or SlackApi()
    given = read_tokens(tokens)
    values = _config(settings)
    saved = on_file(settings, workspace, values)
    server = values["mcp_server"] if workspace == DEFAULT else ""
    if workspace != DEFAULT and (problem := plugins.plugin(PLUGIN).problem(dict(values))):
        raise SlackSetupError(
            f"{workspace} is added beside your first workspace, and there is none yet "
            f"({problem}): run it without --workspace first. Nothing was saved"
        )
    access = access or saved.access
    needed = token_kinds(access)
    if extra := [kind for kind in given if kind not in needed]:
        names = " or ".join(f"{_a(TOKEN_NAMES[kind])} ({TOKEN_PREFIXES[kind]})" for kind in extra)
        raise SlackSetupError(
            f"the {access} level does not use {names}: leave it out, or choose a higher "
            "level with --access. Nothing was saved"
        )
    held = {kind: given.get(kind) or saved.token(kind) or "" for kind in needed}
    if lacking := [kind for kind in needed if not held[kind]]:
        if not given:
            raise SlackSetupError("no token on standard input")
        names = " and ".join(f"the {TOKEN_NAMES[kind]} ({TOKEN_PREFIXES[kind]})"
                             for kind in lacking)
        raise SlackSetupError(f"the {access} level also needs {names}, one to a line. "
                              "Nothing was saved")
    before = saved.token(BOT) or ""
    token = held[BOT]
    # An address that was asked for replaces the destination; otherwise one is looked for
    # only when what is on file does not stand for this token.
    address = (email or "").strip()
    kept = saved.channel if not address and keeps(saved.channel, new_token=token != before) else ""
    by_token = access.reaches(Access.READ)  # the user token says who they are, and so where to send
    if not (address or kept or by_token):
        address = settings.user_google_email or ""
    if not (address or kept or by_token):
        raise SlackSetupError(f"{_nowhere(saved.channel, server)}: add --email ADDRESS, "
                              "the one you use in Slack. Nothing was saved")

    team = api.auth_test(token)
    if not team.ok:
        why = team.problem if team.refused else (
            f"the token could not be checked ({team.problem})")
        raise SlackSetupError(f"{why}; nothing was saved")
    if access.reaches(Access.CONVERSE) and not (team.team_id and team.user_id):
        raise SlackSetupError(
            "Slack did not say which workspace the bot token belongs to, or which member "
            "the app is there, and above announce every message is held to both. Nothing "
            "was saved"
        )
    if saved.owner_id and saved.team_id and saved.team_id != team.team_id:
        # Another workspace's token would bring another owner with it, and the owner is
        # fixed for as long as a workspace is connected.
        named = "" if workspace == DEFAULT else f" --workspace {workspace}"
        raise SlackSetupError(
            f"that bot token is for {team.team or 'another workspace'}, and {saved.label} "
            f"is connected here: `keryx auth logout slack{named}` first, then log in "
            "again. Nothing was saved"
        )
    token_owner = ""
    if USER in needed:
        token_owner = _check_user_token(api, held[USER], team, access)
    if APP in needed:
        opened = api.open_connection(held[APP])
        if not opened.ok:
            raise SlackSetupError(
                f"the app-level token could not be used ({opened.problem}): it needs the "
                "connections:write scope, and Socket Mode on in the app. Nothing was saved"
            )
    found = None
    if address:
        found = api.lookup_by_email(token, address)
        if not found.ok:
            raise SlackSetupError(
                f"Slack could not find {address}: {found.problem}. Nothing was saved. Try "
                f"another --email, or by hand: `keryx config set {TOKEN_KEY} --stdin`, "
                f"then {BY_HAND}"
            )
    channel = found.user_id if found is not None else (kept or token_owner)
    acted_on, who = _owner(
        access=access, standing=standing_owner(saved, team, same_app=token == before),
        token_owner=token_owner, found=found, channel=channel,
        named="" if workspace == DEFAULT else f" --workspace {workspace}",
    )

    _save_tokens(store, settings, workspace, held)
    echo(_connected_line(workspace, team, access, needed))
    if workspace == DEFAULT and store.source_of(TOKEN_KEY, settings) == FROM_ENV:
        echo(f"note: {TOKEN_KEY} is also set in the environment, which wins")
    if workspace == DEFAULT and before and token != before:
        # Only the first workspace's: a further one's tokens are read from the store
        # every time they are asked for.
        echo(RESTART_NOTE)
    if workspace == DEFAULT:
        settings = settings.model_copy(update={"slack_bot_token": token})
    if found is not None:
        echo(f"found {found.name or address} ({channel}): messages go to them")
    elif kept:
        echo(f"it keeps sending to {channel}")
    else:
        echo(f"you are {channel} there: messages go to you")
    if access.reaches(Access.CONVERSE):
        echo(OWNER_LINE.format(who=who))
    try:
        _write_workspace(settings, values, workspace, {
            "channel_id": channel,
            "access": str(access),
            "owner_id": acted_on,
            "team": team.team,
            "team_id": team.team_id,
            "bot_user_id": team.user_id,
        })
        plugins.install(settings, PLUGIN)
    except plugins.PluginConfigError as error:
        raise SlackSetupError(f"the token is saved, but {error}") from None
    status = next(one for one in plugins.status(settings) if one.name == PLUGIN)
    if not status.on:
        raise SlackSetupError(f"{PLUGIN} is installed but refused: {status.refused}")
    echo(f"{PLUGIN} is on, from the next call")
    if server:
        echo(f"note: {PLUGIN} now posts with this token to {channel}, not with the "
             f"{server} MCP server's token and channel; subagents still use that server")
    if access.reaches(Access.CONVERSE):
        echo(LISTEN_NOTE)
        if not (is_member(channel) or slack.is_dm(channel)):
            echo(CHANNEL_NOTE.format(channel=channel))
    if access.reaches(Access.READ):
        echo(READ_NOTE)
    if access.reaches(Access.WRITE):
        echo(WRITE_NOTE)
    for kind in saved.tokens:
        if kind not in needed:
            echo(f"{_stored_as(workspace, kind)} is forgotten: the {access} level does not "
                 "use it")

    if not test:
        echo("no test message was sent (--no-test), so nothing has shown it arrives")
        return
    sent = api.post_text(token, channel, test_message(access))
    if not sent.ok:
        elsewhere = "" if found is not None else (
            ". To send to yourself instead, run it again with --email ADDRESS")
        raise SlackSetupError(
            f"everything is saved, but the test message to {channel} did not go through: "
            f"{sent.problem}{elsewhere}"
        )
    echo("sent a test message: it is in Slack, under Apps")


def _owner(
    *,
    access: Access,
    standing: str,
    token_owner: str,
    found: Member | None,
    channel: str,
    named: str,
) -> tuple[str, str]:
    """The member whose messages are acted on, and how to name them in a line.

    The owner on file stands. Only when there is none is one established: whom the user
    token belongs to, else the member the address found, else a destination that is a
    member's id. Below `converse` nobody's messages are acted on, so nothing is
    established: an owner on file is kept for the day the level is raised. Nothing here
    replaces an owner: that is `keryx auth logout slack` and a new login.
    """
    if standing:
        acted_on = standing
    elif not access.reaches(Access.CONVERSE):
        return "", ""
    else:
        acted_on = token_owner or (found.user_id if found is not None else "") or (
            channel if is_member(channel) else "")
    if token_owner and acted_on != token_owner:
        raise SlackSetupError(
            f"the user token belongs to {token_owner}, and the member whose messages Keryx "
            f"acts on there is {acted_on}: paste your own user token. To connect as "
            f"somebody else, `keryx auth logout slack{named}` first. Nothing was saved"
        )
    if access.reaches(Access.CONVERSE) and not acted_on:
        raise SlackSetupError(
            f"at the {access} level Keryx acts only on messages from your own member id, "
            f"and {channel} is not one: add --email ADDRESS, the one you use in Slack. "
            "Nothing was saved"
        )
    name = found.name if found is not None and found.user_id == acted_on else ""
    return acted_on, f"{name} ({acted_on})" if name else acted_on


def test_message(access: Access) -> str:
    """The one message that shows it arriving, saying what a reply to it does."""
    return TEST_MESSAGE + (CONVERSE_TEST_MESSAGE if access.reaches(Access.CONVERSE) else "")


def _a(name: str) -> str:
    return f"{'an' if name.startswith('app') else 'a'} {name}"


def user_token_problem(checked: Workspace, team: Workspace) -> str | None:
    """Why a user token Slack answered for cannot be this workspace's owner's; None when
    it can. `team` is what the bot token's own check said."""
    if not checked.ok:
        return checked.problem if checked.refused else (
            f"it could not be checked ({checked.problem})")
    if not checked.user_id:
        return "Slack did not say which member it belongs to"
    if not team.team_id:
        # The same workspace has to be shown, never assumed.
        return "which workspace the bot token belongs to is not known, so it cannot be matched"
    if checked.team_id != team.team_id:
        return ("it belongs to another workspace than the bot token"
                + (f" ({checked.team})" if checked.team else ""))
    return None


def scope_problem(checked: Workspace, access: Access) -> str | None:
    """What a user token Slack answered for may not yet do that `access` needs; None when
    it may, and when Slack did not say what it may do (which is then found out in use)."""
    if checked.scopes is None:
        return None
    lacking = [scope for scope in slack.user_scopes(access) if scope not in checked.scopes]
    if not lacking:
        return None
    return (f"it may not yet do what the {access} level needs (it lacks "
            f"{', '.join(lacking)}): save the manifest in the app, reinstall it to the "
            "workspace, and hand over the User OAuth Token shown after that")


def _check_user_token(api: SlackApi, token: str, team: Workspace,
                      access: Access = Access.READ) -> str:
    """The member the user token acts as, who is the owner; refused in a sentence."""
    checked = api.auth_test(token)
    if problem := user_token_problem(checked, team) or scope_problem(checked, access):
        raise SlackSetupError(f"the user token cannot be used: {problem}. Nothing was saved")
    return checked.user_id


def _connected_line(workspace: str, team: Workspace, access: Access, kinds: tuple) -> str:
    where = f"connected to the {team.team or 'Slack'} workspace"
    if workspace != DEFAULT:
        return f"{where} as {workspace}, at the {access} level; its tokens are saved"
    saved = [TOKEN_KEYS[kind] for kind in kinds]
    names = saved[0] if len(saved) == 1 else ", ".join(saved[:-1]) + " and " + saved[-1]
    level = "" if access is Access.ANNOUNCE else f" at the {access} level"
    return f"{where}{level}; {names} saved"


def _nowhere(on_file: str, server: str) -> str:
    """Why there is no destination for a token, when no address was given to look for."""
    if on_file:
        return (f"the channel on file ({on_file}) is a direct message with the app the old "
                "token came from, and this token cannot post to it")
    if server:
        return (f"a token of its own needs somewhere of its own to send, and the channel so "
                f"far is the {server} MCP server's")
    return "nowhere to send yet"
