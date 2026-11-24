"""Connecting Slack: what the wizard's Slack step and `keryx auth login slack` share.

Each owner makes a Slack app of their own, from the manifest in `integrations.slack`, and
pastes its tokens once. **The access level comes first** (`integrations.slack.Access`),
because it decides the manifest: what each level lets Keryx see is said in plain words
(`MEANING`) before any link is shown. Everything after the tokens Keryx does: `auth.test`
says whose each is, `apps.connections.open` shows the app-level one is taken, the member
the user token belongs to is where reports go (stored as `channel_id`, so nobody copies
a channel id out of Slack), and one test message shows it arriving.

The wizard asks its questions in `setup/plugins.py::run_slack`, through `Probes`. This
module holds the wording both use and the command-line path, which is one command at a
terminal (`login`: the steps, then each token the level still needs asked for there,
hidden, and checked) and two without one (`start` prints the steps and ends; `connect`
takes the tokens on standard input, one to a line and told apart by how each starts,
which is how a coding agent or a pipe hands them over). A token is never an argument,
never echoed and never in an error; what Slack refused is its error code and nothing
else it said.

Four rules both paths keep. **A token brings its destination with it** (`keeps`): a direct
message's id (`D…`) belongs to the app that opened it, so a token that is not the one on
file is never paired with one — not the one in the plugin's file, and not a named MCP
server's — and reports go to the owner's own member id instead. A member's id or a
channel's is the workspace's and is kept. **Nothing is called connected that was not shown
to arrive**: either the test message went, or the last line says none was sent and why.
**A level is all of its tokens or none of it**: every token the level needs is checked
before anything is saved, one the level does not use is refused by name, and lowering the
level forgets the tokens it no longer uses. **An app is never made twice**: raising the
level of a workspace that is connected prints the new manifest to paste into the app there
is.

**What sets Keryx to work is the owner writing to it.** In the app's own direct messages
that is any message of theirs; anywhere else, one that mentions the app. The app's own
member id (`bot_user_id`, what `auth.test` says of the bot token) is stored at connect,
because a mention is a mention of it and because nothing is ever sent to it as the
owner; a workspace above `announce` without it hears nothing.

**One owner, fixed at connect, and Slack says who** (the owner's rulings of 2026-11-23
and 2026-11-24, "let's always require a user token"). A bot token names the bot, not
whoever installed it, so connecting takes the owner's user token at every level, and
`owner_id` (the one member whose messages may start work with no PIN) is the member
`auth.test` says that token belongs to, in the bot token's own workspace. Nothing else
establishes one: not an address looked up, not where reports go. It is recorded at
`announce` too, so raising the level changes nothing about who the owner is. After that
nothing changes it in place: a user token that is somebody else's is refused, a bot
token from another workspace is refused, and there is no option for it. `keryx auth
logout slack` and a new login is the one way to connect as somebody else. `OWNER_LINE`
names the member on every connect at `converse` or above. **Below `read` the user token
is used for that one `auth.test` and nothing after**: the clients that read and send as
the owner are gated on the level (`SlackWorkspace.reads`, `.writes`), not on a token
being on file.

A second workspace is the same flow with `--workspace NAME`: its own app from the same
manifest, its own level, tokens, member id and destination (`plugins.slack`), and
`keryx auth logout slack [--workspace NAME]` (`logout`) forgets one.

**The app carries the assistant's name** (`app_of`: `ASSISTANT_NAME`, fitted by
`integrations.slack.app_name`), so everything here that tells the owner what to type in
Slack is written with `{app}` and filled with it: the level descriptions, the steps, the
owner line, the test message. Keryx stays the name of the service that posts, reads and
listens. Slack learns a name only from a manifest, so an app keeps the one it was made
with until its manifest is saved again, and a workspace that is connected is always
shown that manifest (`_already`).

**An app is made from the manifest of its day, and a later manifest may ask for more.**
Sending a file takes the bot scope `files:write`, which apps made before it lack. Slack
says what a token may do with every answer, so a login asks (`lacking_bot_scopes`): a
workspace that is whole at its level and whose app lacks a scope is shown the steps that
change the app there is, with the manifest, exactly as raising a level shows them. A
missing scope never refuses a login or stops a report: what the app could do it still
does, and `connect` says in one line what it cannot do yet.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
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
#: words: said before the link is shown. `{app}` is what the app is called (`meaning`).
MEANING = {
    Access.ANNOUNCE: "Keryx posts to you on Slack and reads nothing there.",
    Access.CONVERSE: (
        "Keryx posts to you, and is sent the direct messages you type to the {app} app "
        "itself: nothing else in Slack. Every message you type there is a request, with "
        "no @{app} needed: a new message starts work, and a reply under a task's report "
        "resumes that task, both without the phone PIN. Only your own member id is "
        "checked."
    ),
    Access.READ: (
        "Everything in converse. You can also mention the app (@{app}) in any conversation "
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
    Access.CONVERSE: "also acts on every message you send the {app} app itself: each "
                     "can resume or start work without the PIN",
    Access.READ: "also answers your mention in a conversation it was added to, there, "
                 "and reads Slack as you when you ask it about messages",
    Access.WRITE: "also sends a message as you, to a person or a channel, when you ask "
                  "for that message",
}
#: Said at `read`: when Slack is read as the owner, and by what.
READ_NOTE = (
    "Keryx reads Slack as you only when you ask about messages: on a call with the "
    "`slack_read` voice tool, and in a task through `keryx slack`, which a coding agent "
    "runs when your request is about Slack."
)

#: Said at `write`: what sending as the owner is, and by what.
WRITE_NOTE = (
    "A message Keryx sends at this level is yours: other people read it as written by "
    "you. On a call that is the `slack_message` voice tool, which always asks before it "
    "sends; in a task it is `keryx slack message`, which a coding agent runs only when "
    "your request asks for that message to that person or channel."
)

#: The voice tools a level unlocks, lowest level first. A login that takes a workspace
#: to a level it was not at turns them on (`connect`), because a level that reads with
#: nothing on a call to read with is what the owner met on 2026-11-24: the question was
#: dispatched, and answered from somewhere else. Lowering, or disconnecting, turns off
#: the ones no workspace has a level for any more (`_retire`). One turned off by hand
#: stays off until a level is reached again.
VOICE_TOOLS = ((Access.READ, "slack_read"), (Access.WRITE, "slack_message"))
#: Said before the link, of the voice tools this login would turn on.
WILL_TURN_ON = (
    "Connecting at this level also turns on {tools} for calls, with nothing more to "
    "run; `keryx plugins remove NAME` turns one off."
)
#: Said once each, when it has.
TURNED_ON = {
    "slack_read": (
        "slack_read is on too, from the next call: a question about your Slack is "
        "answered on the call (`keryx plugins remove slack_read` turns that off)"
    ),
    "slack_message": (
        "slack_message is on too, from the next call: a message is sent as you on a "
        "call, after it has said who to and the words and you have said yes (`keryx "
        "plugins remove slack_message` turns that off)"
    ),
}
#: Said of one that is off in a workspace already at its level, where a login changes
#: nothing: what that leaves undone, and the one command that turns it on.
STILL_OFF = {
    "slack_read": "slack_read is off, so nothing reads Slack on a call: `keryx plugins "
                  "install slack_read` turns it on.",
    "slack_message": "slack_message is off, so nothing sends as you on a call: `keryx "
                     "plugins install slack_message` turns it on.",
}
#: Said when one is turned off because no workspace has the level for it any more.
TURNED_OFF = "{name} is off with it; {again} again turns it back on."

#: Places in Slack's app settings, each written as the console lays it out: the page in
#: the sidebar, then the section on it, then what is clicked there. The steps and the
#: prompts name a place only by one of these, so the two cannot drift apart.
APP_TOKENS_PLACE = "Basic Information > App-Level Tokens > Generate Token and Scopes"
OAUTH_TOKENS_PLACE = "OAuth & Permissions > OAuth Tokens"
ICON_PLACE = "Basic Information > Display Information"
#: What each of the two tokens in that section is called there, in the order it lists
#: them: the user's first, the bot's below it.
OAUTH_TOKEN_LABELS = {USER: "User OAuth Token", BOT: "Bot User OAuth Token"}

#: The steps in Slack, after the link: one visit to each page, in the order someone meets
#: the pages. Markdown; `plain` is the same without the marks.
CREATE_STEP = (
    "Open the link: it is Slack's page for a new app, with yours already filled in and "
    "named {app}, after your assistant. Pick your workspace and click through to "
    "**Create**."
)
#: From `converse`. The token needs no install, and its page is the one the new app
#: opens on, so it comes before the install.
APP_TOKEN_STEP = (
    f"**{APP_TOKENS_PLACE}**: name the token anything, add the `connections:write` scope, "
    "generate it, and copy the token (`xapp-…`)."
)
#: Said without one button's label: Slack's own pages put the install button at the top
#: of OAuth & Permissions (its quickstart) and on Install App (its distribution guide),
#: and the label carries the workspace's name. Both places are named.
INSTALL_STEP = (
    "Install the app to your workspace and click **Allow**: the install button is at the "
    "top of **OAuth & Permissions**, and on **Install App** too."
)
#: The steps that change the app there is, in place of the first above: to raise its
#: level, or to give an app made before user tokens the one it now needs.
RAISE_STEPS = (
    f"Open {APPS_URL} and pick the app you already have for Keryx. Do not create a "
    "second one.",
    "**App Manifest**: replace what is there with the manifest below, and click **Save "
    "Changes**.",
)
REINSTALL_STEP = (
    "**Install App**: click **Reinstall to Workspace**, then **Allow**. The new "
    "permissions count only after that."
)


def oauth_tokens_step(wanted: tuple[str, ...]) -> str | None:
    """The one visit to the page that lists the user token and the bot token: which of
    the two to copy, in the order the page lists them. None when neither is wanted."""
    copied = [f"the **{OAUTH_TOKEN_LABELS[kind]}** (`{TOKEN_PREFIXES[kind]}…`)"
              for kind in (USER, BOT) if kind in wanted]
    if not copied:
        return None
    if len(copied) == 2:
        return f"**{OAUTH_TOKENS_PLACE}**: copy {copied[0]}, then {copied[1]} below it."
    which = "first" if USER in wanted else "second"
    return f"**{OAUTH_TOKENS_PLACE}**: copy {copied[0]}, the {which} of the two there."


#: The Keryx mark as a square PNG, shipped beside this module, for the app's icon. A
#: manifest cannot carry an icon, so uploading it is a step of the owner's, and optional.
ICON = Path(__file__).resolve().with_name("slack_app_icon.png")
#: The last step, for a new app and for one being changed: Markdown, with the file on a
#: line of its own (`shown` makes the terminal's two lines of it).
ICON_STEP = (
    f"Optional: **{ICON_PLACE}**: upload the Keryx mark from the file below as the app's "
    "icon. Skipping it changes nothing else.\n\n   `{icon}`"
)

#: What the app posts to show it arrives. The app is the assistant in Slack, so it does
#: not call itself Keryx: it names nothing but what the owner would type (`@{app}`).
#: Where each token is found, for its prompt: the same place its step names, and for the
#: two that share a section, what the token is called there.
TOKEN_PLACES = {
    APP: "Basic Information > App-Level Tokens",
    USER: f"{OAUTH_TOKENS_PLACE} > {OAUTH_TOKEN_LABELS[USER]}",
    BOT: f"{OAUTH_TOKENS_PLACE} > {OAUTH_TOKEN_LABELS[BOT]}",
}
#: How many times one token is asked for again after an answer that could not be it, and
#: how many times Slack may refuse what was typed, before the login ends unsaved.
MAX_WRONG = 3
MAX_REFUSALS = 3

TEST_MESSAGE = (
    "Connected. What you ask for in writing on a call arrives here, and so does each "
    "finished task's report."
)
#: Added to it where a message typed back is acted on.
CONVERSE_TEST_MESSAGE = (
    " Anything you type here is a request, with no @{app} needed: a reply under a "
    "report resumes that task, and a new message starts one."
)
#: Said with every manifest for an app there already is: Slack learns a name only there.
RENAME_NOTE = (
    "Saving it also names the app {app}, after your assistant: Slack shows the name it "
    "was made with until then."
)

#: Said where the destination is printed: the one way reports go anywhere else.
ELSEWHERE = (
    f"To send reports somewhere else: `keryx plugins install {PLUGIN} --set "
    "channel_id=ID`, with a channel's id or a member's."
)
#: The same for a further workspace, whose settings no command line sets.
ELSEWHERE_NAMED = (
    "To send reports somewhere else: set channel_id under [workspace-{name}] in {file}."
)
#: Said below `converse`, where nobody's messages are acted on yet.
OWNER_NOTED = (
    "{who} is taken to be you there, by your user token. Nothing you type is acted on at "
    "this level, and raising it later keeps you as the owner."
)

#: Said when a token replaces another: `plugins.secret` prefers the settings a process
#: started with, so a running service goes on using the old one.
RESTART_NOTE = "A Keryx that started with the old token keeps it until `keryx restart`."
#: Said when a workspace starts being listened to, or its listening changes.
LISTEN_NOTE = (
    "Keryx starts listening for your messages there at its next start: `keryx restart`. "
    "From then on every message you type to the {app} app is a request."
)
#: Said at `converse` and above, every time: whose messages may start work with no PIN.
OWNER_LINE = (
    "Only messages from {who} are acted on there: every one typed to the {app} app "
    "itself, and elsewhere only one that mentions it (@{app}). That member id is taken "
    "to be you, for as long as this workspace is connected."
)
#: Said when the destination is a channel: only the app's direct messages are heard.
CHANNEL_NOTE = (
    "Reports go to {channel}, which is not your direct messages with the app: a reply "
    "typed there is not seen. Messages you send the app directly are."
)

_MEMBER = re.compile(r"[UW][A-Z0-9]{2,31}")


class SlackSetupError(RuntimeError):
    """Connecting Slack could not go on, in a sentence that never holds a token."""


class TokenRefused(SlackSetupError):
    """One token is not one this workspace can be connected with: Slack said no to it,
    or it is somebody else's or another workspace's. `kind` is which token and `why` the
    reason alone, so a terminal can say so and ask for that one again. Never raised for
    Slack not answering, which asking again would not mend."""

    def __init__(self, kind: str, why: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.why = why


class Stopped(SlackSetupError):
    """The questions at the terminal ended before every token was in: the owner left, or
    an answer was wrong too many times. Nothing was saved."""


@dataclass(frozen=True)
class SlackApi:
    """The calls `connect` makes, replaced in the tests."""

    auth_test: Callable[[str], Workspace] = slack.auth_test
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


def app_of(settings: Settings) -> str:
    """What the owner's Slack app is called: their assistant's name, fitted for Slack."""
    return slack.app_name(settings.assistant_name)


def meaning(access: Access, app: str) -> str:
    """What `access` lets Keryx see, said of an app called `app`."""
    return MEANING[access].format(app=app)


def short(access: Access, app: str) -> str:
    """The same in a few words, beside the wizard's option."""
    return SHORT[access].format(app=app)


