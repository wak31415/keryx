"""Connecting Slack: what the wizard's Slack step and `keryx auth login slack` share.

Each owner makes a Slack app of their own, from the manifest in `integrations.slack`, and
pastes its bot token once. Everything after that Keryx does: `auth.test` says whose token
it is, `users.lookupByEmail` turns the address they use in Slack into the member to send
to (stored as the plugin's `channel_id`, so nobody copies a channel id out of Slack), and
one test message shows it arriving.

The wizard asks its questions in `setup/plugins.py::run_slack`, through `Probes`. This
module holds the wording both use and `connect`, the command-line path, which is the same
steps with nothing to ask: the token on standard input, the address as `--email`. The
token is never an argument, never echoed and never in an error; what Slack refused is its
error code and nothing else it said.

Two rules both paths keep. **A token brings its destination with it** (`keeps`): a direct
message's id (`D…`) belongs to the app that opened it, so a token that is not the one on
file is never paired with one — not the one in the plugin's file, and not a named MCP
server's — and the owner is looked up again instead. A member's id or a channel's is the
workspace's and is kept. **Nothing is called connected that was not shown to arrive**:
either the test message went, or the last line says none was sent and why.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keryx import plugins
from keryx.config import Settings
from keryx.config.permissions import OWNER
from keryx.config.store import FROM_ENV, ConfigError, ConfigStore
from keryx.integrations import slack
from keryx.integrations.slack import Member, SlackResult, Workspace

PLUGIN = "send_to_slack"
TOKEN_KEY = "SLACK_BOT_TOKEN"

#: The steps in Slack, after the link. Markdown; `plain` is the same without the marks.
STEPS = (
    "Open the link: it is Slack's page for a new app, with Keryx's already filled in. "
    "Pick your workspace and click through to **Create**.",
    "Click **Install to Workspace**, then **Allow**.",
    "Under **OAuth & Permissions**, copy the **Bot User OAuth Token** (`xoxb-…`).",
)

#: Said with the question that asks for an id by hand, when Slack could not find them.
MEMBER_ID_HINT = (
    "A member id works as well as a channel id: in Slack, open your profile, click More "
    "(⋮), then Copy member ID."
)

TEST_MESSAGE = (
    "Keryx is connected. What you ask for in writing on a call arrives here, and so does "
    "each finished task's report."
)

BY_HAND = f"`keryx plugins install {PLUGIN} --set channel_id=ID` with a channel or member id"

#: Said when a token replaces another: `plugins.secret` prefers the settings a process
#: started with, so a running service goes on using the old one.
RESTART_NOTE = "A Keryx that started with the old token keeps it until `keryx restart`."


class SlackSetupError(RuntimeError):
    """Connecting Slack could not go on, in a sentence that never holds the token."""


@dataclass(frozen=True)
class SlackApi:
    """The three calls `connect` makes, replaced in the tests."""

    auth_test: Callable[[str], Workspace] = slack.auth_test
    lookup_by_email: Callable[[str, str], Member] = slack.lookup_by_email
    post_text: Callable[[str, str, str], SlackResult] = slack.post_text


def token_problem(value: str) -> str | None:
    """Why `value` cannot be a Slack token, before Slack is asked; None when it could be."""
    return None if value.strip().startswith("xox") else "A Slack token starts xox."


def guide() -> str:
    """The steps, as Markdown for the wizard, which shows the link above them."""
    return "\n".join(f"{number}. {step}" for number, step in enumerate(STEPS, 1))


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


def without_email(settings: Settings) -> str:
    """What `connect` does when no `--email` is given, in a sentence, as things stand."""
    try:
        channel = _config(settings)["channel_id"]
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


def start(settings: Settings, echo: Callable[[str], None]) -> None:
    """`keryx auth login slack` with no token: the link, the steps, and the next command."""
    echo("Create your Slack app from this link:\n")
    echo(slack.new_app_url())
    echo("")
    for number, step in enumerate(STEPS, 1):
        echo(f"{number}. {plain(step)}")
    echo(
        "\nThen hand the token over on standard input, never on the command line, with "
        "the address you use in Slack:"
        "\n  keryx auth login slack --stdin --email ADDRESS"
    )
    echo(without_email(settings))


def connect(
    settings: Settings,
    store: ConfigStore,
    *,
    token: str,
    email: str | None,
    test: bool,
    echo: Callable[[str], None],
    api: SlackApi | None = None,
) -> None:
    """Check the token, find where to send, save both, turn the plugin on, and prove it.

    Raises `SlackSetupError` saying how far it got. Nothing is saved until there is a
    token Slack takes *and* a destination that goes with it, so a token Slack refuses or
    an address it cannot find leaves a working install exactly as it was. A token that is
    not the one on file brings its destination with it (`keeps`): an address is looked
    up, never a direct message's old id kept. After the save, only the test message can
    fail, and that says so and undoes nothing.
    """
    api = api or SlackApi()
    token = token.strip()
    if not token:
        raise SlackSetupError("no token on standard input")
    if token_problem(token):
        raise SlackSetupError("that is not a Slack token: a bot token starts xoxb-")
    values = _config(settings)
    on_file, server = values["channel_id"], values["mcp_server"]
    before = settings.slack_bot_token or ""
    # An address that was asked for replaces the destination; otherwise one is looked for
    # only when what is on file does not stand for this token.
    address = (email or "").strip()
    kept = on_file if not address and keeps(on_file, new_token=token != before) else ""
    if not (address or kept):
        address = settings.user_google_email or ""
    if not (address or kept):
        raise SlackSetupError(f"{_nowhere(on_file, server)}: add --email ADDRESS, the one "
                              "you use in Slack. Nothing was saved")

    workspace = api.auth_test(token)
    if not workspace.ok:
        why = workspace.problem if workspace.refused else (
            f"the token could not be checked ({workspace.problem})")
        raise SlackSetupError(f"{why}; nothing was saved")
    found = None
    if address:
        found = api.lookup_by_email(token, address)
        if not found.ok:
            raise SlackSetupError(
                f"Slack could not find {address}: {found.problem}. Nothing was saved. Try "
                f"another --email, or by hand: `keryx config set {TOKEN_KEY} --stdin`, "
                f"then {BY_HAND}"
            )
    channel = found.user_id if found is not None else kept

    try:
        store.set({TOKEN_KEY: token}, actor=OWNER, settings=settings)
    except ConfigError as error:
        raise SlackSetupError(str(error)) from None
    echo(f"connected to the {workspace.team or 'Slack'} workspace; {TOKEN_KEY} saved")
    if store.source_of(TOKEN_KEY, settings) == FROM_ENV:
        echo(f"note: {TOKEN_KEY} is also set in the environment, which wins")
    if before and token != before:
        echo(RESTART_NOTE)
    settings = settings.model_copy(update={"slack_bot_token": token})
    if found is not None:
        echo(f"found {found.name or address} ({channel}): messages go to them")
    else:
        echo(f"it keeps sending to {channel}")
    try:
        plugins.write_config(settings, PLUGIN, {"channel_id": channel})
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

    if not test:
        echo("no test message was sent (--no-test), so nothing has shown it arrives")
        return
    sent = api.post_text(token, channel, TEST_MESSAGE)
    if not sent.ok:
        elsewhere = "" if found is not None else (
            ". To send to yourself instead, run it again with --email ADDRESS")
        raise SlackSetupError(
            f"everything is saved, but the test message to {channel} did not go through: "
            f"{sent.problem}{elsewhere}"
        )
    echo("sent a test message: it is in Slack, under Apps")


def _nowhere(on_file: str, server: str) -> str:
    """Why there is no destination for a token, when no address was given to look for."""
    if on_file:
        return (f"the channel on file ({on_file}) is a direct message with the app the old "
                "token came from, and this token cannot post to it")
    if server:
        return (f"a token of its own needs somewhere of its own to send, and the channel so "
                f"far is the {server} MCP server's")
    return "nowhere to send yet"
