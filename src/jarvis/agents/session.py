"""One agent session for every backend: the loop is written once, here.

A backend is an *adapter*. It turns one prompt into a stream of normalized events —
`Text`, `ToolCall`, `FileEdit`, `SessionId`, `Notice`, and a closing `Done` — and it knows
how to steer, interrupt and close its own client. Everything else is `AdapterSession`, the
one `AgentSession` a real backend needs: progress lines, the final text, the
`SPOKEN_SUMMARY:` and `RESTART_REQUIRED:` reading, the usage, and the redaction of every
credential the adapter was handed from whatever it logs or returns.

`AdapterRunner` is the matching `AgentRunner`. It resolves the task's `AgentContext` once
— the directory, the instructions, the model, the credential, the MCP servers — and asks
the backend to `connect()` an adapter for it. A connection that fails is an
`AgentOpenError` with the credentials already taken out.
"""

import contextlib
import json
import logging
import traceback
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jarvis.agents.auth import AuthSource, AuthStatus, redact, resolve_auth
from jarvis.agents.base import (
    AgentOpenError,
    AgentRunner,
    AgentSession,
    RunResult,
    TokenUsage,
    emit_progress,
    extract_restart_request,
    extract_spoken_summary,
    failure_summary,
    render_subagent_suffix,
    workspace_dir,
)
from jarvis.config import Settings
from jarvis.issues import IssueReporting
from jarvis.tasks.models import Task

log = logging.getLogger("jarvis.agents.session")

#: How much of a tool call's arguments a progress line carries.
MAX_TOOL_INPUT_CHARS = 200
#: An MCP server's environment value shorter than this is a flag (`1`, `true`), not a
#: credential, and redacting it would blank every digit in an error message.
MIN_SECRET_CHARS = 8
#: What a turn that produced no `Done` at all is reported as.
NO_DONE_ERROR = "the agent stopped without finishing"
INTERRUPTED_ERROR = "interrupted"


# ------------------------------------------------------------------------------ events


@dataclass(frozen=True)
class Text:
    """Something the agent said: progress while it works, the report at the end."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """A tool the agent started to use."""

    name: str
    arguments: Any = None


@dataclass(frozen=True)
class FileEdit:
    """Files the agent changed: `(kind, path)` pairs, such as `("add", "a.py")`."""

    changes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SessionId:
    """The id a later run resumes this conversation by."""

    id: str


@dataclass(frozen=True)
class Notice:
    """Worth a line in the log, not in the task's progress."""

    text: str
    level: int = logging.WARNING


@dataclass(frozen=True)
class Done:
    """The end of the turn, and how it went."""

    ok: bool
    result_text: str | None = None
    error: str | None = None
    interrupted: bool = False
    usage: TokenUsage | None = None
    cost_usd: float | None = None


AgentEvent = Text | ToolCall | FileEdit | SessionId | Notice | Done


class AgentAdapter(Protocol):
    """One backend's live conversation, as `AdapterSession` drives it."""

    #: Every credential the adapter's process was handed, to be kept out of what is said.
    secrets: Sequence[str | None]

    def turn(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Run one turn; ends with a `Done`, or raises."""
        ...

    async def steer(self, text: str) -> None:
        """Put `text` into the running turn; `SteerUnavailable` when that cannot be done."""
        ...

    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


# ----------------------------------------------------------------------------- context


@dataclass(frozen=True)
class AgentContext:
    """What a task's agent is started with, resolved once for whichever agent runs it."""

    cwd: Path
    instructions: str
    model: str | None
    auth: AuthStatus
    mcp_servers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        task: Task,
        settings: Settings,
        *,
        auth: AuthSource,
        model: str | None,
        mcp_servers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> "AgentContext":
        from jarvis.plugins.slack import slack_route

        slack = slack_route(settings)
        return cls(
            cwd=workspace_dir(task, settings),
            instructions=render_subagent_suffix(
                task,
                slack_mcp_server=slack.mcp_server if slack is not None else None,
                owner=settings.owner_label,
                tools_dir=settings.custom_tools_dir,
                issues=IssueReporting.from_settings(settings),
            ),
            model=model or None,
            auth=resolve_auth(auth, settings, probe=False),
            mcp_servers=dict(mcp_servers or {}),
        )

    @property
    def mcp_env(self) -> dict[str, str]:
        """Every MCP server's environment, merged: what the agent's process must carry."""
        env: dict[str, str] = {}
        for config in self.mcp_servers.values():
            env.update({str(k): str(v) for k, v in (config.get("env") or {}).items()})
        return env

    @property
    def secrets(self) -> tuple[str, ...]:
        """The credential, and every MCP value that could be one.

        Paths and URLs are configuration, and short values are flags; everything else an
        MCP server is handed — a client secret, a bot token — is treated as a secret.
        """
        values = [self.auth.secret or ""]
        values += [
            value
            for value in self.mcp_env.values()
            if len(value) >= MIN_SECRET_CHARS and not value.startswith(("/", "~", "http"))
        ]
        return tuple(dict.fromkeys(value for value in values if value))


# ---------------------------------------------------------------------------- progress


def _arguments(value: Any) -> str:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):  # pragma: no cover - json.dumps(default=str) rarely fails
        text = str(value)
    return text[:MAX_TOOL_INPUT_CHARS]


