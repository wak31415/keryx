"""What every coding-agent backend shares.

A backend is an `AgentRunner` whose `open()` hands back an `AgentSession`; everything
above that seam — the task manager, the notifier, the voice tools — sees only a
`RunResult`. So the protocol, the result, and the text protocol a subagent speaks back
(`SPOKEN_SUMMARY:` and `RESTART_REQUIRED:`) live here once, along with the context every
subagent is handed whichever agent runs it: the rendered system-prompt suffix, the
directory it starts in, and the `workspace-mcp` server that gives it Gmail and Calendar.

`FakeAgentRunner` is the scripted stand-in used by tests and by `--fake-agents`.

Every subagent is told (via `prompts/subagent_suffix.md`) to end its final message with a
`SPOKEN_SUMMARY:` line; `extract_spoken_summary` turns that into the sentence the voice
session reads out, falling back to the last paragraph when the agent forgot.
"""

import asyncio
import inspect
import logging
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jarvis.config import OWNER_FALLBACK, Settings, secure_dir
from jarvis.issues import IssueReporting
from jarvis.prompts import render_prompt
from jarvis.skills import CUSTOM_TOOLS_SKILL
from jarvis.tasks.models import Task

log = logging.getLogger("jarvis.agents")

SUBAGENT_SUFFIX_PROMPT = "subagent_suffix.md"
#: Spliced into the suffix only when a Slack MCP server is configured.
SUBAGENT_SLACK_PROMPT = "subagent_slack.md"
SUBAGENT_CUSTOM_TOOLS_PROMPT = "subagent_custom_tools.md"
#: Spliced in only when there is a repository to file on (`jarvis.issues`).
SUBAGENT_ISSUES_PROMPT = "subagent_issues.md"

SPOKEN_SUMMARY_MARKER = "SPOKEN_SUMMARY:"
#: How a subagent says "I changed Jarvis's own code, and only a restart loads it". The
#: subagent is the only thing that actually knows — `git describe --dirty` flips on any
#: edit anyone has open, and the voice model can only guess from a spoken summary — so it
#: declares it rather than being detected. Read before `SPOKEN_SUMMARY:`, so it never ends
#: up in what gets read out loud.
RESTART_MARKER = "RESTART_REQUIRED:"
MAX_SUMMARY_CHARS = 400
#: How much of the subagent's own "why" is kept as the restart's reason.
MAX_RESTART_REASON_CHARS = 120
NO_SUMMARY = "The task finished, but no summary was produced."

GOOGLE_MCP_ARGS = [
    "workspace-mcp",
    "--tools",
    "gmail",
    "calendar",
    "--transport",
    "stdio",
    "--single-user",
]
GOOGLE_OAUTH_REDIRECT_URI = "http://localhost:8000/oauth2callback"

_MAX_ERROR_CHARS = 160

