"""What every `keryx setup` section is handed: the terminal, the store, and the outside world.

`SetupContext` is the one object a section works through. It asks and says through a
`Prompter`, saves through the store as the owner, and reads a fresh `Settings` after every
save, so a later section sees what an earlier one wrote. Everything that reaches the
network, a login or a subagent is a field of `Probes`, which the tests replace wholesale:
nothing in the wizard can call OpenAI, Twilio, Google, ssh or a coding agent unless a probe
does.
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


def _gmail_address(settings: Settings) -> Awaitable[str]:
    from keryx.setup.google import gmail_address

    return gmail_address(settings)


def _workspace(settings: Settings, echo: Callable[[str], None]) -> bool:
    from keryx.setup.google import run_google_setup

    return run_google_setup(settings, echo=echo)


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
    run_script: Callable[[Sequence[str]], int] = run_command
    headless: Callable[[], bool] = _headless
    gmail_address: Callable[[Settings], Awaitable[str]] = _gmail_address
    workspace_signin: Callable[[Settings, Callable[[str], None]], bool] = _workspace
    http_post: Callable[..., Any] = httpx.post
    #: `~/.ssh/config`, read locally (`plugins.ssh_hosts`): never a connection.
    ssh_hosts: Callable[[], list] = _ssh_hosts
    ssh_master_alive: Callable[[str], bool] = _master_alive
    cluster_partitions: Callable[[str], list[str]] = _partitions
    #: `gh auth status`, for the issue reports section (`keryx.issues.gh_status`).
    gh_status: Callable[[], GhStatus] = gh_status


@dataclass
class SetupContext:
    """The terminal, the store and the outside world, for one run of `keryx setup`."""

    ui: Prompter
    store: ConfigStore
    load: Callable[[], Settings]
    probes: Probes = field(default_factory=Probes)
    #: Walking a section that is already done, to change it: ask again what is set.
    review: bool = False
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
