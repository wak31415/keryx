"""A scripted terminal and a fake outside world for `jarvis setup`.

`ScriptedPrompter` answers each question from a list of `(fragment, answer)` pairs, taken
in order: the question's text must contain the fragment, so a test that drifts out of step
with the wizard fails at the question that moved rather than three questions later. An
answer that is an exception is raised instead (`Aborted`, for ctrl-c). Everything the
wizard says is kept in `said`, one `(kind, text)` per line.
"""

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jarvis.agents.base import RunResult
from jarvis.issues import GhStatus
from jarvis.notify.twilio_out import TwilioError, TwilioNumber
from jarvis.setup.context import Probes
from jarvis.setup.ui import Choice


class ScriptedPrompter:
    def __init__(self, answers: Sequence[tuple[str, Any]] = ()) -> None:
        self.answers = list(answers)
        self.said: list[tuple[str, str]] = []
        self.asked: list[tuple[str, str]] = []
        self.choices: dict[str, list[Choice]] = {}
        #: What each secret question offered to keep.
        self.currents: dict[str, str] = {}

    # --- saying ----------------------------------------------------------------------

    def _say(self, kind: str, text: str) -> None:
        self.said.append((kind, text))

    def intro(self, title: str, subtitle: str = "") -> None:
        self._say("intro", f"{title} {subtitle}")

    def section(self, title: str, step=None) -> None:
        self._say("section", title)

    def note(self, text: str) -> None:
        self._say("note", text)

    def success(self, text: str) -> None:
        self._say("success", text)

    def warn(self, text: str) -> None:
        self._say("warn", text)

    def error(self, text: str) -> None:
        self._say("error", text)

    def markdown(self, text: str) -> None:
        self._say("markdown", text)

    def panel(self, title: str, body: str) -> None:
        self._say("panel", f"{title}\n{body}")

    def table(self, headers, rows) -> None:
        for row in rows:
            self._say("table", " | ".join(row))

    def outro(self, text: str) -> None:
        self._say("outro", text)

    @contextlib.contextmanager
    def spinner(self, message: str):
        self._say("spinner", message)
        yield

    # --- asking ----------------------------------------------------------------------

    def _answer(self, kind: str, message: str) -> Any:
        self.asked.append((kind, message))
        if not self.answers:
            raise AssertionError(f"unscripted {kind}: {message!r}")
        fragment, answer = self.answers.pop(0)
        if fragment not in message:
            raise AssertionError(f"expected a question with {fragment!r}, got {kind}: {message!r}")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def select(self, message, choices, *, default=None) -> str:
        self.choices[message] = list(choices)
        answer = self._answer("select", message)
        if answer is DEFAULT and default is None:
            raise AssertionError(f"{message!r} has no default to take")
        return default if answer is DEFAULT else answer

    def checkbox(self, message, choices) -> list[str]:
        self.choices[message] = list(choices)
        answer = self._answer("checkbox", message)
        if answer is DEFAULT:
            return [choice.value for choice in choices if choice.checked]
        return answer

    def text(self, message, *, default="", validate=None, multiline=False) -> str:
        answer = self._answer("text", message)
        answer = default if answer is DEFAULT else answer
        if validate is not None and (problem := validate(answer)):
            raise AssertionError(f"{message!r} refused {answer!r}: {problem}")
        return answer

    def secret(self, message, *, validate=None, current="") -> str:
        self.currents[message] = current
        answer = self._answer("secret", message)
        if answer is DEFAULT:
            if not current:
                raise AssertionError(f"{message!r} has nothing to keep")
            return current
        if validate is not None and (problem := validate(answer)):
            raise AssertionError(f"{message!r} refused a secret: {problem}")
        return answer

    def confirm(self, message, *, default=True) -> bool:
        answer = self._answer("confirm", message)
        return default if answer is DEFAULT else answer

    # --- reading back ----------------------------------------------------------------

    def lines(self, kind: str | None = None) -> list[str]:
        return [text for said, text in self.said if kind is None or said == kind]

    def done(self) -> bool:
        return not self.answers


#: Take whatever the question offers by default.
DEFAULT = object()


@dataclass
class FakeTwilioAdmin:
    numbers_: list[TwilioNumber] = field(
        default_factory=lambda: [TwilioNumber("PN1", "+15550001111", None, None)]
    )
    refuse: bool = False
    updates: list[tuple[str, str, str]] = field(default_factory=list)

    def account_name(self) -> str:
        if self.refuse:
            raise TwilioError("HTTP 401: Authenticate")
        return "My account"

    def numbers(self) -> list[TwilioNumber]:
        return list(self.numbers_)

    def set_webhooks(self, number_sid: str, *, voice_url: str, status_url: str) -> None:
        self.updates.append((number_sid, voice_url, status_url))


@dataclass
class FakeWorld:
    """Every probe, recording what it was asked. Replace fields per test."""

    openai_problem: str | None = None
    twilio_admin: FakeTwilioAdmin = field(default_factory=FakeTwilioAdmin)
    smoke_result: RunResult = field(
        default_factory=lambda: RunResult(ok=True, spoken_summary="ready")
    )
    task_result: RunResult = field(default_factory=lambda: RunResult(ok=True))
    login_code: int = 0
    script_code: int = 0
    headless: bool = False
    gmail: str = "sam@example.com"
    #: `~/.ssh/config` as `plugins.ssh_hosts.discover` would read it, whose masters are up
    #: now, and what Slurm would say its partitions are.
    ssh_hosts: list = field(default_factory=list)
    masters_up: set = field(default_factory=set)
    partitions: dict = field(default_factory=dict)
    #: What `gh auth status` says, in order: the last one repeats.
    gh: list = field(default_factory=lambda: [GhStatus(installed=True, signed_in=True,
                                                     account="octocat")])
    calls: list[tuple] = field(default_factory=list)

    def probes(self) -> Probes:
        async def smoke(settings, agent):
            self.calls.append(("smoke", agent))
            return self.smoke_result

        async def run_task(settings, agent, prompt):
            self.calls.append(("task", agent, prompt))
            return self.task_result

        async def gmail_address(settings):
            return self.gmail

        def workspace(settings, echo):
            self.calls.append(("workspace",))
            return True

        def record(kind, code):
            def run(argv):
                self.calls.append((kind, list(argv)))
                return code

            return run

        def post(*args, **kwargs):
            raise AssertionError("no token exchange in this test")

        def gh_status():
            self.calls.append(("gh",))
            return self.gh.pop(0) if len(self.gh) > 1 else self.gh[0]

        def openai(key):
            self.calls.append(("openai", key))
            return self.openai_problem

        return Probes(
            openai_key_problem=openai,
            twilio=lambda sid, token: self.calls.append(("twilio", sid)) or self.twilio_admin,
            smoke=smoke,
            run_task=run_task,
            run_login=record("login", self.login_code),
            run_script=record("script", self.script_code),
            headless=lambda: self.headless,
            gmail_address=gmail_address,
            workspace_signin=workspace,
            http_post=post,
            ssh_hosts=lambda: self.calls.append(("ssh_hosts",)) or list(self.ssh_hosts),
            ssh_master_alive=lambda alias: self.calls.append(("master", alias))
            or alias in self.masters_up,
            cluster_partitions=lambda alias: self.calls.append(("partitions", alias))
            or list(self.partitions.get(alias, [])),
            gh_status=gh_status,
        )