# A `SPOKEN_SUMMARY:` line, tolerating the markdown the model wraps it in
# (`## SPOKEN_SUMMARY:`, `**SPOKEN_SUMMARY:**`, `- SPOKEN_SUMMARY:`).
_MARKER_RE = re.compile(
    rf"^[ \t]*(?:[#>*_\-+][ \t]*)*{re.escape(SPOKEN_SUMMARY_MARKER)}",
    re.MULTILINE,
)
_RESTART_RE = re.compile(
    rf"^[ \t]*(?:[#>*_\-+][ \t]*)*{re.escape(RESTART_MARKER)}(?P<why>.*)$",
    re.MULTILINE,
)
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+•]|\d+[.)])[ \t]+", re.MULTILINE)
_HEADING_RE = re.compile(r"^[ \t]*#+[ \t]*", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"[`*_]+")
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True)
class TokenUsage:
    """What one or more turns cost in tokens, the same way for every agent.

    `input_tokens` counts every input token, cached ones included, and `cached_input_tokens`
    says how many of those were cache reads — Codex's meaning, and Claude's once its
    uncached, cache-write and cache-read counts are summed.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass
class RunResult:
    """The outcome of one subagent turn."""

    ok: bool
    final_text: str = ""
    spoken_summary: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    error: str | None = None
    #: The subagent's own `RESTART_REQUIRED:` line, or None when it did not ask for one.
    #: Empty string means it asked without saying why, which is still asking.
    restart_reason: str | None = None
    #: Tokens the turn spent, when the agent said; None when it did not.
    usage: TokenUsage | None = None


class AgentOpenError(RuntimeError):
    """A session could not be opened; the message has every credential redacted."""


class SteerUnavailable(NotImplementedError):
    """`send()` could not put the text into a running turn, and nothing was delivered.

    The agent has no live steer at all, or the turn has already ended. Either way the text
    was refused, never half-accepted, so the caller may safely deliver it another way.
    """


class AgentSession(Protocol):
    """One live subagent conversation."""

    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult:
        """Run one turn to completion. Never raises: failures come back as `ok=False`."""
        ...

    async def send(self, text: str) -> None:
        """Put a follow-up into the turn that is running now.

        `SteerUnavailable` means it was refused and nothing was delivered — the agent has
        no live steer (Claude), or its turn has just ended — and the task manager re-runs
        with the text instead. Any other exception is a steer that may have landed.
        """
        ...

    async def interrupt(self) -> None:
        """Ask the agent to stop the current turn."""
        ...

    async def close(self) -> None:
        """End the conversation and release the process."""
        ...


class AgentRunner(Protocol):
    """Opens subagent conversations."""

    async def open(self, task: Task, *, resume: str | None = None) -> AgentSession:
        """Start (or resume, given a Claude session id) a conversation for `task`."""
        ...


# --------------------------------------------------------------------------- summaries


def _clean(text: str) -> str:
    """Spoken-safe text: no bullets, headings, emphasis or backticks, whitespace collapsed."""
    without_markup = _EMPHASIS_RE.sub("", _HEADING_RE.sub("", _BULLET_RE.sub("", text)))
    return " ".join(without_markup.split())


def _truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """`text` shortened to `limit` characters at a word boundary, with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


def _last_paragraph(text: str) -> str:
    """The last non-empty paragraph of `text`, cleaned; empty when there is none."""
    for paragraph in reversed(_PARAGRAPH_RE.split(text)):
        cleaned = _clean(paragraph)
        if cleaned:
            return cleaned
    return ""


def extract_spoken_summary(text: str) -> str:
    """The sentence(s) to read out loud for a finished task.

    Prefers the text after the last `SPOKEN_SUMMARY:` marker; falls back to the last
    paragraph of the report. Always plain, collapsed, at most `MAX_SUMMARY_CHARS` long.
    """
    text = text or ""
    markers = list(_MARKER_RE.finditer(text))
    summary = _clean(text[markers[-1].end() :]) if markers else ""
    if not summary:
        # An empty (or missing) marked block: fall back to the report above it.
        summary = _last_paragraph(text[: markers[-1].start()] if markers else text)
    return _truncate(summary) if summary else NO_SUMMARY


def extract_restart_request(text: str) -> str | None:
    """The subagent's `RESTART_REQUIRED:` reason, or None when it did not ask for one.

    A restart takes Jarvis off the air and kills the phone call, so this only ever reads
    an explicit line. Anything that merely looks like one — the words in a report, a
    changed checkout — is not it.
    """
    match = _RESTART_RE.search(text or "")
    if match is None:
        return None
    return _truncate(_clean(match.group("why")), MAX_RESTART_REASON_CHARS)


def failure_summary(error: str | None) -> str:
    """A spoken line for a task that blew up before it could summarise itself."""
    detail = _clean(error or "")
    if not detail:
        return "The task failed."
    return _truncate(f"The task failed: {_truncate(detail, _MAX_ERROR_CHARS)}")



# ------------------------------------------------------------------------ agent context


