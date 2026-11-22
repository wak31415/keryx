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
"""

from collections.abc import Callable
from dataclasses import dataclass

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


def start(settings: Settings, echo: Callable[[str], None]) -> None:
    """`keryx auth login slack` with no token: the link, the steps, and the next command."""
    echo("Create your Slack app from this link:\n")
    echo(slack.new_app_url())
    echo("")
    for number, step in enumerate(STEPS, 1):
        echo(f"{number}. {plain(step)}")
    address = settings.user_google_email
    echo(
        "\nThen hand the token over on standard input, never on the command line, with "
        "the address you use in Slack:"
        "\n  keryx auth login slack --stdin --email ADDRESS"
    )
    if address:
        echo(f"Without --email it looks for {address}.")


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
    """Check the token, save it, find where to send, turn the plugin on, and prove it.

    Raises `SlackSetupError` saying how far it got. A token Slack refuses saves nothing;
    after that each step's failure leaves what was saved before it, and says so.
    """
    api = api or SlackApi()
    token = token.strip()
    if not token:
        raise SlackSetupError("no token on standard input")
    if token_problem(token):
        raise SlackSetupError("that is not a Slack token: a bot token starts xoxb-")
    try:
        values = plugins.read_config(settings, PLUGIN)
    except plugins.PluginConfigError as error:
        raise SlackSetupError(str(error)) from None
    channel = values["channel_id"]
    # An address that was asked for replaces the destination; otherwise one is only
    # looked for when there is none.
    address = (email or "").strip() or ("" if channel else settings.user_google_email or "")
    if not (address or channel or values["mcp_server"]):
        raise SlackSetupError(
            "nowhere to send yet, so nothing was saved: add --email ADDRESS, the one you "
            "use in Slack"
        )

    workspace = api.auth_test(token)
    if not workspace.ok:
        raise SlackSetupError(f"{workspace.problem}; nothing was saved")
    try:
        store.set({TOKEN_KEY: token}, actor=OWNER, settings=settings)
    except ConfigError as error:
        raise SlackSetupError(str(error)) from None
    echo(f"connected to the {workspace.team or 'Slack'} workspace; {TOKEN_KEY} saved")
    if store.source_of(TOKEN_KEY, settings) == FROM_ENV:
        echo(f"note: {TOKEN_KEY} is also set in the environment, which wins")
    settings = settings.model_copy(update={"slack_bot_token": token})

    if address:
        member = api.lookup_by_email(token, address)
        if not member.ok:
            raise SlackSetupError(
                f"the token is saved, but Slack could not find {address}: {member.problem}. "
                f"Try another --email, or {BY_HAND}"
            )
        channel = member.user_id
        echo(f"found {member.name or address} ({channel}): messages go to them")
    try:
        plugins.write_config(settings, PLUGIN, {"channel_id": channel})
        plugins.install(settings, PLUGIN)
    except plugins.PluginConfigError as error:
        raise SlackSetupError(f"the token is saved, but {error}") from None
    status = next(one for one in plugins.status(settings) if one.name == PLUGIN)
    if not status.on:
        raise SlackSetupError(f"{PLUGIN} is installed but refused: {status.refused}")
    echo(f"{PLUGIN} is on, from the next call")

    if test and channel:
        sent = api.post_text(token, channel, TEST_MESSAGE)
        if not sent.ok:
            raise SlackSetupError(
                f"everything is saved, but the test message did not go through: {sent.problem}"
            )
        echo("sent a test message: it is in Slack, under Apps")
