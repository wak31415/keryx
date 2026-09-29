"""The Claude backend: subagents on the Claude Agent SDK.

`ClaudeAgentRunner` builds `ClaudeAgentOptions` for one task from its `AgentContext` and
connects a `ClaudeSDKClient`; `ClaudeAdapter` turns the SDK's messages into the events the
shared `AdapterSession` (`agents/session.py`) runs every backend's turn from. The SDK client
is injected (`client_factory`) so tests never start the `claude` CLI.

A follow-up never goes into a running Claude turn: a `query()` sent after the final text
but before the `ResultMessage` starts a second turn that `receive_response()` never reads
(verified 2026-09-26), so `steer()` refuses and the task manager re-runs instead.

Tool restriction goes through `ClaudeAgentOptions.tools` — the base set of built-in tools
the subagent has at all. Do **not** use `allowed_tools` for that: it is an auto-approve
list, and under `permission_mode="bypassPermissions"` everything is approved anyway. The
one `allowed_tools` entry we keep is the `mcp__google__*` wildcard for Google, because MCP
tools are not built-ins and `tools` cannot express them.

This backend stays on the SDK rather than a bare `claude -p` subprocess: the SDK is what
gives us `max_budget_usd`, `max_turns`, the lifted message-size limit and typed messages.

`claude_agent_sdk` is imported only where a Claude agent actually runs: it is the `claude`
extra, and a machine installed with Codex alone must still import all of Jarvis.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.agents.auth import AuthSource, child_env
from jarvis.agents.base import SteerUnavailable, TokenUsage, google_mcp_server_config
from jarvis.agents.session import (
    AdapterRunner,
    AdapterSession,
    AgentContext,
    AgentEvent,
    Done,
    SessionId,
    Text,
    ToolCall,
)
from jarvis.config import Settings
from jarvis.config.files import claude_config_dir
from jarvis.tasks.models import Task

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

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


def claude_cli() -> str | None:
    """The `claude` binary the SDK will run: its bundled copy, else one on PATH.

    None when the SDK itself is not installed (the `claude` extra): a `claude` on PATH is
    no use to a runner that cannot import the SDK that drives it.
    """
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("claude")


def claude_stored_login() -> bool:
    """Best-effort: does the Claude CLI have a stored subscription login on this machine?"""
    if (claude_config_dir() / ".credentials.json").exists():
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


def claude_context(task: Task, settings: Settings) -> AgentContext:
    """Everything a Claude subagent for `task` is started with."""
    servers = {}
    if settings.google_workspace_mcp:
        servers["google"] = google_mcp_server_config(settings)
    return AgentContext.build(
        task,
        settings,
        auth=CLAUDE_AUTH,
        model=resolve_model(task.model, settings),
        mcp_servers=servers,
    )


def build_options(
    task: Task,
    settings: Settings,
    *,
    resume: str | None = None,
    context: AgentContext | None = None,
) -> ClaudeAgentOptions:
    """The Agent SDK options for one task: permissions, tools, model, prompt suffix.

    `context` is the task's resolved `AgentContext`, when the caller already has it.
    """
    return _options(context or claude_context(task, settings), settings, resume)


def _options(context: AgentContext, settings: Settings, resume: str | None) -> ClaudeAgentOptions:
    from claude_agent_sdk import ClaudeAgentOptions

    options: dict[str, Any] = {
        "permission_mode": "bypassPermissions",
        "cwd": str(context.cwd),
        "setting_sources": ["user", "project"],
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": context.instructions,
        },
        "model": context.model,
        "max_turns": settings.subagent_max_turns,
        "max_budget_usd": settings.subagent_max_budget_usd,
        "resume": resume,
        "max_buffer_size": SUBAGENT_MAX_BUFFER_BYTES,
    }
    if context.mcp_servers:
        options["mcp_servers"] = dict(context.mcp_servers)
        options["allowed_tools"] = list(GOOGLE_MCP_TOOLS)
    # With neither key nor token set, the spawned CLI falls back to the user's stored
    # Claude subscription login — the default, so subagents don't bill per token.
    if env := child_env(context.auth):
        options["env"] = env
    return ClaudeAgentOptions(**options)


# ----------------------------------------------------------------------------- adapter


def claude_usage(usage: Mapping[str, Any] | None) -> TokenUsage | None:
    """The SDK's usage as a `TokenUsage`: its `input_tokens` are the uncached ones only."""
    if not usage:
        return None
    cached = int(usage.get("cache_read_input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens") or 0) + written + cached,
        output_tokens=int(usage.get("output_tokens") or 0),
        cached_input_tokens=cached,
    )


class ClaudeAdapter:
    """One `ClaudeSDKClient` conversation, as the events `AdapterSession` runs on."""

    def __init__(self, client: SdkClient, *, secrets: Iterable[str | None] = ()) -> None:
        self._client = client
        self.secrets = list(secrets)

    async def turn(self, prompt: str) -> AsyncIterator[AgentEvent]:
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        await self._client.query(prompt)
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield Text(block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield ToolCall(block.name, block.input)
            elif isinstance(message, ResultMessage):
                yield SessionId(message.session_id)
                result = (message.result or "").strip() or None
                yield Done(
                    ok=not message.is_error,
                    result_text=result,
                    error=(result or message.subtype) if message.is_error else None,
                    usage=claude_usage(message.usage),
                    cost_usd=message.total_cost_usd,
                )

    async def steer(self, text: str) -> None:
        raise SteerUnavailable("claude takes follow-ups as a resumed run")

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def close(self) -> None:
        await self._client.disconnect()


class ClaudeAgentSession(AdapterSession):
    """A `ClaudeSDKClient` conversation as an `AgentSession`."""

    def __init__(self, client: SdkClient) -> None:
        super().__init__(ClaudeAdapter(client))


# ------------------------------------------------------------------------------ runner


class ClaudeAgentRunner(AdapterRunner):
    """Opens real Agent SDK conversations (`client_factory` is injected in tests)."""

    name = "claude"

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[ClaudeAgentOptions], SdkClient] | None = None,
    ) -> None:
        super().__init__(settings)
        self._client_factory = client_factory or _default_client_factory

    def context(self, task: Task) -> AgentContext:
        return claude_context(task, self.settings)

    async def connect(self, context: AgentContext, resume: str | None) -> ClaudeAdapter:
        options = _options(context, self.settings, resume)
        client = self._client_factory(options)
        await client.connect()
        return ClaudeAdapter(client, secrets=context.secrets)


def _default_client_factory(options: ClaudeAgentOptions) -> SdkClient:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options)