def steps(access: Access, *, app: str, raising: bool = False,
          have: tuple[str, ...] = ()) -> list[str]:
    """What to do in Slack for `access`, as Markdown lines without their numbers.

    `app` is what the app is called. `raising` is the app there is, changed in place;
    `have` is the tokens already on file, which that leaves as they are and which are not
    asked for again.

    **Grouped by page, in the order the pages are met, and each page visited once** (the
    owner's report from the real console): the app-level token before the install, then
    the install, then the one section that lists the user token and the bot token, in
    the order it lists them. `token_kinds` is the same order, so the prompts that follow
    ask for the tokens as the steps had them copied.
    """
    wanted = tuple(kind for kind in token_kinds(access) if kind not in have)
    lines = list(RAISE_STEPS) if raising else [CREATE_STEP]
    if APP in wanted:
        lines.append(APP_TOKEN_STEP)
    lines.append(REINSTALL_STEP if raising else INSTALL_STEP)
    if tokens := oauth_tokens_step(wanted):
        lines.append(tokens)
    lines.append(ICON_STEP)
    return [line.format(app=app, icon=ICON) for line in lines]


def guide(access: Access = Access.ANNOUNCE, *, app: str, raising: bool = False,
          have: tuple[str, ...] = ()) -> str:
    """The steps, as Markdown for the wizard, which shows the link above them."""
    return "\n".join(
        f"{number}. {step}"
        for number, step in enumerate(steps(access, app=app, raising=raising, have=have), 1)
    )


