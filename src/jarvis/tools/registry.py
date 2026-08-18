"""The tool layer the model calls into (spec §3.2 `tools/registry.py`).

A tool is a name + an OpenAI function schema + an async handler. The registry is the
only thing the voice session knows about tools: it hands the schemas to the provider at
connect time and routes `FunctionCall` events to `call()`.

`call()` never raises (except cancellation): a missing tool or a broken handler comes
back as `{"error": ...}` so the model can apologize and carry on instead of the session
dying on a bug in one tool.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a circular import
    from jarvis.session import VoiceSession

log = logging.getLogger("jarvis.tools.registry")


@dataclass
class ToolContext:
    """What a handler gets to know about the session it was called from.

    `session` is duck-typed (`.authorized`, `.channel`, `.caller`, `.session_id`,
    `.request_end()`, `.authorize()`) so tools can be tested with a stub. `authorized` is
    a property rather than a snapshot: a PIN entered *during* a long-running tool call
    must be visible to the next check.
    """

    session: "VoiceSession"
    channel: str
    caller: str | None

    @property
    def authorized(self) -> bool:
        return self.session.authorized


ToolHandler = Callable[[ToolContext, dict], Awaitable[dict]]


@dataclass
class _Tool:
    name: str
    description: str
    parameters: dict
    handler: ToolHandler = field(repr=False)

    def schema(self) -> dict:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Named tools plus their schemas; registration order is preserved."""

    def __init__(self) -> None:
        self._tools: dict[str, _Tool] = {}

    def register(
        self, name: str, description: str, parameters: dict, handler: ToolHandler
    ) -> None:
        """Add (or replace) a tool. `parameters` is a JSON-Schema object."""
        if name in self._tools:
            log.warning("re-registering tool %s", name)
        self._tools[name] = _Tool(name, description, parameters, handler)

    def schemas(self) -> list[dict]:
        """The OpenAI function-tool schemas for `SessionConfig.tools`."""
        return [tool.schema() for tool in self._tools.values()]

    async def call(self, name: str, arguments: dict, ctx: ToolContext) -> dict:
        """Run a tool and always return a JSON-serialisable dict."""
        tool = self._tools.get(name)
        if tool is None:
            log.warning("model called unknown tool %s", name)
            return {"error": f"unknown tool: {name}"}

        log.info("tool %s called by session %s", name, ctx.session.session_id)
        try:
            result = await tool.handler(ctx, arguments)
        except Exception as exc:  # a broken tool must not end the call
            log.exception("tool %s failed", name)
            return {"error": f"{type(exc).__name__}: {exc}"}

        if isinstance(result, dict):
            return result
        return {"result": result}