def render_subagent_suffix(
    task: Task,
    *,
    slack_mcp_server: str | None = None,
    owner: str | None = None,
    tools_dir: Path | None = None,
    issues: IssueReporting | None = None,
) -> str:
    """The subagent system-prompt suffix, with this task's project, request and number.

    The number is in there for the commit trailer: it is what ties a change in a repo back
    to the sentence they said out loud, which is the one thing `git log` cannot recover. The
    Slack paragraph is there only when `slack_mcp_server` names a route to use. `owner` is
    whom the work is for (`Settings.owner_label`); `OWNER_FALLBACK` when it is not given.
    `tools_dir` is where the owner's own voice tools live (`jarvis.tools.custom`), and the
    section on writing one is there only when it is given. `issues` is where a report of a
    problem with Jarvis goes (`jarvis.issues`), and that section is there only when given.
    """
    slack = (
        render_prompt(SUBAGENT_SLACK_PROMPT, server=slack_mcp_server).strip()
        if slack_mcp_server
        else ""
    )
    custom_tools = (
        render_prompt(
            SUBAGENT_CUSTOM_TOOLS_PROMPT,
            tools_dir=str(tools_dir),
            skill=str(CUSTOM_TOOLS_SKILL),
            check=f"{sys.executable} -m jarvis tools",
        ).strip()
        if tools_dir is not None
        else ""
    )
    return render_prompt(
        SUBAGENT_SUFFIX_PROMPT,
        owner=owner or OWNER_FALLBACK,
        project=task.project or "none",
        description=task.description,
        task_id=str(task.id) if task.id is not None else "unknown",
        slack=slack,
        custom_tools=custom_tools,
        issues=_issues_section(task, issues),
    )


def _issues_section(task: Task, issues: IssueReporting | None) -> str:
    """How to report a problem with Jarvis, with the call it came from when there is one."""
    if issues is None:
        return ""
    transcript = issues.transcript(task.origin_session_id)
    call = (
        f" The call they asked from is `{transcript}`: read it to see what happened, and "
        "never quote it."
        if transcript is not None
        else ""
    )
    return render_prompt(
        SUBAGENT_ISSUES_PROMPT,
        repo=issues.repo,
        skill=str(issues.skill),
        checkout=str(issues.checkout),
        logs=str(issues.logs),
        jarvis=f"{sys.executable} -m jarvis",
        call=call,
    ).strip()


