"""`keryx auth smoke NAME`: the smoke test `auth status --smoke` runs on each agent, for one,
told as it runs — JSON lines for the Mac app (`--json`), lines of text at a terminal.

It is the same test: `setup.agents.run_smoke`, one minimal task through the agent's real
runner, run as the service, passed only by `agents.passed_smoke`. It asks first what the
wizard asks before its own (`agents._smoke_one`): the agent installed, its CLI there, and a
credential to run on — each refused as an `error` before anything runs. Whether the agent is
in `AGENTS_ENABLED` is not asked: the owner named it.

The events: `step` (`dispatch`, then `answering` once the agent first reports anything),
then exactly one of `done` (`ok`, `summary`, and `detail` — what the agent said or why it
could not run, redacted, null when it passed), `error`, `cancelled`. A test that ran and
failed is `done` with `ok` false, exit 1; one that passed exits 0; a refusal is exit 1, an
agent that is not one exit 2, a Cancel 130. Cancel interrupts the task inside its event
loop, whose closing cancels the turn and closes the agent's session before `cancelled` is
told. Nothing is written.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from keryx.agents import registry
from keryx.agents.base import RunResult
from keryx.agents.registry import BACKENDS
from keryx.child_env import redact_known
from keryx.config import Settings
from keryx.localmodels.pull import EventLines
from keryx.setup import agents

#: The most of what the agent said, or why it could not run, a `detail` carries.
MAX_DETAIL_CHARS = 300

DISPATCH = "Asking {name} to run a test task."
ANSWERING = "{name} is answering."
PASSED = "{name} ran a test task and said it's ready."
UNANSWERED = "{name} ran a test task but didn't answer as asked."
UNRUN = "{name} couldn't run a test task."
NOT_INSTALLED = "{name} isn't installed in this copy of Keryx."
NO_CLI = "{name}'s command-line tool is missing."
NOT_SIGNED_IN = "{name} isn't signed in yet."
NO_SERVER = "The local model has no server to run on yet."
UNKNOWN = "There's no coding agent called {name}."


class SmokeEvents(EventLines):
    """For the Mac app: `step` (`step`, `message`, `detail`), then exactly one of `done`
    (`ok`, `summary`, `detail`), `error`, `cancelled`."""

    def __init__(self, write: Callable[[str], None], **kwargs: Any) -> None:
        super().__init__(write, **kwargs)
        self.passed = False

    def step(self, step: str, message: str, *, detail: str | None = None) -> None:
        self._emit({"event": "step", "step": step, "message": message, "detail": detail})

    def done(self, ok: bool, summary: str, *, detail: str | None = None) -> None:
        self.passed = ok
        self._end({"event": "done", "ok": ok, "summary": summary, "detail": detail})

    def guard(self, run: Callable[[], int]) -> int:
        """`EventLines.guard`, but a Cancel landing just after a `done` that failed still
        exits 1, as that ending says."""
        code = super().guard(run)
        return 1 if code == 0 and self.ending == "done" and not self.passed else code


class SmokeText(SmokeEvents):
    """The same events at a terminal: each step's message, then the summary and its detail;
    a refusal to `write_error`."""

    def __init__(self, write: Callable[[str], None], write_error: Callable[[str], None],
                 **kwargs: Any) -> None:
        super().__init__(write, **kwargs)
        self._write_error = write_error

    def _emit(self, event: dict[str, Any]) -> None:
        write = self._write_error if event["event"] in ("error", "cancelled") else self._write
        if event["event"] == "cancelled":
            write("stopped")
        for key in ("message", "summary", "detail"):
            if event.get(key):
                write(event[key])


def _say(template: str, name: str) -> str:
    """`template` about the agent spoken of as `name`, its first letter a capital: "The
    local model ran…", but "Asking the local model…"."""
    text = template.format(name=name)
    return text[:1].upper() + text[1:]


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL_CHARS else text[: MAX_DETAIL_CHARS - 1] + "…"


def stream(settings: Settings, name: str, events: SmokeEvents) -> int:
    """The smoke test on the agent `name`, told to `events`; the exit code (see above)."""
    if name not in BACKENDS:
        events.error(UNKNOWN.format(name=name), detail=f"one of: {', '.join(BACKENDS)}")
        return 2
    spec = BACKENDS[name]
    spoken = spec.spoken_name
    if not registry.installed(name, settings):
        events.error(_say(NOT_INSTALLED, spoken),
                     detail=registry.install_command(name, settings))
        return 1
    if registry.cli_path(name, settings) is None:
        events.error(_say(NO_CLI, spoken), detail=spec.install_hint)
        return 1
    auth = registry.auth_status(name, settings)
    if not auth.ready:
        message = NO_SERVER if spec.harness is not None else _say(NOT_SIGNED_IN, spoken)
        events.error(message, detail=redact_known(auth.detail, settings) or None)
        return 1
    answering = False

    def progress(_text: str) -> None:
        nonlocal answering
        if not answering:
            answering = True
            events.step("answering", _say(ANSWERING, spoken))

    events.step("dispatch", _say(DISPATCH, spoken))
    result: RunResult = asyncio.run(agents.run_smoke(settings, name, on_progress=progress))
    if agents.passed_smoke(result):
        events.done(True, _say(PASSED, spoken))
        return 0
    if result.ok:
        answer = result.spoken_summary or result.final_text or "nothing"
        events.done(False, _say(UNANSWERED, spoken),
                    detail=_clip(f"It said: {redact_known(answer, settings)}"))
    else:
        events.done(False, _say(UNRUN, spoken),
                    detail=_clip(redact_known(result.error or "no answer", settings)))
    return 1
