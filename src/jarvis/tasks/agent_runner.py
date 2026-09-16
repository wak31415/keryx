"""Claude Agent SDK subagent runner (spec §3.2 `tasks/agent_runner.py`).

`ClaudeAgentRunner.open()` builds `ClaudeAgentOptions` for one task and connects a
`ClaudeSDKClient`; the returned `ClaudeAgentSession` drives one live conversation —
`run()` streams a turn to completion, `send()` queues a follow-up into it, `interrupt()`
and `close()` end it. The SDK client is injected (`client_factory`) so tests never start
the `claude` CLI. `FakeAgentRunner` is the scripted stand-in used by tests and by the
CLI's `--fake-agents` dev flag.

Tool restriction per task kind goes through `ClaudeAgentOptions.tools` — the base set of
built-in tools the subagent has at all. Do **not** use `allowed_tools` for that: it is an
auto-approve list, and under `permission_mode="bypassPermissions"` everything is approved
anyway, so a `chat` subagent listed there would still have Bash, Edit and Write. The one
`allowed_tools` entry we keep is the `mcp__google__*` wildcard for Google, because MCP
tools are not built-ins and `tools` cannot express them.

Every subagent is told (via `prompts/subagent_suffix.md`) to end its final message with a
`SPOKEN_SUMMARY:` line; `extract_spoken_summary` turns that into the sentence the voice
session reads out, falling back to the last paragraph when the agent forgot.
"""

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from jarvis.config import OWNER_FALLBACK, Settings
from jarvis.prompts import render_prompt
from jarvis.tasks.models import Task

log = logging.getLogger("jarvis.tasks.agent_runner")

SUBAGENT_SUFFIX_PROMPT = "subagent_suffix.md"
#: Spliced into the suffix only when a Slack MCP server is configured.
SUBAGENT_SLACK_PROMPT = "subagent_slack.md"

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

MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "fable": "claude-fable-5",
    "haiku": "claude-haiku-4-5-20251001",
}

# Gmail and Calendar reach the subagents through the Claude CLI's own claude.ai
# connectors, which are already authorized; `workspace-mcp` is kept but off by default
# (`GOOGLE_WORKSPACE_MCP`), because attaching an unauthorized second Google path only gave
# subagents a tool that fails.
#
# No `tools` key is ever set: a subagent keeps every built-in tool, including the skills
# and the subagents of its own that a real request tends to need. (`tools` is the only
# option that would restrict — `allowed_tools` merely auto-approves, a no-op under
# `bypassPermissions` — and per-kind restriction went away with the kinds on 2026-08-24.)
#
# MCP tools are not built-ins, so `tools` could not filter them anyway; this wildcard
# auto-approves the google server's.
GOOGLE_MCP_TOOLS = ["mcp__google__*"]

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

#: The largest single message the SDK will accept from the `claude` CLI. The default
#: (1 MiB) is smaller than the echo of one `Read` of a screenshot or figure, which
#: arrives as base64 in a single NDJSON line and killed the turn (tasks 68-98).
SUBAGENT_MAX_BUFFER_BYTES = 64 * 1024 * 1024

_MAX_TOOL_INPUT_CHARS = 200
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


@dataclass
class RunResult:
    """The outcome of one subagent turn (spec §3.2)."""

    ok: bool
    final_text: str = ""
    spoken_summary: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    error: str | None = None
    #: The subagent's own `RESTART_REQUIRED:` line, or None when it did not ask for one.
    #: Empty string means it asked without saying why, which is still asking.
    restart_reason: str | None = None