def google_mcp_server_config(settings: Settings) -> dict[str, Any]:
    """The `workspace-mcp` stdio server config for Gmail + Calendar.

    Public because `jarvis auth login google-workspace` runs the very same server once, by
    hand, to walk through the browser OAuth flow that leaves credentials behind for later
    tasks.
    """
    client = settings.google_oauth_client()
    env = {
        name: value
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", client[0] if client else None),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client[1] if client else None),
            ("USER_GOOGLE_EMAIL", settings.user_google_email),
        )
        if value
    }
    env["GOOGLE_OAUTH_REDIRECT_URI"] = GOOGLE_OAUTH_REDIRECT_URI
    env["GOOGLE_MCP_CREDENTIALS_DIR"] = str(settings.data_dir / "google")
    env["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    return {"type": "stdio", "command": "uvx", "args": list(GOOGLE_MCP_ARGS), "env": env}


def workspace_dir(task: Task, settings: Settings) -> Path:
    """Where the subagent runs: the task's own directory, else a shared workspace.

    The task's directory is never created. It is a project checkout or the projects root,
    both of which are somebody's folders to make, and one that is not there — a root this
    machine never had, a checkout deleted since the task was queued — means the workspace
    under `data_dir`, owner-only like everything else there.
    """
    if task.cwd:
        cwd = Path(task.cwd)
        if cwd.is_dir():
            return cwd
        log.warning("task %s: %s is not a directory; starting in the workspace", task.id, cwd)
    return secure_dir(settings.data_dir / "workspace")



# ---------------------------------------------------------------------------- progress


async def emit_progress(on_progress: Callable[[str], Any], text: str) -> None:
    """Report one progress line; a sync or async callback, whose failures are ignored."""
    try:
        outcome = on_progress(text)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        log.exception("progress callback failed")

# ------------------------------------------------------------------------- fake runner

DEFAULT_FAKE_RESULT = RunResult(
    ok=True,
    final_text="Done.\n\nSPOKEN_SUMMARY: I finished the task.",
    spoken_summary="I finished the task.",
    session_id="fake-session-1",
    cost_usd=0.01,
    error=None,
)

FakeScript = Callable[[Task, str | None], RunResult]

#: What an interrupted turn comes back as, for `FakeAgentRunner(interrupt_ends_run=True)`.
INTERRUPTED_RESULT = RunResult(
    ok=False,
    final_text="",
    spoken_summary="The task failed: interrupted",
    session_id="fake-session-1",
    error="interrupted",
)

#: How long `interrupt()` takes to settle in `interrupt_ends_run` mode — long enough for
#: the caller to have fully processed the ended turn, the way a real control round-trip is.
INTERRUPT_SETTLE_S = 0.05


class FakeAgentSession(AgentSession):
    """A scripted conversation that records everything it was asked to do."""

    def __init__(self, runner: "FakeAgentRunner", task: Task, resume: str | None) -> None:
        self.task = task
        self.resume = resume
        self.prompts: list[str] = []
        self.sent: list[str] = []
        self.interrupts = 0
        self.closed = False
        self._runner = runner
        self._interrupted = asyncio.Event()

    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult:
        """Sleep, emit the scripted progress lines, return the next scripted result.

        Under `interrupt_ends_run`, an `interrupt()` during the sleep ends the turn early
        with `INTERRUPTED_RESULT` — what the real session does, since interrupting is a
        control round-trip that makes `run()` return rather than raise.
        """
        self.prompts.append(prompt)
        if self._runner.interrupt_ends_run:
            try:
                await asyncio.wait_for(self._interrupted.wait(), self._runner.delay_s)
            except TimeoutError:
                pass
            else:
                return INTERRUPTED_RESULT
        else:
            await asyncio.sleep(self._runner.delay_s)
        for line in self._runner.progress:
            await emit_progress(on_progress, line)
        return self._runner.next_result(self.task, self.resume)

    async def send(self, text: str) -> None:
        """Steer when the runner says the agent can; refuse as one that cannot, otherwise."""
        steer = self._runner.steer
        if isinstance(steer, BaseException):
            raise steer
        if not steer:
            raise SteerUnavailable("this fake agent has no live steer")
        self.sent.append(text)

    async def interrupt(self) -> None:
        self.interrupts += 1
        self._interrupted.set()
        if self._runner.interrupt_ends_run:
            await asyncio.sleep(INTERRUPT_SETTLE_S)

    async def close(self) -> None:
        self.closed = True


class FakeAgentRunner(AgentRunner):
    """Scripted `AgentRunner` for tests and the CLI's `--fake-agents` dev flag.

    `results` is a list popped from the front (the last one repeats), a callable taking
    `(task, resume)`, or `None` for `DEFAULT_FAKE_RESULT` every time. `interrupt_ends_run`
    opts into the real session's interrupt semantics (see `FakeAgentSession.run`); it is
    off by default, so an `interrupt()` merely gets counted. `steer` is what `send()` does:
    False refuses with `SteerUnavailable` (Claude), True takes the text (Codex), and an
    exception is raised as a steer that failed for real.
    """

    def __init__(
        self,
        results: Sequence[RunResult] | FakeScript | None = None,
        *,
        delay_s: float = 0.0,
        progress: Iterable[str] | None = None,
        interrupt_ends_run: bool = False,
        steer: bool | BaseException = False,
    ) -> None:
        self.delay_s = delay_s
        self.steer = steer
        self.progress = list(progress or ())
        self.interrupt_ends_run = interrupt_ends_run
        self.opened: list[tuple[Task, str | None]] = []
        self.sessions: list[FakeAgentSession] = []
        self._script: FakeScript | None = results if callable(results) else None
        self._pending: list[RunResult] = [] if callable(results) else list(results or ())

    async def open(self, task: Task, *, resume: str | None = None) -> FakeAgentSession:
        """Record the call and hand back a fresh scripted session."""
        self.opened.append((task, resume))
        session = FakeAgentSession(self, task, resume)
        self.sessions.append(session)
        return session

    def next_result(self, task: Task, resume: str | None) -> RunResult:
        """The result for the next `run()`: from the script, the queue, or the default."""
        if self._script is not None:
            return self._script(task, resume)
        if not self._pending:
            return DEFAULT_FAKE_RESULT
        if len(self._pending) == 1:
            return self._pending[0]
        return self._pending.pop(0)
