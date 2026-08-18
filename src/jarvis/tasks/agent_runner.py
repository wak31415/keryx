"""Claude Agent SDK subagent runner (spec §3.2 `tasks/agent_runner.py`).

`ClaudeAgentRunner.open()` builds `ClaudeAgentOptions` for one task and connects a
`ClaudeSDKClient`; the returned `ClaudeAgentSession` drives one live conversation —
`run()` streams a turn to completion, `send()` queues a follow-up into it, `interrupt()`
and `close()` end it. The SDK client is injected (`client_factory`) so tests never start
the `claude` CLI. `FakeAgentRunner` is the scripted stand-in used by tests and by the
CLI's `--fake-agents` dev flag.

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

from jarvis.config import Settings
from jarvis.prompts import load_prompt
from jarvis.tasks.models import Task, TaskKind

log = logging.getLogger("jarvis.tasks.agent_runner")

SUBAGENT_SUFFIX_PROMPT = "subagent_suffix.md"

SPOKEN_SUMMARY_MARKER = "SPOKEN_SUMMARY:"
MAX_SUMMARY_CHARS = 400
NO_SUMMARY = "The task finished, but no summary was produced."

MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "fable": "claude-fable-5",
    "haiku": "claude-haiku-4-5-20251001",
}

_READ_ONLY_TOOLS = ["WebSearch", "WebFetch", "Read", "Glob", "Grep"]
_COWORK_TOOLS = ["mcp__google__*", "WebSearch", "WebFetch", "Read"]

# `coding` is deliberately absent: no `allowed_tools` restriction at all.
ALLOWED_TOOLS: dict[TaskKind, list[str]] = {
    TaskKind.CHAT: _READ_ONLY_TOOLS,
    TaskKind.RESEARCH: [*_READ_ONLY_TOOLS, "Write"],
    TaskKind.COWORK: _COWORK_TOOLS,
}

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

_MAX_TOOL_INPUT_CHARS = 200
_MAX_ERROR_CHARS = 160

# A `SPOKEN_SUMMARY:` line, tolerating the markdown the model wraps it in
# (`## SPOKEN_SUMMARY:`, `**SPOKEN_SUMMARY:**`, `- SPOKEN_SUMMARY:`).
_MARKER_RE = re.compile(
    rf"^[ \t]*(?:[#>*_\-+][ \t]*)*{re.escape(SPOKEN_SUMMARY_MARKER)}",
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
    num_turns: int | None = None


class AgentSession(Protocol):
    """One live subagent conversation."""

    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult:
        """Run one turn to completion. Never raises: failures come back as `ok=False`."""
        ...

    async def send(self, text: str) -> None:
        """Queue a follow-up into the running conversation."""
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


class _Defaulting(dict):
    """Format mapping that blanks unknown placeholders instead of raising."""

    def __missing__(self, key: str) -> str:
        log.warning("subagent prompt has an unknown placeholder: %s", key)
        return ""


def render_subagent_suffix(task: Task) -> str:
    """The subagent system-prompt suffix, with this task's kind/project/description."""
    values = _Defaulting(
        kind=str(task.kind),
        project=task.project or "none",
        description=task.description,
    )
    return load_prompt(SUBAGENT_SUFFIX_PROMPT).format_map(values)


def _google_mcp_server(settings: Settings) -> dict[str, Any]:
    """The `workspace-mcp` stdio server config for Gmail + Calendar (spec §4)."""
    env = {
        name: value
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", settings.google_oauth_client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", settings.google_oauth_client_secret),
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
            "append": render_subagent_suffix(task),
        },
        "model": resolve_model(task.model, settings),
        "max_turns": settings.subagent_max_turns,
        "max_budget_usd": settings.subagent_max_budget_usd,
        "resume": resume,
    }
    allowed_tools = ALLOWED_TOOLS.get(task.kind)
    if allowed_tools is not None:
        options["allowed_tools"] = list(allowed_tools)
    if task.kind is TaskKind.COWORK:
        options["mcp_servers"] = {"google": _google_mcp_server(settings)}
    if settings.anthropic_api_key:
        options["env"] = {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    return ClaudeAgentOptions(**options)


# ---------------------------------------------------------------------------- progress


async def _emit(on_progress: Callable[[str], Any] | None, text: str) -> None:
    """Report one progress line; a sync or async callback, whose failures are ignored."""
    if on_progress is None:
        return
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
        num_turns: int | None = None
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
                    num_turns = message.num_turns
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
                num_turns=num_turns,
            )

        final_text = result_text or (texts[-1] if texts else "")
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
            num_turns=num_turns,
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

    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult:
        """Sleep, emit the scripted progress lines, return the next scripted result."""
        self.prompts.append(prompt)
        await asyncio.sleep(self._runner.delay_s)
        for line in self._runner.progress:
            await _emit(on_progress, line)
        return self._runner.next_result(self.task, self.resume)

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def close(self) -> None:
        self.closed = True


class FakeAgentRunner(AgentRunner):
    """Scripted `AgentRunner` for tests and the CLI's `--fake-agents` dev flag.

    `results` is a list popped from the front (the last one repeats), a callable taking
    `(task, resume)`, or `None` for `DEFAULT_FAKE_RESULT` every time.
    """

    def __init__(
        self,
        results: Sequence[RunResult] | FakeScript | None = None,
        *,
        delay_s: float = 0.0,
        progress: Iterable[str] | None = None,
    ) -> None:
        self.delay_s = delay_s
        self.progress = list(progress or ())
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