def plain(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def shown(step: str) -> str:
    """A step as the terminal prints it: no Markdown marks, and what it puts on a line of
    its own (the icon's file) straight under it."""
    return plain(step).replace("\n\n", "\n")


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


def _flags(name: str, access: Access, current: Access) -> str:
    """The options that name this workspace and level again on the next command."""
    flags = "" if name == DEFAULT else f" --workspace {name}"
    return flags + ("" if access is current else f" --access {access}")


def lacking_bot_scopes(checked: Workspace, access: Access) -> tuple[str, ...]:
    """What the app's bot may not do yet that the manifest for `access` asks for, from
    what Slack said of its bot token: an app made from an earlier manifest. Empty when
    it lacks nothing, and when Slack did not answer or did not say what the token may
    do, which is never taken to mean "nothing"."""
    if not checked.ok or checked.scopes is None:
        return ()
    return tuple(scope for scope in slack.bot_scopes(access) if scope not in checked.scopes)


def _for_files(lacks: tuple[str, ...]) -> bool:
    return any(scope in lacks for scope in slack.FILE_BOT_SCOPES)


def lacks_note(lacks: tuple[str, ...], workspace: str) -> str:
    """One line for a connected app that lacks a scope: what that costs, and the fix."""
    named = "" if workspace == DEFAULT else f" --workspace {workspace}"
    cost = ("so it cannot send a file yet; everything else works as it did"
            if set(lacks) <= set(slack.FILE_BOT_SCOPES)
            else "so not everything at this level works yet")
    return (f"note: the app in Slack lacks {', '.join(lacks)}, {cost}. To add it: `keryx "
            f"auth login slack{named}` prints the manifest to paste into the app, which "
            "is then reinstalled")


def start(
    settings: Settings,
    echo: Callable[[str], None],
    *,
    workspace: str = DEFAULT,
    access: Access | None = None,
    waiting: bool = False,
    api: "SlackApi | None" = None,
) -> Access | None:
    """`keryx auth login slack` with no token: what the level means, then the link (or
    the manifest, for an app there already is), the steps, and what comes next. A
    workspace that is connected at that level already, with every token a login takes,
    is told so, and never shown the link that would make a second app. One connected
    before user tokens were asked for is shown how to update the app it has, and so is
    one whose app lacks a scope its level's manifest now has (`api`, which is how Slack
    is asked what the bot token may do: once, and only for a workspace that is otherwise
    whole; with none given, nothing is asked).

    What comes next depends on who is there. With nobody at a terminal it is the next
    command, `--stdin`, which is the one place that sentence is printed. `waiting` is a
    terminal, where this same command goes on to ask for the tokens: the steps end by
    sending the owner back to it. Returns the level whose tokens are wanted, or None when
    there is nothing to hand over because the workspace is whole.
    """
    try:
        values = _config(settings)
        saved = on_file(settings, workspace, values)
        first_missing = plugins.plugin(PLUGIN).problem(dict(values)) is not None
    except SlackSetupError:
        saved, first_missing = SlackWorkspace(workspace), True
    connected = bool(saved.token(BOT))
    app = app_of(settings)
    if workspace != DEFAULT and first_missing:
        echo(f"{workspace} would be added beside your first workspace, and there is none "
             "yet: run this without --workspace first.\n")
    current = saved.access
    chosen = access is not None or connected
    access = access or current
    lacking = [kind for kind in token_kinds(access) if not saved.token(kind)]
    lacks: tuple[str, ...] = ()
    if connected and access is current and lacking != [USER]:
        if api is not None and not lacking:
            lacks = lacking_bot_scopes(api.auth_test(saved.token(BOT) or ""), access)
        if not lacks:
            # Whole, or without something its level runs on, which `_already` says.
            _already(saved, echo, app=app, assistant=settings.assistant_name)
            for name in _unlocked(Access.ANNOUNCE, current):
                if not plugins.tool_path(settings, name).is_file():
                    # Reached before a login turned it on, or turned off by hand: this
                    # login changes nothing, so it names the command that does.
                    echo("\n" + STILL_OFF[name])
            return None
    # The app there is, is changed and never made again: to raise its level, or (at the
    # level it has) to give it the user token an app made before them never issued.
    raising = connected and access.reaches(current)
    have = tuple(kind for kind in token_kinds(access) if saved.token(kind)) if raising else ()
    if chosen:
        echo(f"Access level {access}: {meaning(access, app)}")
    else:
        # Nothing is connected and no level was named: say all three before the link,
        # because the level decides the app that link creates.
        echo("What Keryx may see in your Slack is decided now, because it decides the app:\n")
        for level in slack.LEVELS:
            echo(f"  {level}: {meaning(level, app)}")
        others = " or ".join(f"--access {level}" for level in slack.LEVELS if level is not access)
        echo(f"\nWhat follows is {access}; {others} chooses another.")
    if access.reaches(Access.READ):
        echo(READ_NOTE)
    if access.reaches(Access.WRITE):
        echo(WRITE_NOTE)
    if unlocked := _unlocked(current, access):
        echo(WILL_TURN_ON.format(tools=" and ".join(unlocked)))
    echo("")
    if lacks:
        what = (", which sending a file takes. Nothing about it stops working: reports "
                "arrive as they did, and a file cannot be sent until the app has it"
                if _for_files(lacks) else
                ". What it could do it still does")
        echo(f"{saved.label} is connected at the {current} level, with an app made before "
             f"Keryx asked for {', '.join(lacks)}{what}. The app you have is updated, not "
             "made again:\n")
    elif raising and access is current:
        echo(f"{saved.label} is connected at the {current} level, from before Keryx asked "
             "for a user token. Nothing about it stops working. The next login takes "
             "one, because it is how Slack says which member you are, so the app you "
             "have is updated, not made again:\n")
    elif raising:
        echo(f"{saved.label} is connected at the {current} level, so the app you have is "
             "changed, not made again:\n")
    else:
        echo("Create your Slack app from this link:\n")
        echo(slack.new_app_url(access, assistant=settings.assistant_name))
        echo("")
    for number, step in enumerate(steps(access, app=app, raising=raising, have=have), 1):
        echo(f"{number}. {shown(step)}")
    if raising:
        echo("\nThe manifest:\n")
        echo(slack.manifest_json(access, assistant=settings.assistant_name))
        echo("\n" + RENAME_NOTE.format(app=app))
        if BOT in have:
            echo("The bot token stays as it is.")
    wanted = [kind for kind in token_kinds(access) if kind not in have]
    many = len(wanted) > 1
    what = "the tokens" if many else f"the {TOKEN_NAMES[wanted[0]]}" if wanted else ""
    # Where reports will go: where they go now, while the bot token stays; else to them.
    where = (f"reports keep going to {saved.channel}" if BOT in have and saved.channel
             else "reports go to you")
    said = f"The user token says which member you are: {where}, and nothing else is asked."
    if waiting:
        # This command is still running, so the last thing said is to come back to it.
        echo("\n" + said)
        if wanted:
            echo(f"Then come back to this terminal, which is waiting, and paste {what} "
                 f"where {'each is' if many else 'it is'} asked for. What you paste is "
                 "not shown.")
        else:
            echo("Every token this level needs is saved already. Once the app is "
                 "reinstalled, come back to this terminal, which is waiting, and press "
                 "Enter at each token to keep it"
                 + (", or paste the User OAuth Token if Slack shows a different one "
                    "after the reinstall." if USER in have else "."))
        return access
    command = f"keryx auth login slack --stdin{_flags(workspace, access, current)}"
    if wanted:
        echo(
            f"\nThen hand {what} over on standard input, never on the command line"
            + (", one to a line in any order" if many else "")
            + f":\n  {command}"
        )
    else:
        echo("\nEvery token this level needs is saved already, so there is nothing to "
             f"hand over. Once the app is reinstalled:\n  {command} < /dev/null")
        if USER in have:
            echo("If Slack shows a different User OAuth Token after the reinstall, hand "
                 "that one over instead of nothing.")
    echo(said)
    return access


def _unlocked(was: Access, now: Access) -> tuple[str, ...]:
    """The voice tools that `now` has a level for and `was` did not."""
    return tuple(name for level, name in VOICE_TOOLS
                 if now.reaches(level) and not was.reaches(level))


def _retire(settings: Settings, echo: Callable[[str], None], *, again: str) -> None:
    """Turn off each voice tool that no workspace has the level for any more: left on,
    it would be refused at every call and reported by `keryx tools` until somebody
    removed it. Its settings are set aside, as `keryx plugins remove` leaves them."""
    for name, ready in (("slack_read", plugin.can_read), ("slack_message", plugin.can_write)):
        if plugins.tool_path(settings, name).is_file() and not ready(settings):
            plugins.remove(settings, name)
            echo(TURNED_OFF.format(name=name, again=again))


def _already(saved: SlackWorkspace, echo: Callable[[str], None], *, app: str,
             assistant: str) -> None:
    """What a workspace that is connected at the level asked for is told: how it stands,
    the three things that change it, and its manifest, which is the one way the app in
    Slack is renamed after the assistant. Never the link that makes a second app."""
    named = "" if saved.name == DEFAULT else f" --workspace {saved.name}"
    echo(f"{saved.label} is connected at the {saved.access} level: "
         f"{meaning(saved.access, app)}")
    if saved.missing:
        echo(f"It is without {' or '.join(saved.missing)}.")
    if saved.access.reaches(Access.CONVERSE) and saved.owner_id:
        echo(OWNER_LINE.format(who=saved.owner_id, app=app))
        echo(f"To connect as somebody else: keryx auth logout slack{named}, then log in "
             "again.")
    others = [level for level in slack.LEVELS if level is not saved.access]
    echo(
        f"\nTo change what Keryx may see there: keryx auth login slack{named} --access "
        f"{' or --access '.join(others)}"
        f"\nTo replace a token: keryx auth login slack --stdin{named}, which asks for "
        "each at a terminal (Enter keeps one) and otherwise reads standard input"
        f"\nTo disconnect it: keryx auth logout slack{named}"
    )
    # Slack is never asked what the app is called, so the manifest is always shown: it is
    # the same app with nothing else changed, and saving it needs no reinstall.
    echo(f"\nThe app is called {app} in Slack, after your assistant, once its manifest "
         "says so: one made under another name keeps it until then. To rename it, open "
         f"{APPS_URL}, pick the app, open App Manifest, replace what is there with this, "
         "and click Save Changes:\n")
    echo(slack.manifest_json(saved.access, assistant=assistant))
    echo("\n" + shown(ICON_STEP.format(icon=ICON)))


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
    # What the lower level works with; and the user token, when all it could ever do was
    # say who they are. One that could read or send as them is forgotten below `read`:
    # it keeps those permissions at Slack until the app is reinstalled, and Keryx has no
    # use for it meanwhile. The next login asks for the one the reinstalled app issues.
    keeping = set(slack.used_kinds(access))
    if not saved.access.reaches(Access.READ):
        keeping.add(USER)
    kept = {kind: saved.tokens[kind] for kind in saved.tokens if kind in keeping}
    dropped = [kind for kind in saved.tokens if kind not in kept]
    _save_tokens(store, settings, workspace, kept)
    _write_workspace(settings, values, workspace, {"access": str(access)})
    app = app_of(settings)
    echo(f"{saved.label} is now at the {access} level: {meaning(access, app)}")
    if dropped:
        names = " and ".join(_stored_as(workspace, kind) for kind in dropped)
        echo(f"{names} {'are' if len(dropped) > 1 else 'is'} forgotten.")
    # Said of what really stops: at converse Keryx goes on acting on their messages, and
    # at read it goes on reading as them, with the user token it still holds.
    if access is Access.ANNOUNCE:
        stops = "A running Keryx stops acting on your messages there at once."
    elif access.reaches(Access.READ):
        stops = ("Keryx no longer sends messages as you, from this moment; it still reads "
                 "Slack as you when you ask, with the user token it keeps, and still acts "
                 "on the messages you type to the app.")
    else:
        stops = ("Keryx no longer holds your user token, so nothing reads Slack as you; it "
                 "still acts on the messages you type to the app.")
    _retire(settings, echo, again="raising the level")
    echo(f"{stops} The app in Slack keeps the permissions it was made with: to take those "
         "away too, paste this manifest into its App Manifest page and reinstall it:\n")
    echo(slack.manifest_json(access, assistant=settings.assistant_name))
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
    _retire(settings, echo, again="connecting at that level")
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


def prompt_for(kind: str, *, saved: bool) -> str:
    """What a terminal asks for one token: which it is, where Slack shows it, and that
    Enter keeps the one on file when there is one."""
    asked = f"{TOKEN_NAMES[kind].capitalize()} ({TOKEN_PREFIXES[kind]}…, {TOKEN_PLACES[kind]})"
    return asked + (", or Enter to keep the one saved" if saved else "")


def wrong_token(value: str, kind: str) -> str | None:
    """Why what was typed cannot be the token asked for, in a sentence that does not
    repeat it; None when it could be. Told by how it starts, before Slack is asked."""
    typed = slack.token_kind(value)
    if typed == kind or (kind == BOT and typed is None and value.startswith("xox")):
        return None
    name, prefix = TOKEN_NAMES[kind], TOKEN_PREFIXES[kind]
    if typed is not None:
        return (f"That is {_a(TOKEN_NAMES[typed])}, which starts {TOKEN_PREFIXES[typed]}: "
                f"the {name} starts {prefix}.")
    return f"That is not a Slack token: the {name} starts {prefix}."


class _Left(Exception):
    """The owner left a question, or an answer could not be the token too many times."""


def ask_token(
    kind: str, *, saved: bool, ask: Callable[[str], str], echo: Callable[[str], None]
) -> str:
    """One token, asked for hidden until what is typed could be it.

    Empty keeps the one on file, when there is one. With none on file an empty answer is
    asked again once, and a second one leaves; so does Ctrl-C or the end of input, and
    so does an answer that is the wrong kind of token `MAX_WRONG` times. What was typed
    is never said back.
    """
    empties = wrongs = 0
    while True:
        try:
            answer = ask(prompt_for(kind, saved=saved)).strip()
        except (EOFError, KeyboardInterrupt):
            raise _Left from None
        if not answer:
            if saved:
                return ""
            empties += 1
            if empties > 1:
                raise _Left
            echo(f"{_a(TOKEN_NAMES[kind]).capitalize()} is needed at this level, and none "
                 "is saved: paste it, or press Enter again to stop.")
            continue
        problem = wrong_token(answer, kind)
        if problem is None:
            return answer
        wrongs += 1
        echo(problem)
        if wrongs >= MAX_WRONG:
            raise _Left


def login(
    settings: Settings,
    store: ConfigStore,
    *,
    ask: Callable[[str], str],
    echo: Callable[[str], None],
    test: bool = True,
    steps: bool = True,
    api: SlackApi | None = None,
    workspace: str = DEFAULT,
    access: Access | None = None,
) -> None:
    """`keryx auth login slack` at a terminal: one command, which waits.

    It says what the level means and prints the link and the steps (`start`; not with
    `steps` off, which is `--stdin` typed at a terminal), then stays and asks for each
    token the level takes, hidden, through `ask`: one on file is kept with Enter, one
    that is not that token is said so and asked again. Then everything `connect` does,
    with all the tokens at once, so nothing is saved until every one is taken. A token
    Slack refuses (`TokenRefused`) is named and asked for again, `MAX_REFUSALS` times in
    all. Leaving at any question raises `Stopped`: nothing was saved, and the sentence
    says the same command picks it up. A workspace that is whole is told how it stands
    and asked nothing, unless Slack says its app lacks a scope the level's manifest has
    (`lacking_bot_scopes`): then the steps are the ones that update the app there is,
    and Enter at each token is all that is asked.
    """
    again = "keryx auth login slack" + ("" if steps else " --stdin") + (
        "" if workspace == DEFAULT else f" --workspace {workspace}") + (
        "" if access is None else f" --access {access}")
    stopped = Stopped(f"nothing was saved: run `{again}` again to pick this up")
    values = _config(settings)
    if workspace != DEFAULT and (problem := plugins.plugin(PLUGIN).problem(dict(values))):
        raise SlackSetupError(
            f"{workspace} is added beside your first workspace, and there is none yet "
            f"({problem}): run it without --workspace first. Nothing was saved"
        )
    saved = on_file(settings, workspace, values)
    level = access or saved.access
    api = api or SlackApi()
    if steps:
        wanted = start(settings, echo, workspace=workspace, access=access, waiting=True,
                       api=api)
        if wanted is None:
            return
        level = wanted
        echo("")
    given: dict[str, str] = {}
    try:
        for kind in token_kinds(level):
            if answer := ask_token(kind, saved=bool(saved.token(kind)), ask=ask, echo=echo):
                given[kind] = answer
        for refusals in range(1, MAX_REFUSALS + 1):
            try:
                connect(settings, store, tokens=given, test=test, echo=echo, api=api,
                        workspace=workspace, access=level)
                return
            except TokenRefused as refused:
                echo(f"The {TOKEN_NAMES[refused.kind]} was not taken: {refused.why}. "
                     "Nothing was saved.")
                if refusals == MAX_REFUSALS:
                    break
                # The one on file is offered again only when it is not what was refused.
                on_hand = bool(saved.token(refused.kind)) and refused.kind in given
                given.pop(refused.kind, None)
                if answer := ask_token(refused.kind, saved=on_hand, ask=ask, echo=echo):
                    given[refused.kind] = answer
    except _Left:
        pass
    raise stopped


def read_tokens(text: str) -> dict[str, str]:
    """The tokens on standard input, by kind: one to a line, in any order.

    Raises `SlackSetupError` for a line that is none of the three, or a kind given twice.
    A bot token is told by `xox` alone when it is the only line, as it always was; and
    among several, a line that starts `xox` and holds `xoxb-` further in is one too: a
    rotating bot token is written `xoxe.xoxb-…`, and it no longer arrives alone, since
    the user token comes with it.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found: dict[str, str] = {}
    for line in lines:
        kind = slack.token_kind(line)
        if kind is None:
            if line.startswith("xox") and (len(lines) == 1 or "xoxb-" in line):
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


def another_workspace(saved: SlackWorkspace, team: Workspace) -> str | None:
    """Why the bot token Slack answered `team` for may not replace the workspace on file;
    None when it may.

    Another workspace's token would bring another owner with it, and the owner is fixed
    for as long as a workspace is connected. The one check, for the command line and the
    wizard alike, so that neither can let through what the other refuses.
    """
    if not (saved.owner_id and saved.team_id) or saved.team_id == team.team_id:
        return None
    named = "" if saved.name == DEFAULT else f" --workspace {saved.name}"
    return (f"that bot token is for {team.team or 'another workspace'}, and {saved.label} "
            f"is connected here: `keryx auth logout slack{named}` first, then log in "
            "again. Nothing was saved")


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
    tokens: str | Mapping[str, str],
    test: bool,
    echo: Callable[[str], None],
    api: SlackApi | None = None,
    workspace: str = DEFAULT,
    access: Access | None = None,
) -> None:
    """Check the tokens, learn who the owner is, save it all, turn the plugin on (and
    the voice tools of a level it had not reached before, `VOICE_TOOLS`), and prove it.

    `tokens` is standard input: one token to a line, told apart by how each starts; or,
    from a terminal that asked for each by name, the tokens by kind. A token the level
    takes and that is on file already need not be given again. A token that is refused
    for what it is raises `TokenRefused`, which says which. Raises
    `SlackSetupError` saying how far it got. Nothing is saved until every token is one
    Slack takes, so a token Slack refuses leaves a working install exactly as it was.
    After the save, only the test message can fail, and that says so and undoes nothing.

    **The owner is whoever the user token belongs to**, at every level: `auth.test` on
    it has to name the bot token's own workspace, and its member is the owner. An owner
    on file stands, and a user token that is somebody else's is refused. **Reports go to
    the owner's own member id**, unless a destination is on file that still goes with the
    bot token (`keeps`), which stays: an install from before any of this keeps posting
    where it always did. Nothing is asked about where to send, and nothing is looked up.
    """
    api = api or SlackApi()
    given = read_tokens(tokens) if isinstance(tokens, str) else dict(tokens)
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

    team = api.auth_test(token)
    if team.refused:
        raise TokenRefused(BOT, team.problem, f"{team.problem}; nothing was saved")
    if not team.ok:
        raise SlackSetupError(
            f"the token could not be checked ({team.problem}); nothing was saved")
    if access.reaches(Access.CONVERSE) and not (team.team_id and team.user_id):
        raise SlackSetupError(
            "Slack did not say which workspace the bot token belongs to, or which member "
            "the app is there, and above announce every message is held to both. Nothing "
            "was saved"
        )
    if problem := another_workspace(saved, team):
        raise SlackSetupError(problem)
    standing = standing_owner(saved, team, same_app=token == before)
    # Lowered out of reading with nothing new handed over: the owner on file stands, and
    # the token that could read Slack as them is not kept below `read`, as `lower` does
    # not keep it. One handed over now is the reinstalled app's, and is kept.
    retired = (bool(standing) and USER not in given and saved.access.reaches(Access.READ)
               and not access.reaches(Access.READ))
    if retired:
        needed = tuple(kind for kind in needed if kind != USER)
        held.pop(USER)
    token_owner = standing if retired else _check_user_token(api, held[USER], team, access)
    if APP in needed:
        opened = api.open_connection(held[APP])
        if not opened.ok:
            why = (f"it could not be used ({opened.problem}): it needs the "
                   "connections:write scope, and Socket Mode on in the app")
            message = f"the app-level token {why.removeprefix('it ')}. Nothing was saved"
            raise (TokenRefused(APP, why, message) if opened.refused
                   else SlackSetupError(message))
    named = "" if workspace == DEFAULT else f" --workspace {workspace}"
    app = app_of(settings)
    acted_on = _owner(standing=standing, token_owner=token_owner, named=named)
    # Where reports go: where they went, while that still goes with this token; else to
    # the owner themselves.
    kept = saved.channel if keeps(saved.channel, new_token=token != before) else ""
    channel = kept or acted_on

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
    if kept:
        echo(f"it keeps sending to {channel}")
    else:
        echo(f"you are {channel} there: reports go to you")
    echo(ELSEWHERE if workspace == DEFAULT else ELSEWHERE_NAMED.format(
        name=workspace, file=plugins.config_path(settings, PLUGIN).name))
    echo((OWNER_LINE if access.reaches(Access.CONVERSE) else OWNER_NOTED).format(
        who=acted_on, app=app))
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
    # What the level now reached unlocks on a call, and the one before it did not:
    # turned on here, so that raising a level is one command and not two.
    for name in _unlocked(saved.access, access):
        try:
            plugins.install(settings, name)
        except plugins.PluginConfigError as error:
            echo(str(error))
        else:
            echo(TURNED_ON[name])
    if lacks := lacking_bot_scopes(team, access):
        echo(lacks_note(lacks, workspace))
    if server:
        echo(f"note: {PLUGIN} now posts with this token to {channel}, not with the "
             f"{server} MCP server's token and channel, and subagents are told to send "
             "with `keryx slack report`, no longer through that server")
    if access.reaches(Access.CONVERSE):
        echo(LISTEN_NOTE.format(app=app))
        if not (is_member(channel) or slack.is_dm(channel)):
            echo(CHANNEL_NOTE.format(channel=channel))
    if access.reaches(Access.READ):
        echo(READ_NOTE)
    if access.reaches(Access.WRITE):
        echo(WRITE_NOTE)
    for kind in saved.tokens:
        if kind not in needed:
            echo(f"{_stored_as(workspace, kind)} is forgotten: the {access} level does not "
                 "use it" + (", and until the app is reinstalled it could still read Slack "
                             "as you" if kind == USER else ""))

    if not test:
        echo("no test message was sent (--no-test), so nothing has shown it arrives")
        return
    sent = api.post_text(token, channel, test_message(access, app))
    if not sent.ok:
        raise SlackSetupError(
            f"everything is saved, but the test message to {channel} did not go through: "
            f"{sent.problem}"
        )
    echo("sent a test message: it is in Slack, under Apps")


def _owner(*, standing: str, token_owner: str, named: str) -> str:
    """The member whose messages are acted on: whom the user token belongs to.

    **Slack says who the owner is, and nothing else does.** Not an address looked up, not
    where reports go: the member `auth.test` names for the user token, in the bot token's
    own workspace. An owner on file stands, and a user token that is somebody else's is
    refused: nothing here replaces an owner, which is `keryx auth logout slack` and a new
    login. Recorded at every level, `announce` included, so raising the level later
    changes nothing about who the owner is.
    """
    if standing and token_owner != standing:
        why = (f"belongs to {token_owner}, and the member whose messages Keryx acts on "
               f"there is {standing}: paste your own user token. To connect as somebody "
               f"else, `keryx auth logout slack{named}` first")
        raise TokenRefused(USER, f"it {why}", f"the user token {why}. Nothing was saved")
    return token_owner


def test_message(access: Access, app: str) -> str:
    """The one message that shows it arriving, saying what a reply to it does. `app` is
    what the app is called, which is what the owner would type to mention it."""
    said = TEST_MESSAGE + (CONVERSE_TEST_MESSAGE if access.reaches(Access.CONVERSE) else "")
    return said.format(app=app)


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
    # Below `read` the token is asked who it is and nothing more, which takes no scope.
    wanted = slack.user_scopes(access) if access.reaches(Access.READ) else ()
    lacking = [scope for scope in wanted if scope not in checked.scopes]
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
        message = f"the user token cannot be used: {problem}. Nothing was saved"
        # The token's own fault when Slack answered for it (or refused it) and the bot
        # token's workspace is known; anything else is Slack not having been asked.
        its_own = (checked.ok or checked.refused) and bool(team.team_id)
        raise TokenRefused(USER, problem, message) if its_own else SlackSetupError(message)
    return checked.user_id


def _connected_line(workspace: str, team: Workspace, access: Access, kinds: tuple) -> str:
    where = f"connected to the {team.team or 'Slack'} workspace"
    if workspace != DEFAULT:
        return f"{where} as {workspace}, at the {access} level; its tokens are saved"
    # In the order the settings are listed, whatever order the tokens were asked for in.
    saved = [key for kind, key in TOKEN_KEYS.items() if kind in kinds]
    names = saved[0] if len(saved) == 1 else ", ".join(saved[:-1]) + " and " + saved[-1]
    level = "" if access is Access.ANNOUNCE else f" at the {access} level"
    return f"{where}{level}; {names} saved"
