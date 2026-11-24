"""Connecting Slack: what the wizard's Slack step and `keryx auth login slack` share.

Each owner makes a Slack app of their own, from the manifest in `integrations.slack`, and
pastes its tokens once. **What they tick comes first**, because it decides the manifest:
sending them reports and files is always on, and the three things they may add
(`integrations.slack.Permission`: `requests`, `read`, `send`) are one question with a
box each, in plain words (`ALWAYS`, `QUESTION`, `choices`), asked before any link is
shown. They are independent, and no word of the code is ever shown to somebody ticking.
Everything after the tokens Keryx does: `auth.test` says whose each is,
`apps.connections.open` shows the app-level one is taken, the member the user token
belongs to is where reports go (stored as `channel_id`, so nobody copies a channel id
out of Slack), and one test message shows it arriving.

The wizard asks its questions in `setup/plugins.py::run_slack`, through `Probes`. This
module holds the wording both use and the command-line path, which is one command at a
terminal (`login`: the boxes, the steps, then each token still needed asked for there,
hidden, and checked) and two without one (`start` prints the steps and ends; `connect`
takes the tokens on standard input, one to a line and told apart by how each starts,
which is how a coding agent or a pipe hands them over, with `--allow` saying what is
ticked). A token is never an argument, never echoed and never in an error; what Slack
refused is its error code and nothing else it said.

Five rules both paths keep. **A token brings its destination with it** (`keeps`): a direct
message's id (`D…`) belongs to the app that opened it, so a token that is not the one on
file is never paired with one — not the one in the plugin's file, and not a named MCP
server's — and reports go to the owner's own member id instead. A member's id or a
channel's is the workspace's and is kept. **Nothing is called connected that was not shown
to arrive**: either the test message went, or the last line says none was sent and why.
**What is ticked is all of its tokens or none of it**: every token it needs is checked
before anything is saved, and one it does not use is refused by name. **An app is never
made twice**: ticking more for a workspace that is connected prints the new manifest to
paste into the app there is. **Unticking needs nothing from Slack and counts at once**
(`untick`): the selection is saved, the tokens it no longer uses are forgotten, the voice
tool that went with it is turned off and a running service stops, whether or not the
app in Slack is ever changed; the manifest that takes the permissions away there too is
printed. It is the same on every path, and always first: at a terminal (`login`), with
the tokens piped in (`hand_over`), with nothing piped (`setup.auth`), and in the wizard,
where it is the one thing saved before the step's last answer.

**The voice tools follow the ticks** (`VOICE_TOOLS`, `sync_tools`): `slack_read` is on
with `read` and `slack_message` with `send`, turned on and off by the login or the
wizard step that changes what is ticked, with no second command and no second question.
One turned off by hand (`keryx plugins remove`) stays off until what is ticked next
changes.

**What sets Keryx to work is the owner writing to it.** In the app's own direct messages
that is any message of theirs; anywhere else, one that mentions the app. The app's own
member id (`bot_user_id`, what `auth.test` says of the bot token) is stored at connect,
because a mention is a mention of it and because nothing is ever sent to it as the
owner; a workspace with `requests` ticked and without it hears nothing.

**One owner, fixed at connect, and Slack says who** (the owner's rulings of 2026-11-23
and 2026-11-24, "let's always require a user token"). A bot token names the bot, not
whoever installed it, so a first connection takes the owner's user token whatever is
ticked, and `owner_id` (the one member whose messages may start work with no PIN) is the
member `auth.test` says that token belongs to, in the bot token's own workspace. Nothing
else establishes one: not an address looked up, not where reports go. It is recorded with
nothing ticked too, so ticking more later changes nothing about who the owner is. After
that nothing changes it in place: a user token that is somebody else's is refused, a bot
token from another workspace is refused, and there is no option for it. `keryx auth
logout slack` and a new login is the one way to connect as somebody else. `OWNER_LINE`
names the member on every connect that has `requests`.

**A user token is on file only while `read` or `send` is ticked.** With neither it is
asked for only when the workspace has no owner on file (the first connection), shown to
Slack once to learn which member the owner is, and not saved (`USED_ONCE`). A workspace
with an owner on file and neither box ticked is whole without one: nothing asks for one
(`token_kinds(…, owner_known=True)`), and a login that changes nothing ends with
"connected, and nothing was changed". One handed over anyway is still held to the owner
on file, and still not saved. One saved for that alone by an earlier build is dropped
the next time a sign-in writes, in one line. The clients that read and send as the owner
are gated on what is ticked besides (`SlackWorkspace.reads`, `.writes`). **A token that
may do more at Slack than is ticked is taken** (`beyond_ticked`), because Keryx cannot
shrink a token: one line says the app still grants more, and how the manifest trims it.

A second workspace is the same flow with `--workspace NAME`: its own app from the same
manifest, its own ticks, tokens, member id and destination (`plugins.slack`), and
`keryx auth logout slack [--workspace NAME]` (`logout`) forgets one.

**The app carries the assistant's name** (`app_of`: `ASSISTANT_NAME`, fitted by
`integrations.slack.app_name`), so everything here that tells the owner what to type in
Slack is written with `{app}` and filled with it: the boxes, the steps, the owner line,
the test message. Keryx stays the name of the service that posts, reads and listens.
Slack learns a name only from a manifest, so an app keeps the one it was made with until
its manifest is saved again, and a workspace that is connected is always shown that
manifest (`_already`).

**An app is made from the manifest of its day, and a later manifest may ask for more.**
Sending a file takes the bot scope `files:write`, which apps made before it lack. Slack
says what a token may do with every answer, so a login asks (`lacking_bot_scopes`): a
workspace that is whole and whose app lacks a scope is shown the steps that change the
app there is, with the manifest, exactly as ticking more shows them. A missing scope
never refuses a login or stops a report: what the app could do it still does, and
`connect` says in one line what it cannot do yet.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
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
    BASE,
    BOT,
    PERMISSIONS,
    TOKEN_PREFIXES,
    USER,
    Allowed,
    Connection,
    Permission,
    SlackResult,
    Workspace,
    as_owner,
    token_kinds,
)
from keryx.plugins import slack as plugin
from keryx.plugins.slack import DEFAULT, IN_WORDS, SlackWorkspace, in_words
from keryx.setup.ui import Choice

PLUGIN = "slack_report"
TOKEN_KEY = "SLACK_BOT_TOKEN"
#: Where the first workspace's tokens are stored, by kind.
TOKEN_KEYS = {BOT: TOKEN_KEY, APP: "SLACK_APP_TOKEN", USER: "SLACK_USER_TOKEN"}
#: And every further workspace's, in one table.
WORKSPACE_TOKENS_KEY = "SLACK_WORKSPACE_TOKENS"
#: What each token is called when it is asked for or refused.
TOKEN_NAMES = {BOT: "bot token", APP: "app-level token", USER: "user token"}
APPS_URL = "https://api.slack.com/apps"

#: What the app does whatever is ticked, said above the boxes. `{app}` is what the app
#: is called.
ALWAYS = (
    "Always on: Keryx sends you reports and files on Slack, as the {app} app. That alone "
    "reads nothing."
)
#: The one question, at a terminal and in the wizard. Its wording never changes, so a
#: replay of the wizard's step finds the answer it recorded.
QUESTION = "What else may Keryx do in your Slack?"
#: Each box: what Keryx may do, and beside it what that lets it see, in one line.
LABELS = {
    Permission.REQUESTS: "Act on what you type to the {app} app",
    Permission.READ: "Read your Slack as you, when you ask",
    Permission.SEND: "Send messages as you, after a yes",
}
HINTS = {
    Permission.REQUESTS: "each message starts or resumes work, with no PIN; it sees only "
                         "those messages",
    Permission.READ: "your conversations and search; with the first box, it also "
                     "answers your @{app} in a channel, where others read it",
    Permission.SEND: "read back to you first; it sees the names of your conversations "
                     "and of members, and no messages",
}
#: The same in full, said of what was ticked before the link is shown, and listed where
#: there is no terminal to tick at.
MEANING = {
    Permission.REQUESTS: (
        "Keryx acts on what you type to the {app} app itself: every message of yours in "
        "its direct messages is a request, with no @{app} needed. A new message starts "
        "work and a reply under a task's report resumes that task, both without the "
        "phone PIN. It is sent those messages and nothing else in Slack, and only your "
        "own member id is acted on."
    ),
    Permission.READ: (
        "Keryx reads Slack as you, only when you ask about messages, on a call or in a "
        "task: every channel, private channel, direct message and group message you are "
        "in, their lists, the member list, and search."
    ),
    Permission.SEND: (
        "Keryx sends messages as you, in your name, to other people and to channels you "
        "are in: a new message or a reply in a thread, only when you ask for that "
        "message, and on a call only after it has said who it goes to and the words, "
        "and you have said yes. A coding agent working on a request of yours can send "
        "one too."
    ),
}
#: Added when `requests` and `read` are both ticked: the one thing the pair unlocks.
#: `MENTIONS` is the thing itself, for where the pair is named some other way.
MENTIONS = (
    "you can also mention the app (@{app}) in any conversation it has been added to: "
    "every message that mentions it there is sent to Keryx, anyone's but yours is "
    "dropped, and yours is answered in that thread, where the others in the "
    "conversation read the answer too."
)
MENTION_MEANING = "With acting on what you type and reading both ticked, " + MENTIONS
#: Added when `send` is ticked and `read` is not: what sending alone lets Keryx see.
SEND_ALONE = (
    "To find who you mean it sees the names of the conversations you are in and the "
    "member list, and no message."
)

#: The voice tool that goes with each permission: on when it is ticked, off when it is
#: not (`sync_tools`). Ticking the box is the yes, because a permission with nothing on
#: a call to use it is what the owner met on 2026-11-24: the question was dispatched,
#: and answered from somewhere else.
VOICE_TOOLS = ((Permission.READ, "slack_read"), (Permission.SEND, "slack_message"))
#: Said once each, when a tool has been turned on.
TURNED_ON = {
    "slack_read": (
        "slack_read is on, from the next call: a question about your Slack is answered "
        "on the call"
    ),
    "slack_message": (
        "slack_message is on, from the next call: a message is sent as you on a call, "
        "after it has said who to and the words and you have said yes"
    ),
}
#: Said of one that is off where its box is ticked and nothing was changed: it was
#: turned off by hand, and this is the one command that turns it back on.
STILL_OFF = {
    "slack_read": "slack_read is off, so nothing reads Slack on a call: `keryx plugins "
                  "install slack_read` turns it back on.",
    "slack_message": "slack_message is off, so nothing sends as you on a call: `keryx "
                     "plugins install slack_message` turns it back on.",
}
#: Said when one is turned off because nothing is ticked for it any more.
TURNED_OFF = "{name} is off with it, from the next call."
#: Said with what was unticked: what a Keryx that is running does about it. Reading and
#: sending are asked of the settings at every use, so they stop there and then; the
#: connection `requests` listened on is closed by the next message Slack sends over it
#: (`SlackInbox.handle`), and nothing that arrives meanwhile is acted on.
STOPS = "A running Keryx stops at once."
STOPS_LISTENING = (
    "A running Keryx stops at once, and closes its connection to Slack at the next "
    "message it is sent there."
)
#: Said when a user token was shown to Slack with neither reading nor sending ticked.
USED_ONCE = (
    "The user token said which member you are, and is not saved: nothing that is ticked "
    "uses it."
)
#: Said when the user token may do more at Slack than is ticked: the app was not
#: reinstalled from the manifest that takes it away. `{named}` is ` --workspace NAME`.
MORE_THAN_TICKED = (
    "note: the app in Slack still grants more than is ticked ({scopes}). Keryx does not "
    "use it; pasting the manifest that `keryx auth login slack{named}` prints into the "
    "app, and reinstalling it, trims that"
)
#: Said when something was unticked: the machine has stopped, and Slack has not.
STILL_HOLDS = (
    "The app in Slack still holds the permissions it was made with until you paste this "
    "manifest into its App Manifest page ({url}) and reinstall it:"
)

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
#: With `requests`. The token needs no install, and its page is the one the new app
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
#: The steps that change the app there is, in place of the first above: to add what
#: was ticked, or to give an app made before user tokens the one it now needs.
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
#: Added to it where a message typed back is acted on (`requests`).
REQUESTS_TEST_MESSAGE = (
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
#: Said without `requests`, where nobody's messages are acted on.
OWNER_NOTED = (
    "{who} is taken to be you there, by your user token. Nothing you type there is "
    "acted on, since that is not ticked, and ticking it later keeps you as the owner."
)

#: Said when a token replaces another: `plugins.secret` prefers the settings a process
#: started with, so a running service goes on using the old one.
RESTART_NOTE = "A Keryx that started with the old token keeps it until `keryx restart`."
#: Said when a workspace starts being listened to, or its listening changes.
LISTEN_NOTE = (
    "Keryx starts listening for your messages there at its next start: `keryx restart`. "
    "From then on every message you type to the {app} app is a request."
)
#: Said with `requests`, every time: whose messages may start work with no PIN.
#: `{elsewhere}` is `MENTIONED` where a mention is a request too, and nothing otherwise.
OWNER_LINE = (
    "Only messages from {who} are acted on there: every one typed to the {app} app "
    "itself{elsewhere}. That member id is taken to be you, for as long as this workspace "
    "is connected."
)
MENTIONED = ", and elsewhere only one that mentions it (@{app})"
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


#: What `--allow` takes for "nothing ticked": the base alone, said on purpose.
NONE = "none"


def parse_allow(values: Sequence[str] | None) -> Allowed | None:
    """`--allow`, given once for each thing ticked, as that selection; None when it was
    not given at all, which is never read as "nothing". `none`, alone, is nothing
    ticked."""
    if not values:
        return None
    names = [value.strip().lower() for value in values]
    if NONE in names:
        if len(names) > 1:
            raise SlackSetupError(
                "--allow none is nothing but reports and files, so it goes alone")
        return BASE
    try:
        return slack.permissions(names)
    except ValueError:
        # What was given is never said back: it is whatever was on the command line.
        raise SlackSetupError(
            f"--allow takes {', '.join(PERMISSIONS[:-1])} or {PERMISSIONS[-1]}, once "
            f"for each, or {NONE} alone, and one given is none of those"
        ) from None


def allow_flags(allowed: Allowed) -> str:
    """`allowed` as the options that say it on a command line."""
    return "".join(f" --allow {each}" for each in slack.ordered(allowed)) or f" --allow {NONE}"


def choices(app: str, ticked: Iterable[Permission] = ()) -> list[Choice]:
    """The three boxes, for an app called `app`, with `ticked` already ticked: the one
    list the terminal and the wizard both show."""
    ticked = set(ticked)
    return [Choice(str(each), LABELS[each].format(app=app), hint=HINTS[each].format(app=app),
                   checked=each in ticked)
            for each in PERMISSIONS]


def meanings(allowed: Iterable[Permission], *, app: str, whole: Allowed | None = None) -> list[str]:
    """What each of `allowed` lets Keryx do and see, in full sentences, one to a
    permission. `whole` is everything that will be ticked, when `allowed` is only what
    is being added: what a pair unlocks is said when the pair is completed."""
    allowed = frozenset(allowed)
    whole = allowed if whole is None else whole
    lines = [MEANING[each] for each in slack.ordered(allowed)]
    if slack.mentions(whole) and allowed & {Permission.REQUESTS, Permission.READ}:
        lines.append(MENTION_MEANING)
    if Permission.SEND in allowed and Permission.READ not in whole:
        lines.append(SEND_ALONE)
    return [line.format(app=app) for line in lines]


def words(chosen: Iterable[Permission]) -> str:
    """`chosen` as a few plain words each, in a list to print."""
    named = [IN_WORDS[each] for each in slack.ordered(chosen)]
    return named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]


def offer(app: str, workspace: str = DEFAULT) -> list[str]:
    """What there is to choose from, for somebody with no terminal to tick at: every
    permission by the name `--allow` takes, in full, and the shapes of the command."""
    named = "" if workspace == DEFAULT else f" --workspace {workspace}"
    width = max(len(each) for each in PERMISSIONS)
    lines = [
        ALWAYS.format(app=app),
        "What else it may do is decided now, because it decides the app. Any of these, "
        "in any combination:",
        "",
        *(f"  {each:<{width}}  {MEANING[each].format(app=app)}" for each in PERMISSIONS),
        "",
        f"  {Permission.REQUESTS} and {Permission.READ} together: "
        + _capital(MENTIONS.format(app=app)),
        f"  {Permission.SEND} without {Permission.READ}: " + SEND_ALONE,
        "",
        f"At a terminal, `keryx auth login slack{named}` asks with a box to tick for each. "
        "Without one, say which, once for each:",
        f"  keryx auth login slack{named} --allow {NONE}",
        f"  keryx auth login slack{named} --allow {Permission.REQUESTS} --allow "
        f"{Permission.READ}",
        "Either prints the link that creates the app for exactly that, and its steps. "
        "Nothing was chosen for you, and nothing was saved.",
    ]
    return lines


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


def steps(allowed: Allowed = BASE, *, app: str, raising: bool = False,
          have: tuple[str, ...] = (), owner_known: bool = False) -> list[str]:
    """What to do in Slack for `allowed`, as Markdown lines without their numbers.

    `app` is what the app is called. `raising` is the app there is, changed in place;
    `have` is the tokens already on file, which that leaves as they are and which are not
    asked for again. `owner_known` is a workspace with its owner on file, which is asked
    for no user token unless reading or sending is ticked.

    **Grouped by page, in the order the pages are met, and each page visited once** (the
    owner's report from the real console): the app-level token before the install, then
    the install, then the one section that lists the user token and the bot token, in
    the order it lists them. `token_kinds` is the same order, so the prompts that follow
    ask for the tokens as the steps had them copied. Only what is ticked varies them:
    with no `requests` there is no app-level token and no step for one.
    """
    wanted = tuple(kind for kind in token_kinds(allowed, owner_known=owner_known)
                   if kind not in have)
    lines = list(RAISE_STEPS) if raising else [CREATE_STEP]
    if APP in wanted:
        lines.append(APP_TOKEN_STEP)
    lines.append(REINSTALL_STEP if raising else INSTALL_STEP)
    if tokens := oauth_tokens_step(wanted):
        lines.append(tokens)
    lines.append(ICON_STEP)
    return [line.format(app=app, icon=ICON) for line in lines]


def guide(allowed: Allowed = BASE, *, app: str, raising: bool = False,
          have: tuple[str, ...] = (), owner_known: bool = False) -> str:
    """The steps, as Markdown for the wizard, which shows the link above them."""
    listed = steps(allowed, app=app, raising=raising, have=have, owner_known=owner_known)
    return "\n".join(f"{number}. {step}" for number, step in enumerate(listed, 1))


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


#: Said when the plugin's file held a line from before the boxes that said nothing such
#: a line ever could: it stopped the file being read, so the sign-in rewrote it.
MENDED = (
    "The Slack settings held a line from before the boxes that was not understood. It is "
    "read as nothing ticked, and the file is rewritten without it."
)


def mend(settings: Settings, echo: Callable[[str], None]) -> None:
    """Rewrite a plugin's file that only a line from before the boxes stops being read
    (`plugins.mend`), and say so: the first thing every sign-in does, since the refusal
    of such a line says that this command rewrites it."""
    if plugins.mend(settings, PLUGIN):
        echo(MENDED)


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


def _flags(name: str, allowed: Allowed, current: Allowed, *, connected: bool = True) -> str:
    """The options that name this workspace and what is ticked again on the next
    command. What is ticked is said only when it is not what is on file already."""
    flags = "" if name == DEFAULT else f" --workspace {name}"
    return flags + ("" if connected and allowed == current else allow_flags(allowed))


def lacking_bot_scopes(checked: Workspace, allowed: Allowed) -> tuple[str, ...]:
    """What the app's bot may not do yet that the manifest for `allowed` asks for, from
    what Slack said of its bot token: an app made from an earlier manifest. Empty when
    it lacks nothing, and when Slack did not answer or did not say what the token may
    do, which is never taken to mean "nothing"."""
    if not checked.ok or checked.scopes is None:
        return ()
    return tuple(scope for scope in slack.bot_scopes(allowed) if scope not in checked.scopes)


def _for_files(lacks: tuple[str, ...]) -> bool:
    return any(scope in lacks for scope in slack.FILE_BOT_SCOPES)


def lacks_note(lacks: tuple[str, ...], workspace: str) -> str:
    """One line for a connected app that lacks a scope: what that costs, and the fix."""
    named = "" if workspace == DEFAULT else f" --workspace {workspace}"
    cost = ("so it cannot send a file yet; everything else works as it did"
            if set(lacks) <= set(slack.FILE_BOT_SCOPES)
            else "so not everything you ticked works yet")
    return (f"note: the app in Slack lacks {', '.join(lacks)}, {cost}. To add it: `keryx "
            f"auth login slack{named}` prints the manifest to paste into the app, which "
            "is then reinstalled")


def is_connected(settings: Settings, workspace: str = DEFAULT) -> bool:
    """Whether Keryx holds a bot token of its own for `workspace`: there is an app."""
    try:
        return bool(on_file(settings, workspace).token(BOT))
    except SlackSetupError:
        return False


def start(
    settings: Settings,
    echo: Callable[[str], None],
    *,
    workspace: str = DEFAULT,
    allowed: Allowed | None = None,
    waiting: bool = False,
    boxes: bool = False,
    api: "SlackApi | None" = None,
) -> Allowed | None:
    """`keryx auth login slack` with no token: what was ticked, in full, then the link
    (or the manifest, for an app there already is), the steps, and what comes next. A
    workspace that is connected with exactly that ticked, and every token a login takes,
    is told so, and never shown the link that would make a second app: with neither
    reading nor sending ticked and its owner on file, that is without a user token. One
    connected before user tokens were asked for, which has no owner on file, is shown
    how to update the app it has, and so is one whose app lacks a scope its manifest
    now has (`api`, which is how Slack is asked what the bot token may do: once, and
    only for a workspace that is otherwise whole; with none given, nothing is asked).

    `allowed` is what is ticked; None is what the workspace has. **With nothing
    connected and nothing said, nothing is guessed**: the choices are printed, with the
    shapes of the command that says one, and that is all.

    What comes next depends on who is there. With nobody at a terminal it is the next
    command, `--stdin`, which is the one place that sentence is printed. `waiting` is a
    terminal, where this same command goes on to ask for the tokens: the steps end by
    sending the owner back to it. `boxes` says the boxes were just shown there, with
    what is always on said above them, so it is not said again. Returns what is ticked,
    whose tokens are wanted, or None when there is nothing to hand over: the workspace
    is whole, or nothing was chosen.
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
    if allowed is None and not connected:
        for line in offer(app, workspace):
            echo(line)
        return None
    current = saved.allowed if connected else BASE
    allowed = current if allowed is None else allowed
    # Who the owner is, is asked once: a workspace that has one on file takes a user
    # token only for reading or sending as them.
    owner_known = connected and bool(saved.owner_id)
    needed = token_kinds(allowed, owner_known=owner_known)
    lacking = [kind for kind in needed if not saved.token(kind)]
    lacks: tuple[str, ...] = ()
    if connected and allowed == current and lacking != [USER]:
        if api is not None and not lacking:
            lacks = lacking_bot_scopes(api.auth_test(saved.token(BOT) or ""), allowed)
        if not lacks:
            # Whole, or without something it runs on, which `_already` says.
            _already(saved, echo, app=app, assistant=settings.assistant_name)
            for permission, name in VOICE_TOOLS:
                if permission in current and not plugins.tool_path(settings, name).is_file():
                    # Turned off by hand, and this login changes nothing, so it names
                    # the one command that turns it back on.
                    echo("\n" + STILL_OFF[name])
            return None
    # The app there is, is changed and never made again: to add what was ticked, or
    # (with nothing new) to give it the user token an app made before them never issued.
    raising = connected
    have = tuple(kind for kind in needed if saved.token(kind)) if raising else ()
    added = allowed - current
    if lacks:
        what = (", which sending a file takes. Nothing about it stops working: reports "
                "arrive as they did, and a file cannot be sent until the app has it"
                if _for_files(lacks) else
                ". What it could do it still does")
        echo(f"{saved.label} is connected, with an app made before Keryx asked for "
             f"{', '.join(lacks)}{what}. The app you have is updated, not made again:\n")
    elif raising and not added and owner_known:
        # Ticked, and without the token it runs on: unset by hand, since a login never
        # leaves it so.
        acting = slack.ordered(allowed & {Permission.READ, Permission.SEND})
        echo(f"{saved.label} is connected, and has no user token on file, which "
             f"{words(acting)} {'take' if len(acting) > 1 else 'takes'}. The app you have "
             "is updated, not made again:\n")
    elif raising and not added:
        echo(f"{saved.label} is connected, and has no owner on file: it was connected "
             "before Keryx asked for a user token. Nothing about it stops working. The "
             "next login takes one, because it is how Slack says which member you are, "
             "so the app you have is updated, not made again:\n")
    elif raising:
        echo(f"{saved.label} is connected. Adding: {words(added)}.")
        for line in meanings(added, app=app, whole=allowed):
            echo(f"  {line}")
        echo("\nThe app you have is changed, not made again:\n")
    else:
        if not boxes:
            echo(ALWAYS.format(app=app))
        if allowed:
            echo(f"You ticked: {words(allowed)}.")
            for line in meanings(allowed, app=app):
                echo(f"  {line}")
        else:
            echo("You ticked nothing else, so that is all it does.")
        echo("\nCreate your Slack app from this link:\n")
        echo(slack.new_app_url(allowed, assistant=settings.assistant_name))
        echo("")
    listed = steps(allowed, app=app, raising=raising, have=have, owner_known=owner_known)
    for number, step in enumerate(listed, 1):
        echo(f"{number}. {shown(step)}")
    if raising:
        echo("\nThe manifest:\n")
        echo(slack.manifest_json(allowed, assistant=settings.assistant_name))
        echo("\n" + RENAME_NOTE.format(app=app))
        if BOT in have:
            echo("The bot token stays as it is.")
    wanted = [kind for kind in needed if kind not in have]
    many = len(wanted) > 1
    what = "the tokens" if many else f"the {TOKEN_NAMES[wanted[0]]}" if wanted else ""
    # Where reports will go: where they go now, while the bot token stays; else to them.
    where = (f"reports keep going to {saved.channel}" if BOT in have and saved.channel
             else "reports go to you")
    said = (f"The user token says which member you are: {where}, and nothing else is asked."
            if USER in needed else
            f"Which member you are is on file, so no user token is asked for: {where}.")
    if waiting:
        # This command is still running, so the last thing said is to come back to it.
        echo("\n" + said)
        if wanted:
            echo(f"Then come back to this terminal, which is waiting, and paste {what} "
                 f"where {'each is' if many else 'it is'} asked for. What you paste is "
                 "not shown.")
        else:
            echo("Every token this needs is saved already. Once the app is reinstalled, "
                 "come back to this terminal, which is waiting, and press Enter at each "
                 "token to keep it"
                 + (", or paste the User OAuth Token if Slack shows a different one "
                    "after the reinstall." if USER in have else "."))
        return allowed
    flags = _flags(workspace, allowed, current, connected=connected)
    command = f"keryx auth login slack --stdin{flags}"
    if wanted:
        echo(
            f"\nThen hand {what} over on standard input, never on the command line"
            + (", one to a line in any order" if many else "")
            + f":\n  {command}"
        )
    else:
        echo("\nEvery token this needs is saved already, so there is nothing to hand "
             f"over. Once the app is reinstalled:\n  {command} < /dev/null")
        if USER in have:
            echo("If Slack shows a different User OAuth Token after the reinstall, hand "
                 "that one over instead of nothing.")
    echo(said)
    return allowed


def sync_tools(settings: Settings, allowed: Allowed, echo: Callable[[str], None]) -> None:
    """Make the voice tools match what is ticked, after a change to it: each one whose
    box is in `allowed` is turned on when it is not, and each one that no workspace has
    a tick for any more is turned off (`retire`). Called only when what is ticked has
    changed, so one turned off by hand stays off until then."""
    for permission, name in VOICE_TOOLS:
        if permission not in allowed or plugins.tool_path(settings, name).is_file():
            continue
        try:
            plugins.install(settings, name)
        except plugins.PluginConfigError as error:
            echo(str(error))
        else:
            echo(TURNED_ON[name])
    retire(settings, echo)


def retire(settings: Settings, echo: Callable[[str], None]) -> None:
    """Turn off each voice tool that no workspace has a tick for any more: left on, it
    would be refused at every call and reported by `keryx tools` until somebody removed
    it. Its settings are set aside, as `keryx plugins remove` leaves them.

    The one function behind every way a tick goes: unticking, logging out, and turning
    `slack_report` itself off, in the wizard's list and with `keryx plugins remove`
    alike, after which no workspace has a tick for either."""
    for name, ready in (("slack_read", plugin.can_read), ("slack_message", plugin.can_write)):
        if plugins.tool_path(settings, name).is_file() and not ready(settings):
            plugins.remove(settings, name)
            echo(TURNED_OFF.format(name=name))


def _already(saved: SlackWorkspace, echo: Callable[[str], None], *, app: str,
             assistant: str) -> None:
    """What a workspace that is connected with exactly what was asked for is told: how
    it stands, the three things that change it, and its manifest, which is the one way
    the app in Slack is renamed after the assistant. Never the link that makes a second
    app."""
    named = "" if saved.name == DEFAULT else f" --workspace {saved.name}"
    echo(f"{saved.label} is connected, and nothing was changed. "
         f"{_capital(in_words(saved.allowed))}.")
    if saved.missing:
        echo(f"It is without {' or '.join(saved.missing)}.")
    if Permission.REQUESTS in saved.allowed and saved.owner_id:
        echo(owner_line(saved.owner_id, saved.allowed, app))
        echo(f"To connect as somebody else: keryx auth logout slack{named}, then log in "
             "again.")
    echo(
        f"\nTo change what is on: keryx auth login slack{named} at a terminal, and tick "
        f"or untick it (with no terminal: --allow {NONE}, or --allow once for each of "
        f"{', '.join(PERMISSIONS)} that is to be on)"
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
    echo(slack.manifest_json(saved.allowed, assistant=assistant))
    echo("\n" + shown(ICON_STEP.format(icon=ICON)))


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def owner_line(who: str, allowed: Allowed, app: str) -> str:
    """Whose messages are acted on, and where: said on every connect with `requests`."""
    elsewhere = MENTIONED.format(app=app) if slack.mentions(allowed) else ""
    return OWNER_LINE.format(who=who, app=app, elsewhere=elsewhere)


@dataclass(frozen=True)
class Unticked:
    """What `untick` did, and what is left to do."""

    #: The settings to go on with: the ones given, without a token that was forgotten.
    settings: Settings
    #: What was taken away, which is done.
    removed: Allowed = BASE
    #: What `allowed` would add, which is not: it is the rest of the login.
    added: Allowed = BASE

    @property
    def done(self) -> bool:
        """Taking away was all there was to do, and it is done."""
        return bool(self.removed) and not self.added


#: The setting each of the first workspace's tokens is, as a field of `Settings`.
_TOKEN_FIELDS = {BOT: "slack_bot_token", APP: "slack_app_token", USER: "slack_user_token"}


def untick(
    settings: Settings,
    store: ConfigStore,
    *,
    workspace: str,
    allowed: Allowed,
    echo: Callable[[str], None],
    steps_follow: bool = False,
    show: Callable[[str], None] | None = None,
) -> Unticked:
    """Take away, now, whatever a connected workspace has ticked that `allowed` has not,
    and say what `allowed` would add.

    Slack is not asked anything, because taking a permission away needs nothing from it:
    what is ticked is saved without what was removed, the tokens that no longer uses are
    forgotten, the voice tools nothing is ticked for are turned off, and a running
    service honours all of it at once. **Every path that takes a selection calls this
    before it does anything else**: the terminal, the pipe and the wizard. The app in
    Slack keeps what it was made with until its manifest is pasted and it is reinstalled,
    which is said, with the manifest for `allowed` (through `show`, when the caller has
    its own way to show one); only where the steps that follow print that manifest
    themselves (`steps_follow`, with something added) is it left to them. Whatever is
    *added* is not applied here: it needs the app changed and tokens checked, which is
    the rest of the login. Nothing is touched, and both are empty, for a workspace that
    is not connected.
    """
    values = _config(settings)
    saved = on_file(settings, workspace, values)
    if not saved.token(BOT):
        return Unticked(settings)
    removed, added = saved.allowed - allowed, allowed - saved.allowed
    if not removed:
        return Unticked(settings, added=added)
    staying = saved.allowed & allowed
    # A token is on file only while something ticked uses it. The user token goes with
    # the last of reading and sending: it keeps those permissions at Slack until the app
    # is reinstalled, Keryx has no use for it meanwhile, and who the owner is stays on
    # file without it. Ticking either again asks for the one the app then issues.
    keeping = slack.used_kinds(staying)
    kept = {kind: saved.tokens[kind] for kind in saved.tokens if kind in keeping}
    dropped = [kind for kind in saved.tokens if kind not in kept]
    # The tokens first: should the file then refuse the write, what is still ticked has
    # nothing to run on, which is closed.
    _save_tokens(store, settings, workspace, kept)
    try:
        _write_workspace(settings, values, workspace, {"allow": _stored(staying)})
    except plugins.PluginConfigError as error:
        raise SlackSetupError(
            f"the tokens it used are forgotten, but {error}") from None
    gone = slack.ordered(removed)
    echo(f"{saved.label}: {words(gone)} {'are' if len(gone) > 1 else 'is'} off, from this "
         f"moment. {STOPS_LISTENING if Permission.REQUESTS in removed else STOPS}")
    if dropped:
        names = " and ".join(_stored_as(workspace, kind) for kind in dropped)
        echo(f"{names} {'are' if len(dropped) > 1 else 'is'} forgotten.")
    retire(settings, echo)
    if added and steps_follow:
        echo("The app in Slack still holds the permissions it was made with until the "
             "manifest below is pasted into it.\n")
    else:
        echo(STILL_HOLDS.format(url=APPS_URL) + ("\n" if show is None else ""))
        (show or echo)(slack.manifest_json(allowed, assistant=settings.assistant_name))
    if workspace == DEFAULT and dropped:
        # A token these settings were loaded with is not on file any more.
        settings = settings.model_copy(
            update={_TOKEN_FIELDS[kind]: None for kind in dropped})
    return Unticked(settings, removed=removed, added=added)


def _stored(allowed: Allowed) -> list[str]:
    """What is ticked as the plugin's file holds it: `allow`, a list in one order."""
    return [str(each) for each in slack.ordered(allowed)]


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
                "channel_id": "", "allow": [], "owner_id": "", "team": "",
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
    retire(settings, echo)
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
            echo(f"{_a(TOKEN_NAMES[kind]).capitalize()} is needed for this, and none is "
                 "saved: paste it, or press Enter again to stop.")
            continue
        problem = wrong_token(answer, kind)
        if problem is None:
            return answer
        wrongs += 1
        echo(problem)
        if wrongs >= MAX_WRONG:
            raise _Left


def pick(choose: Callable[[str, Sequence[Choice]], Sequence[str]], *, app: str,
         ticked: Allowed, echo: Callable[[str], None]) -> Allowed:
    """The boxes, at a terminal: what is always on, said above them, then the one
    question with `ticked` already ticked. `choose` shows a list of boxes and answers
    with the values ticked (`setup.ui.ask_checkbox`); leaving it raises `_Left`."""
    echo(ALWAYS.format(app=app) + "\n")
    try:
        answer = choose(QUESTION, choices(app, ticked))
    except (EOFError, KeyboardInterrupt):
        raise _Left from None
    return slack.permissions(answer)


def login(
    settings: Settings,
    store: ConfigStore,
    *,
    ask: Callable[[str], str],
    echo: Callable[[str], None],
    choose: Callable[[str, Sequence[Choice]], Sequence[str]] | None = None,
    test: bool = True,
    steps: bool = True,
    api: SlackApi | None = None,
    workspace: str = DEFAULT,
    allowed: Allowed | None = None,
) -> None:
    """`keryx auth login slack` at a terminal: one command, which waits.

    **The boxes first** (`pick`, through `choose`), with what the workspace has already
    ticked when it is connected: not when `allowed` says what is ticked (`--allow`), and
    not for `--stdin` typed at a terminal for a workspace that is connected, which is
    how a token is replaced. Then whatever was unticked is taken away at once
    (`untick`); if that was all, the command ends there. Then the link or the manifest
    and the steps (`start`; not with `steps` off), and each token still needed is asked
    for, hidden, through `ask`: one on file is kept with Enter, one that is not that
    token is said so and asked again. The user token is among them only with reading or
    sending ticked, or for a workspace with no owner on file yet (`token_kinds`). Then
    everything `connect` does, with all the
    tokens at once, so nothing more is saved until every one is taken. A token Slack
    refuses (`TokenRefused`) is named and asked for again, `MAX_REFUSALS` times in all.
    Leaving at any question raises `Stopped`, whose sentence says the same command picks
    it up. A workspace that is whole with nothing changed is told how it stands and
    asked nothing, unless Slack says its app lacks a scope its manifest has
    (`lacking_bot_scopes`): then the steps are the ones that update the app there is,
    and Enter at each token is all that is asked.
    """
    named = "" if workspace == DEFAULT else f" --workspace {workspace}"
    again = "keryx auth login slack" + ("" if steps else " --stdin") + named + (
        "" if allowed is None else allow_flags(allowed))
    values = _config(settings)
    if workspace != DEFAULT and (problem := plugins.plugin(PLUGIN).problem(dict(values))):
        raise SlackSetupError(
            f"{workspace} is added beside your first workspace, and there is none yet "
            f"({problem}): run it without --workspace first. Nothing was saved"
        )
    saved = on_file(settings, workspace, values)
    connected = bool(saved.token(BOT))
    current = saved.allowed if connected else BASE
    api = api or SlackApi()
    removed: Allowed = BASE
    given: dict[str, str] = {}
    boxes = False
    try:
        if allowed is None and choose is not None and (steps or not connected):
            allowed = pick(choose, app=app_of(settings), ticked=current, echo=echo)
            boxes = True
            echo("")
        allowed = current if allowed is None else allowed
        unticked = untick(settings, store, workspace=workspace, allowed=allowed, echo=echo,
                          steps_follow=steps)
        if unticked.done:
            return
        if removed := unticked.removed:
            settings = unticked.settings
            saved = on_file(settings, workspace)
        if steps:
            wanted = start(settings, echo, workspace=workspace, allowed=allowed,
                           waiting=True, boxes=boxes, api=api)
            if wanted is None:
                return
            echo("")
        for kind in token_kinds(allowed, owner_known=connected and bool(saved.owner_id)):
            if answer := ask_token(kind, saved=bool(saved.token(kind)), ask=ask, echo=echo):
                given[kind] = answer
        for refusals in range(1, MAX_REFUSALS + 1):
            try:
                connect(settings, store, tokens=given, test=test, echo=echo, api=api,
                        workspace=workspace, allowed=allowed)
                return
            except TokenRefused as refused:
                echo(f"The {TOKEN_NAMES[refused.kind]} was not taken: {refused.why}. "
                     f"Nothing {'else ' if removed else ''}was saved.")
                if refusals == MAX_REFUSALS:
                    break
                # The one on file is offered again only when it is not what was refused.
                on_hand = bool(saved.token(refused.kind)) and refused.kind in given
                given.pop(refused.kind, None)
                if answer := ask_token(refused.kind, saved=on_hand, ask=ask, echo=echo):
                    given[refused.kind] = answer
    except _Left:
        pass
    kept = ("what you unticked stays off, and nothing else was saved" if removed
            else "nothing was saved")
    raise Stopped(f"{kept}: run `{again}` again to pick this up")


def hand_over(
    settings: Settings,
    store: ConfigStore,
    *,
    tokens: str,
    test: bool,
    echo: Callable[[str], None],
    api: SlackApi | None = None,
    workspace: str = DEFAULT,
    allowed: Allowed | None = None,
) -> None:
    """`keryx auth login slack --stdin` with no terminal: what a pipe or a coding agent
    hands over, in the order a terminal takes it.

    **Whatever `allowed` unticks is taken away first** (`untick`): saved, its tokens
    forgotten and its voice tool turned off with nothing asked of Slack, and the
    manifest that takes it away there printed, whatever becomes of the tokens after.
    With nothing on standard input and nothing added, that is all, so unticking by pipe
    works while Slack cannot be reached. Then `connect`, with what was handed over. A
    refusal there says that what was unticked stays off.
    """
    unticked = Unticked(settings)
    if allowed is not None:
        unticked = untick(settings, store, workspace=workspace, allowed=allowed, echo=echo)
        if unticked.done and not tokens.strip():
            return
    try:
        connect(unticked.settings, store, tokens=tokens, test=test, echo=echo, api=api,
                workspace=workspace, allowed=allowed)
    except SlackSetupError as error:
        if not unticked.removed:
            raise
        raise SlackSetupError(_after_untick(str(error))) from None


def _after_untick(refusal: str) -> str:
    """A refusal's sentence, said after something was unticked: that much was saved."""
    for said in ("Nothing was saved", "nothing was saved"):
        if said in refusal:
            return refusal.replace(
                said, f"{said[:7]} else was saved, and what you unticked stays off")
    return f"{refusal}. What you unticked stays off"


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
    allowed: Allowed | None = None,
) -> None:
    """Check the tokens, learn who the owner is, save it all, turn the plugin on (and
    make the voice tools match what is ticked when that changed, `sync_tools`), and
    prove it.

    `allowed` is what is ticked; None is what the workspace has. `tokens` is standard
    input: one token to a line, told apart by how each starts; or, from a terminal that
    asked for each by name, the tokens by kind. A token this takes and that is on file
    already need not be given again. **What is saved is what a connection runs on**
    (`used_kinds`): the user token only with reading or sending ticked. With neither it
    is needed only while no owner is on file, shown to Slack once, and not saved; one
    handed over beside an owner on file is held to that owner and not saved either. A
    token that is refused
    for what it is raises `TokenRefused`, which says which. Raises
    `SlackSetupError` saying how far it got. Nothing is saved until every token is one
    Slack takes, so a token Slack refuses leaves a working install exactly as it was.
    After the save, only the test message can fail, and that says so and undoes nothing.

    **The owner is whoever the user token belongs to**, whatever is ticked: `auth.test`
    on it has to name the bot token's own workspace, and its member is the owner. An owner
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
    was = saved.allowed if saved.token(BOT) else BASE
    allowed = saved.allowed if allowed is None else allowed
    acts = as_owner(allowed)
    # A user token may always be handed over; whether one is needed follows from what
    # is ticked and whether the owner is on file. Only the app-level one can be extra.
    takes = token_kinds(allowed)
    if extra := [kind for kind in given if kind not in takes]:
        names = " or ".join(f"{_a(TOKEN_NAMES[kind])} ({TOKEN_PREFIXES[kind]})" for kind in extra)
        raise SlackSetupError(
            f"what is ticked does not use {names}: leave it out, or add --allow "
            f"{Permission.REQUESTS}. Nothing was saved"
        )
    held = {kind: given.get(kind) or saved.token(kind) or "" for kind in takes}
    needed = token_kinds(allowed, owner_known=bool(saved.token(BOT) and saved.owner_id))
    if lacking := [kind for kind in needed if not held[kind]]:
        names = " and ".join(f"the {TOKEN_NAMES[kind]} ({TOKEN_PREFIXES[kind]})"
                             for kind in lacking)
        if not given:
            raise SlackSetupError(f"no token on standard input: this needs {names}")
        raise SlackSetupError(f"this also needs {names}, one to a line. Nothing was saved")
    before = saved.token(BOT) or ""
    token = held[BOT]

    team = api.auth_test(token)
    if team.refused:
        raise TokenRefused(BOT, team.problem, f"{team.problem}; nothing was saved")
    if not team.ok:
        raise SlackSetupError(
            f"the token could not be checked ({team.problem}); nothing was saved")
    if Permission.REQUESTS in allowed and not (team.team_id and team.user_id):
        raise SlackSetupError(
            "Slack did not say which workspace the bot token belongs to, or which member "
            "the app is there, and every message you type to it is held to both. Nothing "
            "was saved"
        )
    if problem := another_workspace(saved, team):
        raise SlackSetupError(problem)
    standing = standing_owner(saved, team, same_app=token == before)
    # Whose the user token is, is asked of Slack when it is to be kept (reading or
    # sending), when nobody is the owner yet, and when one was handed over anyway, which
    # is still held to the owner on file. Otherwise that owner stands and none is asked.
    shown_user: Workspace | None = None
    if acts or not standing or USER in given:
        if not held[USER]:
            raise SlackSetupError(
                f"this also needs the user token ({TOKEN_PREFIXES[USER]}), which says "
                "which member you are: nobody is on file as the owner for this bot "
                "token. Nothing was saved")
        shown_user = _check_user_token(api, held[USER], team, allowed)
    token_owner = shown_user.user_id if shown_user else standing
    # What is kept is what a connection runs on: the user token only while it acts.
    keeping = {kind: held[kind] for kind in slack.used_kinds(allowed)}
    if APP in keeping:
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

    _save_tokens(store, settings, workspace, keeping)
    echo(_connected_line(workspace, team, tuple(keeping)))
    echo(_capital(in_words(allowed)) + ".")
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
    echo(owner_line(acted_on, allowed, app) if Permission.REQUESTS in allowed
         else OWNER_NOTED.format(who=acted_on))
    try:
        _write_workspace(settings, values, workspace, {
            "channel_id": channel,
            "allow": _stored(allowed),
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
    # The voice tools follow the ticks, so that ticking a box is one command and not
    # two. Only when what is ticked changed: one turned off by hand stays off till then.
    if allowed != was:
        sync_tools(settings, allowed, echo)
    if lacks := lacking_bot_scopes(team, allowed):
        echo(lacks_note(lacks, workspace))
    if server:
        echo(f"note: {PLUGIN} now posts with this token to {channel}, not with the "
             f"{server} MCP server's token and channel, and subagents are told to send "
             "with `keryx slack report`, no longer through that server")
    if Permission.REQUESTS in allowed:
        echo(LISTEN_NOTE.format(app=app))
        if not (is_member(channel) or slack.is_dm(channel)):
            echo(CHANNEL_NOTE.format(channel=channel))
    for kind in saved.tokens:
        if kind in keeping:
            continue
        if kind == USER and not as_owner(saved.allowed):
            # One an earlier build saved with neither box ticked, to say who the owner is.
            echo(f"{_stored_as(workspace, kind)} is forgotten: it was kept only to say "
                 "which member you are, which is on file, and nothing that is ticked "
                 "uses it")
            continue
        echo(f"{_stored_as(workspace, kind)} is forgotten: what is ticked does not "
             "use it" + (", and until the app is reinstalled it could still act as "
                         "you in Slack" if kind == USER else ""))
    if shown_user is not None and not acts:
        echo(USED_ONCE)
    if shown_user is not None and (more := beyond_ticked(shown_user, allowed)):
        echo(MORE_THAN_TICKED.format(scopes=", ".join(more), named=named))

    if not test:
        echo("no test message was sent (--no-test), so nothing has shown it arrives")
        return
    sent = api.post_text(token, channel, test_message(allowed, app))
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
    login. Recorded whatever is ticked, nothing included, so ticking more later changes
    nothing about who the owner is.
    """
    if standing and token_owner != standing:
        why = (f"belongs to {token_owner}, and the member whose messages Keryx acts on "
               f"there is {standing}: paste your own user token. To connect as somebody "
               f"else, `keryx auth logout slack{named}` first")
        raise TokenRefused(USER, f"it {why}", f"the user token {why}. Nothing was saved")
    return token_owner


def test_message(allowed: Allowed, app: str) -> str:
    """The one message that shows it arriving, saying what a reply to it does. `app` is
    what the app is called, which is what the owner would type to mention it."""
    said = TEST_MESSAGE + (REQUESTS_TEST_MESSAGE if Permission.REQUESTS in allowed else "")
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


def scope_problem(checked: Workspace, allowed: Allowed) -> str | None:
    """What a user token Slack answered for may not yet do that `allowed` needs; None
    when it may, and when Slack did not say what it may do (which is then found out in
    use)."""
    if checked.scopes is None:
        return None
    # With neither reading nor sending ticked the token is asked who it is and nothing
    # more, which takes no scope.
    wanted = slack.user_scopes(allowed) if as_owner(allowed) else ()
    lacking = [scope for scope in wanted if scope not in checked.scopes]
    if not lacking:
        return None
    return (f"it may not yet do what you ticked (it lacks {', '.join(lacking)}): save "
            "the manifest in the app, reinstall it to the workspace, and hand over the "
            "User OAuth Token shown after that")


def beyond_ticked(checked: Workspace, allowed: Allowed) -> tuple[str, ...]:
    """What a user token Slack answered for may do there that nothing ticked uses: the
    history, search, lookup and posting scopes a manifest of Keryx's could have asked
    for and the one for `allowed` does not. Empty when there is none, and when Slack did
    not say what the token may do.

    Such a token is the app's from before a box was unticked, not yet reinstalled from
    the manifest that takes the scope away. It is taken all the same, because Keryx
    cannot shrink a token and what it does with one is gated on what is ticked; this is
    what the one line that says so is made from (`MORE_THAN_TICKED`). The scope that
    only makes Slack issue a user token (`IDENTITY_USER_SCOPES`) is never counted."""
    if checked.scopes is None:
        return ()
    ticked = slack.user_scopes(allowed) if as_owner(allowed) else ()
    known = slack.LOOKUP_USER_SCOPES + slack.READ_USER_SCOPES + slack.SEND_USER_SCOPES
    return tuple(scope for scope in dict.fromkeys(known)
                 if scope in checked.scopes and scope not in ticked)


def _check_user_token(api: SlackApi, token: str, team: Workspace,
                      allowed: Allowed = BASE) -> Workspace:
    """What Slack says of the user token, whose member is the owner; refused in a
    sentence."""
    checked = api.auth_test(token)
    if problem := user_token_problem(checked, team) or scope_problem(checked, allowed):
        message = f"the user token cannot be used: {problem}. Nothing was saved"
        # The token's own fault when Slack answered for it (or refused it) and the bot
        # token's workspace is known; anything else is Slack not having been asked.
        its_own = (checked.ok or checked.refused) and bool(team.team_id)
        raise TokenRefused(USER, problem, message) if its_own else SlackSetupError(message)
    return checked


def _connected_line(workspace: str, team: Workspace, kinds: tuple) -> str:
    where = f"connected to the {team.team or 'Slack'} workspace"
    if workspace != DEFAULT:
        return f"{where} as {workspace}; its tokens are saved"
    # In the order the settings are listed, whatever order the tokens were asked for in.
    saved = [key for kind, key in TOKEN_KEYS.items() if kind in kinds]
    names = saved[0] if len(saved) == 1 else ", ".join(saved[:-1]) + " and " + saved[-1]
    return f"{where}; {names} saved"
