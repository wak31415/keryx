"""The tool layer the model calls into.

A tool is a name + an OpenAI function schema + an async handler. The registry is the
only thing the voice session knows about tools: it hands the schemas to the provider at
connect time and routes `FunctionCall` events to `call()`.

`call()` never raises (except cancellation): a missing tool or a broken handler comes
back as `{"error": ...}` so the model can apologize and carry on instead of the session
dying on a bug in one tool.

A tool may also be **silent**. Submitting a tool result normally asks the provider for a
new response, which is why every call the model makes costs a spoken turn — and why pure
bookkeeping (`mark_reported`) used to make it say the thing it had just said all over
again. `silent=True` says "this result has nothing to speak about": the session submits
the output without asking for a response, and the model's next turn is the caller's.
Only for tools the model calls *after* it has spoken, never for one whose answer they are
waiting to hear.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from keryx.trust import TrustLevel

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a circular import
    from keryx.session import VoiceSession

log = logging.getLogger("keryx.tools.registry")


@dataclass
class ToolContext:
    """What a handler gets to know about the session it was called from.

    `session` is duck-typed (`.authorized`, `.trust`, `.channel`, `.caller`,
    `.session_id`, `.opening_task_id`, `.request_end()`, `.authorize()`) so tools can be
    tested with a stub. `authorized` and `trust` are properties rather than snapshots: a
    PIN entered *during* a long-running tool call must be visible to the next check.
    """

    session: "VoiceSession"
    channel: str
    caller: str | None

    @property
    def authorized(self) -> bool:
        return self.session.authorized

    @property
    def trust(self) -> TrustLevel:
        """What this call has proved, right now (`keryx.trust`)."""
        return self.session.trust


ToolHandler = Callable[[ToolContext, dict], Awaitable[dict]]


@dataclass
class _Tool:
    name: str
    description: str
    parameters: dict
    handler: ToolHandler = field(repr=False)
    #: True when this tool's result must not provoke a spoken turn (see the module docstring).
    silent: bool = False

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
        self._loader: Callable[[ToolRegistry], None] | None = None

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        """Every tool's name, in the order they are offered."""
        return list(self._tools)

    def set_loader(self, loader: Callable[["ToolRegistry"], None]) -> None:
        """Run `loader` over a copy of this registry at the top of every call.

        How the owner's own tools (`keryx.tools.custom`) arrive without a restart: the
        built-in tools are registered once, and whatever the loader adds is read afresh
        for each call, into that call's copy alone.
        """
        self._loader = loader

    def for_call(self) -> "ToolRegistry":
        """The tools one call is offered: these, plus whatever the loader adds now.

        A loader that fails costs the call its extra tools, never the call.
        """
        if self._loader is None:
            return self
        copy = ToolRegistry()
        copy._tools = dict(self._tools)
        try:
            self._loader(copy)
        except Exception:
            log.exception("loading the custom tools failed; this call has the built-in ones")
            copy._tools = dict(self._tools)
        return copy

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: ToolHandler,
        *,
        silent: bool = False,
    ) -> None:
        """Add (or replace) a tool. `parameters` is a JSON-Schema object.

        `silent` marks a tool whose result must not provoke a spoken turn — bookkeeping
        the caller has already heard the point of.
        """
        if name in self._tools:
            log.warning("re-registering tool %s", name)
        self._tools[name] = _Tool(name, description, parameters, handler, silent=silent)

    def is_silent(self, name: str) -> bool:
        """True when this tool's result should be submitted without asking for a response.

        An unknown name is not silent: the model has to be told it called something that
        does not exist, and that is a sentence.
        """
        tool = self._tools.get(name)
        return tool is not None and tool.silent

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