class AgentSession(Protocol):
    """One live subagent conversation."""

    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult:
        """Run one turn to completion. Never raises: failures come back as `ok=False`."""
        ...

    async def send(self, text: str) -> None:
        """Queue a follow-up into the running conversation.

        Part of the protocol, but the task manager never calls it: `run()` returns at the
        first result and the SDK's mid-turn `query()` semantics are unverified, so live
        follow-ups become a resumed run instead (spec §3.3 ruling).
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


class SdkClient(Protocol):
    """The slice of `ClaudeSDKClient` this module uses (injectable for tests)."""

    async def connect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> Any: ...
    async def interrupt(self) -> None: ...
    async def disconnect(self) -> None: ...


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


def _failure_summary(error: str | None) -> str:
    """A spoken line for a task that blew up before it could summarise itself."""
    detail = _clean(error or "")
    if not detail:
        return "The task failed."
    return _truncate(f"The task failed: {_truncate(detail, _MAX_ERROR_CHARS)}")


# ------------------------------------------------------------------------ agent options


def resolve_model(name: str | None, settings: Settings) -> str:
    """A spoken alias (`opus`, `sonnet`, …) or full model id as the id to run."""
    alias = (name or "").strip()
    if not alias:
        return settings.subagent_model
    return MODEL_ALIASES.get(alias.lower(), alias)


def render_subagent_suffix(
    task: Task, *, slack_mcp_server: str | None = None, owner: str | None = None
) -> str:
    """The subagent system-prompt suffix, with this task's project, request and number.

    The number is in there for the commit trailer: it is what ties a change in a repo back
    to the sentence he said out loud, which is the one thing `git log` cannot recover. The
    Slack paragraph is there only when `slack_mcp_server` names a route to use. `owner` is
    whom the work is for (`Settings.owner_label`); `OWNER_FALLBACK` when it is not given.
    """
    slack = (
        render_prompt(SUBAGENT_SLACK_PROMPT, server=slack_mcp_server).strip()
        if slack_mcp_server
        else ""
    )
    return render_prompt(
        SUBAGENT_SUFFIX_PROMPT,
        owner=owner or OWNER_FALLBACK,
        project=task.project or "none",
        description=task.description,
        task_id=str(task.id) if task.id is not None else "unknown",
        slack=slack,
    )


def google_mcp_server_config(settings: Settings) -> dict[str, Any]:
    """The `workspace-mcp` stdio server config for Gmail + Calendar (spec §4).

    Public because `jarvis setup-google` runs the very same server once, by hand, to walk
    through the browser OAuth flow that leaves credentials behind for later tasks.
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


