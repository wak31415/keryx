"""What every `keryx setup` section is handed: the terminal, the store, and the outside world.

`SetupContext` is the one object a section works through. It asks and says through a
`Prompter`, saves through the store as the owner, and reads a fresh `Settings` after every
save, so a later section sees what an earlier one wrote. Everything that reaches the
network, a login or a subagent is a field of `Probes`, which the tests replace wholesale:
nothing in the wizard can call OpenAI, Twilio, Google, Slack, ssh or a coding agent unless a
probe does.
"""

import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from keryx.agents.base import RunResult
from keryx.config import Settings
from keryx.config.permissions import OWNER
from keryx.config.store import FROM_ENV, ConfigError, ConfigStore
from keryx.integrations import slack
from keryx.issues import GhStatus, gh_status
from keryx.notify.twilio_out import RestTwilioAdmin, TwilioAdmin
from keryx.setup.ui import Prompter

#: The one read OpenAI answers for any working key.
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
PROBE_TIMEOUT_S = 15.0


def openai_key_problem(key: str) -> str | None:
    """Why OpenAI will not take `key`, in a sentence; None when it does. One `GET`."""
    try:
        response = httpx.get(
            OPENAI_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=PROBE_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        return f"could not reach OpenAI ({type(exc).__name__})"
    if response.status_code == 200:
        return None
    if response.status_code == 401:
        return "OpenAI does not recognise that key"
    return f"OpenAI answered HTTP {response.status_code}"


def run_command(argv: Sequence[str]) -> int:
    """Run a command on this terminal — its prompts and its output are the person's."""
    try:
        return subprocess.run(list(argv), check=False).returncode
    except OSError:
        return 127


def _smoke(settings: Settings, agent: str) -> Awaitable[RunResult]:
    from keryx.setup.agents import run_smoke

    return run_smoke(settings, agent)


def _task(settings: Settings, agent: str, prompt: str) -> Awaitable[RunResult]:
    from keryx.setup.agents import run_task

    return run_task(settings, agent, prompt)


def _google_browser_signin(settings: Settings, chosen: Any, announce: Any) -> Any:
    from keryx.setup.google import GoogleSetupError, browser_grant

    try:
        return browser_grant(settings, chosen, announce=announce)
    except GoogleSetupError as error:
        return error
    except KeyboardInterrupt:
        # Ctrl-C while the browser is out stops the wait, not the whole of `keryx setup`.
        return GoogleSetupError("stopped before the browser came back")


def _google_finish(settings: Settings, redirected_to: str) -> Any:
    from keryx.setup.google import GoogleSetupError, finish_grant

    try:
        return finish_grant(settings, redirected_to)
    except GoogleSetupError as error:
        return error


def _google_discard(settings: Settings, grant: Any) -> bool:
    from keryx.setup.google import discard

    return discard(settings, grant)


def _install_service(settings: Settings, echo: Callable[[str], None]) -> None:
    from keryx.setup.service import install

    install(settings, echo=echo)


def _ssh_hosts() -> list:
    from keryx.plugins.ssh_hosts import discover

    return discover()


def _master_alive(alias: str) -> bool:
    from keryx.plugins.ssh_hosts import master_alive

    return master_alive(alias)


def _partitions(alias: str) -> list[str]:
    from keryx.plugins.ssh_hosts import partitions

    return partitions(alias)


def _headless() -> bool:
    from keryx.setup.agents import is_headless

    return is_headless()


@dataclass
class Probes:
    """Everything the wizard does to the outside world. Each is replaced in the tests."""

    openai_key_problem: Callable[[str], str | None] = openai_key_problem
    twilio: Callable[[str, str], TwilioAdmin] = RestTwilioAdmin
    smoke: Callable[[Settings, str], Awaitable[RunResult]] = _smoke
    run_task: Callable[[Settings, str, str], Awaitable[RunResult]] = _task
    run_login: Callable[[Sequence[str]], int] = run_command
    #: `keryx service install`; raises `ServiceError` with what to fix.
    install_service: Callable[[Settings, Callable[[str], None]], None] = _install_service
    headless: Callable[[], bool] = _headless
    #: The Google sign-in with a browser on this machine (`setup.google.browser_grant`:
    #: listener, browser, exchange), and the exchange that ends a pasted-back one. Each
    #: returns a `Grant` that nothing has saved yet, or the `GoogleSetupError` it ended in
    #: *as a value*: a result is recorded and replayed, and a raise would run the sign-in
    #: again on Esc. `google_discard` revokes a grant the owner turned down.
    google_browser_signin: Callable[[Settings, Any, Any], Any] = _google_browser_signin
    google_finish: Callable[[Settings, str], Any] = _google_finish
    google_discard: Callable[[Settings, Any], bool] = _google_discard
    #: `~/.ssh/config`, read locally (`plugins.ssh_hosts`): never a connection.
    ssh_hosts: Callable[[], list] = _ssh_hosts
    ssh_master_alive: Callable[[str], bool] = _master_alive
    cluster_partitions: Callable[[str], list[str]] = _partitions
    #: `gh auth status`, for the issue reports section (`keryx.issues.gh_status`).
    gh_status: Callable[[], GhStatus] = gh_status
    #: The Slack step's calls (`integrations.slack`): is this a token and whose (a bot
    #: token's workspace, a user token's member), the member an address belongs to, one
    #: message to a member or channel, and whether an app-level token opens a connection.
    slack_auth_test: Callable[[str], slack.Workspace] = slack.auth_test
    slack_post: Callable[[str, str, str], slack.SlackResult] = slack.post_text
    slack_connection: Callable[[str], slack.Connection] = slack.open_connection


@dataclass
class SetupContext:
    """The terminal, the store and the outside world, for one run of `keryx setup`."""

    ui: Prompter
    store: ConfigStore
    load: Callable[[], Settings]
    probes: Probes = field(default_factory=Probes)
    #: Walking a section that is already done, to change it: ask again what is set.
    review: bool = False
    #: What a section has already asked the outside world in this walk, by whatever key
    #: it likes. For a step that saves nothing until its end (Slack's): going back and
    #: typing the same token again must not show it to Slack a second time.
    memo: dict = field(default_factory=dict)
    _settings: Settings | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = self.load()
        return self._settings

    def refresh(self) -> Settings:
        self._settings = None
        return self.settings

    def save(self, values: Mapping[str, Any]) -> bool:
        """Store `values` as the owner and say where they went; False (and said) if refused."""
        try:
            stored = self.store.set(values, actor=OWNER, settings=self.settings)
        except ConfigError as error:
            self.ui.error(str(error))
            return False
        self.refresh()
        if written := [key for key, value in stored.items() if value is not None]:
            self.ui.success(f"saved {', '.join(written)}")
        for key in stored:
            self.warn_if_overridden(key)
        return True

    def current(self, key: str) -> str:
        """What `key` is set to now, for a question that offers to keep it; "" when unset."""
        return str(getattr(self.settings, key.lower(), None) or "")

    def warn_if_overridden(self, key: str) -> bool:
        """Say so when the environment sets `key`: it wins over anything saved here."""
        if self.store.source_of(key, self.settings) != FROM_ENV:
            return False
        self.ui.warn(f"{key} is also set in your environment, which wins over the saved value")
        return True
