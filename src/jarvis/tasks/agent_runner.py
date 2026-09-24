"""The subagent runner's spec §3.2 names, kept where the spec puts them.

The runners themselves live in `jarvis.agents` now — one module per coding agent, with
what they share in `jarvis.agents.base`. These names stay importable from here because
§3.2 keeps them stable.
"""

from jarvis.agents.base import (
    DEFAULT_FAKE_RESULT,
    INTERRUPTED_RESULT,
    NO_SUMMARY,
    AgentRunner,
    AgentSession,
    FakeAgentRunner,
    FakeAgentSession,
    RunResult,
    extract_restart_request,
    extract_spoken_summary,
    google_mcp_server_config,
    render_subagent_suffix,
)
from jarvis.agents.claude import ClaudeAgentRunner, ClaudeAgentSession, resolve_model

__all__ = [
    "DEFAULT_FAKE_RESULT",
    "INTERRUPTED_RESULT",
    "NO_SUMMARY",
    "AgentRunner",
    "AgentSession",
    "ClaudeAgentRunner",
    "ClaudeAgentSession",
    "FakeAgentRunner",
    "FakeAgentSession",
    "RunResult",
    "extract_restart_request",
    "extract_spoken_summary",
    "google_mcp_server_config",
    "render_subagent_suffix",
    "resolve_model",
]