def _workspace_dir(task: Task, settings: Settings) -> Path:
    """Where the subagent runs: the task's project checkout, else a shared workspace."""
    cwd = Path(task.cwd) if task.cwd else settings.data_dir / "workspace"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def build_options(
    task: Task, settings: Settings, *, resume: str | None = None
) -> ClaudeAgentOptions:
    """The Agent SDK options for one task: permissions, tools, model, prompt suffix."""
    options: dict[str, Any] = {
        "permission_mode": "bypassPermissions",
        "cwd": str(_workspace_dir(task, settings)),
        "setting_sources": ["user", "project"],
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": render_subagent_suffix(
                task, slack_mcp_server=settings.slack_mcp_server, owner=settings.owner_label
            ),
        },
        "model": resolve_model(task.model, settings),
        "max_turns": settings.subagent_max_turns,
        "max_budget_usd": settings.subagent_max_budget_usd,
        "resume": resume,
        "max_buffer_size": SUBAGENT_MAX_BUFFER_BYTES,
    }
    if settings.google_workspace_mcp:
        options["mcp_servers"] = {"google": google_mcp_server_config(settings)}
        options["allowed_tools"] = list(GOOGLE_MCP_TOOLS)
    if settings.anthropic_api_key:
        options["env"] = {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    elif settings.claude_code_oauth_token:
        options["env"] = {"CLAUDE_CODE_OAUTH_TOKEN": settings.claude_code_oauth_token}
    # With neither set, the spawned CLI falls back to the user's stored Claude
    # subscription login — the default, so subagents don't bill per token.
    return ClaudeAgentOptions(**options)


# ---------------------------------------------------------------------------- progress


async def _emit(on_progress: Callable[[str], Any], text: str) -> None:
    """Report one progress line; a sync or async callback, whose failures are ignored."""
    try:
        outcome = on_progress(text)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        log.exception("progress callback failed")


def _tool_line(block: ToolUseBlock) -> str:
    """A short, loggable one-liner for a tool call."""
    try:
        arguments = json.dumps(block.input, default=str)
    except (TypeError, ValueError):  # pragma: no cover - json.dumps(default=str) rarely fails
        arguments = str(block.input)
    return f"[tool] {block.name} {arguments[:_MAX_TOOL_INPUT_CHARS]}"


# ------------------------------------------------------------------------- real runner


class ClaudeAgentSession(AgentSession):
    """One `ClaudeSDKClient` conversation, driven message by message."""

    def __init__(self, client: SdkClient) -> None:
        self._client = client

    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult:
        """Send `prompt` and consume the reply stream until the agent's turn is done."""
        texts: list[str] = []
        result_text = ""
        session_id: str | None = None
        cost_usd: float | None = None
        error: str | None = None
        ok = True
        try:
            await self._client.query(prompt)
            async for message in self._client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text = block.text.strip()
                            if not text:
                                continue
                            texts.append(text)
                            await _emit(on_progress, text)
                        elif isinstance(block, ToolUseBlock):
                            await _emit(on_progress, _tool_line(block))
                elif isinstance(message, ResultMessage):
                    session_id = message.session_id
                    cost_usd = message.total_cost_usd
                    result_text = (message.result or "").strip()
                    if message.is_error:
                        ok = False
                        error = result_text or message.subtype
        except Exception as exc:
            log.exception("subagent turn failed")
            final_text = result_text or "\n\n".join(texts)
            return RunResult(
                ok=False,
                final_text=final_text,
                spoken_summary=_failure_summary(str(exc)),
                session_id=session_id,
                cost_usd=cost_usd,
                error=f"{type(exc).__name__}: {exc}",
            )

        final_text = result_text or (texts[-1] if texts else "")
        restart_reason = extract_restart_request(final_text) if ok else None
        if ok or final_text:
            spoken_summary = extract_spoken_summary(final_text)
        else:  # errored with nothing to summarise
            spoken_summary = _failure_summary(error)
        return RunResult(
            ok=ok,
            final_text=final_text,
            spoken_summary=spoken_summary,
            session_id=session_id,
            cost_usd=cost_usd,
            error=error,
            restart_reason=restart_reason,
        )

    async def send(self, text: str) -> None:
        """Queue a follow-up message into the live conversation."""
        await self._client.query(text)

    async def interrupt(self) -> None:
        """Interrupt the current turn; a client that refuses is only logged."""
        try:
            await self._client.interrupt()
        except Exception:
            log.exception("interrupting the subagent failed")

    async def close(self) -> None:
        """Disconnect the client; safe to call more than once."""
        try:
            await self._client.disconnect()
        except Exception:
            log.exception("disconnecting the subagent failed")


class ClaudeAgentRunner(AgentRunner):
    """Opens real Agent SDK conversations (`client_factory` is injected in tests)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[ClaudeAgentOptions], SdkClient] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or _default_client_factory

    async def open(self, task: Task, *, resume: str | None = None) -> AgentSession:
        """Connect a client for `task`; a failure to connect propagates to the caller."""
        options = build_options(task, self._settings, resume=resume)
        client = self._client_factory(options)
        await client.connect()
        log.info(
            "subagent opened for task %s (%s, model=%s, cwd=%s, resume=%s)",
            task.id,
            task.kind,
            options.model,
            options.cwd,
            resume,
        )
        return ClaudeAgentSession(client)


def _default_client_factory(options: ClaudeAgentOptions) -> SdkClient:
    return ClaudeSDKClient(options)


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
            await _emit(on_progress, line)
        return self._runner.next_result(self.task, self.resume)

    async def send(self, text: str) -> None:
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
    off by default, so an `interrupt()` merely gets counted.
    """

    def __init__(
        self,
        results: Sequence[RunResult] | FakeScript | None = None,
        *,
        delay_s: float = 0.0,
        progress: Iterable[str] | None = None,
        interrupt_ends_run: bool = False,
    ) -> None:
        self.delay_s = delay_s
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