def progress_line(event: ToolCall | FileEdit) -> str:
    """A short, loggable one-liner for a tool call or a set of file changes."""
    if isinstance(event, ToolCall):
        return f"[tool] {event.name} {_arguments(event.arguments)}"
    paths = ", ".join(f"{kind} {path}" for kind, path in event.changes)
    return f"[edit] {paths[:MAX_TOOL_INPUT_CHARS]}"


# ----------------------------------------------------------------------------- session


class AdapterSession(AgentSession):
    """The `AgentSession` of every real backend: one adapter, driven event by event."""

    def __init__(self, adapter: AgentAdapter, *, session_id: str | None = None) -> None:
        self._adapter = adapter
        self._session_id = session_id
        self._closed = False

    def _redact(self, text: str) -> str:
        return redact(text, self._adapter.secrets)

    def _log_failure(self, what: str, exc: BaseException) -> str:
        """Log `exc` with every credential taken out, and return its redacted one-liner.

        Never `log.exception`: a provider's message can quote a key, and a traceback
        formatted by the logging module would carry it past the redaction.
        """
        error = self._redact(f"{type(exc).__name__}: {exc}")
        log.warning("%s: %s", what, error)
        log.debug("%s", self._redact("".join(traceback.format_exception(exc))))
        return error

    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult:
        """Run one turn to its `Done`; never raises, except to be cancelled."""
        texts: list[str] = []
        done: Done | None = None
        try:
            async with contextlib.aclosing(self._adapter.turn(prompt)) as events:
                async for event in events:
                    if isinstance(event, Done):
                        done = event
                        break
                    await self._handle(event, texts, on_progress)
        except Exception as exc:
            error = self._log_failure("subagent turn failed", exc)
            return self._failed(error, "\n\n".join(texts))

        final_text = (done.result_text if done else None) or (texts[-1] if texts else "")
        if done is None:
            return self._failed(NO_DONE_ERROR, final_text)
        if not done.ok:
            # The spoken line is the failure, never the last message: that is usually the
            # model announcing what it was about to do, which reads as a result it is not.
            error = done.error or (INTERRUPTED_ERROR if done.interrupted else "the turn failed")
            return self._failed(self._redact(error), final_text, done)
        return RunResult(
            ok=True,
            final_text=final_text,
            spoken_summary=extract_spoken_summary(final_text),
            session_id=self._session_id,
            cost_usd=done.cost_usd,
            restart_reason=extract_restart_request(final_text),
            usage=done.usage,
        )

    async def _handle(
        self, event: AgentEvent, texts: list[str], on_progress: Callable[[str], Any]
    ) -> None:
        if isinstance(event, Text):
            text = event.text.strip()
            if text:
                texts.append(text)
                await emit_progress(on_progress, text)
        elif isinstance(event, ToolCall | FileEdit):
            await emit_progress(on_progress, progress_line(event))
        elif isinstance(event, SessionId):
            self._session_id = event.id
        elif isinstance(event, Notice):
            log.log(event.level, "subagent: %s", self._redact(event.text))

    def _failed(self, error: str, final_text: str, done: Done | None = None) -> RunResult:
        return RunResult(
            ok=False,
            final_text=final_text,
            spoken_summary=failure_summary(error),
            session_id=self._session_id,
            cost_usd=done.cost_usd if done else None,
            error=error,
            usage=done.usage if done else None,
        )

    async def send(self, text: str) -> None:
        """Steer the running turn. `SteerUnavailable` means nothing was delivered.

        Anything else is a real failure, redacted: the text may or may not have arrived,
        so the caller must not quietly deliver it a second way.
        """
        try:
            await self._adapter.steer(text)
        except NotImplementedError:
            raise
        except Exception as exc:
            raise RuntimeError(self._log_failure("steering the subagent failed", exc)) from None

    async def interrupt(self) -> None:
        """Ask the agent to stop the current turn; a refusal is only logged."""
        try:
            await self._adapter.interrupt()
        except Exception as exc:
            self._log_failure("interrupting the subagent failed", exc)

    async def close(self) -> None:
        """End the conversation and its process; safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._adapter.close()
        except Exception as exc:
            self._log_failure("closing the subagent failed", exc)


# ------------------------------------------------------------------------------ runner


class AdapterRunner(AgentRunner):
    """An `AgentRunner` for a backend that is an adapter plus a way to connect one."""

    #: The name the log lines use.
    name = "agent"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def context(self, task: Task) -> AgentContext:
        """Everything `task` is started with, resolved once."""
        raise NotImplementedError

    async def connect(self, context: AgentContext, resume: str | None) -> AgentAdapter:
        """A live adapter for `context`, resuming `resume` when it is given."""
        raise NotImplementedError

    async def open(self, task: Task, *, resume: str | None = None) -> AgentSession:
        """Connect an adapter for `task`; a failure is an `AgentOpenError`, redacted."""
        context = self.context(task)
        try:
            adapter = await self.connect(context, resume)
        except Exception as exc:
            error = redact(f"{type(exc).__name__}: {exc}", context.secrets)
            raise AgentOpenError(error) from None
        log.info(
            "%s subagent opened for task %s (model=%s, auth=%s, cwd=%s, resume=%s)",
            self.name,
            task.id,
            context.model or "default",
            context.auth.mode,
            context.cwd,
            resume,
        )
        return AdapterSession(adapter, session_id=resume)
