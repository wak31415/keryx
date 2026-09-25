"""The Claude backend: subagents on the Claude Agent SDK.

`ClaudeAgentRunner.open()` builds `ClaudeAgentOptions` for one task and connects a
`ClaudeSDKClient`; the returned `ClaudeAgentSession` drives one live conversation —
`run()` streams a turn to completion, `send()` queues a follow-up into it, `interrupt()`
and `close()` end it. The SDK client is injected (`client_factory`) so tests never start
the `claude` CLI.

Tool restriction goes through `ClaudeAgentOptions.tools` — the base set of built-in tools
the subagent has at all. Do **not** use `allowed_tools` for that: it is an auto-approve
list, and under `permission_mode="bypassPermissions"` everything is approved anyway. The
one `allowed_tools` entry we keep is the `mcp__google__*` wildcard for Google, because MCP
tools are not built-ins and `tools` cannot express them.

This backend stays on the SDK rather than a bare `claude -p` subprocess: the SDK is what
gives us `max_budget_usd`, `max_turns`, the lifted message-size limit and typed messages.
"""

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from jarvis.agents.auth import AuthSource, child_env, resolve_auth
from jarvis.agents.base import (
    AgentRunner,
    AgentSession,
    RunResult,
    emit_progress,
    extract_restart_request,
    extract_spoken_summary,
    failure_summary,
    google_mcp_server_config,
    render_subagent_suffix,
    workspace_dir,
)
from jarvis.config import Settings
from jarvis.tasks.models import Task

log = logging.getLogger("jarvis.agents.claude")

#: The model names that can be said out loud, and the Claude ids they mean.
CLAUDE_MODELS = {
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

#: The largest single message the SDK will accept from the `claude` CLI. The default
#: (1 MiB) is smaller than the echo of one `Read` of a screenshot or figure, which
#: arrives as base64 in a single NDJSON line and killed the turn (tasks 68-98).
SUBAGENT_MAX_BUFFER_BYTES = 64 * 1024 * 1024

_MAX_TOOL_INPUT_CHARS = 200


def claude_cli() -> str | None:
    """The `claude` binary the SDK will run: its bundled copy, else one on PATH."""
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("claude")


def claude_stored_login() -> bool:
    """Best-effort: does the Claude CLI have a stored subscription login on this machine?"""
    if (Path.home() / ".claude" / ".credentials.json").exists():
        return True
    try:  # macOS stores the login in the Keychain instead of a file
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


CLAUDE_AUTH = AuthSource(
    api_key_setting="anthropic_api_key",
    api_key_env="ANTHROPIC_API_KEY",
    token_setting="claude_code_oauth_token",
    token_env="CLAUDE_CODE_OAUTH_TOKEN",
    stored_login=claude_stored_login,
    login_hint=(
        "run `claude` and `/login` once, or `claude setup-token` for a headless machine,"
        " or set ANTHROPIC_API_KEY"
    ),
)


class SdkClient(Protocol):
    """The slice of `ClaudeSDKClient` this module uses (injectable for tests)."""

    async def connect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> Any: ...
    async def interrupt(self) -> None: ...
    async def disconnect(self) -> None: ...


# ------------------------------------------------------------------------ agent options


def resolve_model(name: str | None, settings: Settings) -> str:
    """A spoken alias (`opus`, `sonnet`, …) or full model id as the id to run."""
    alias = (name or "").strip()
    if not alias:
        return settings.subagent_model
    return CLAUDE_MODELS.get(alias.lower(), alias)


def build_options(
    task: Task, settings: Settings, *, resume: str | None = None
) -> ClaudeAgentOptions:
    """The Agent SDK options for one task: permissions, tools, model, prompt suffix."""
    options: dict[str, Any] = {
        "permission_mode": "bypassPermissions",
        "cwd": str(workspace_dir(task, settings)),
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
    # With neither key nor token set, the spawned CLI falls back to the user's stored
    # Claude subscription login — the default, so subagents don't bill per token.
    if env := child_env(resolve_auth(CLAUDE_AUTH, settings, probe=False)):
        options["env"] = env
    return ClaudeAgentOptions(**options)


# ---------------------------------------------------------------------------- progress



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
                            await emit_progress(on_progress, text)
                        elif isinstance(block, ToolUseBlock):
                            await emit_progress(on_progress, _tool_line(block))
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
                spoken_summary=failure_summary(str(exc)),
                session_id=session_id,
                cost_usd=cost_usd,
                error=f"{type(exc).__name__}: {exc}",
            )

        final_text = result_text or (texts[-1] if texts else "")
        restart_reason = extract_restart_request(final_text) if ok else None
        if ok or final_text:
            spoken_summary = extract_spoken_summary(final_text)
        else:  # errored with nothing to summarise
            spoken_summary = failure_summary(error)
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


