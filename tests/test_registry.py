"""Tests for the tool registry and tool context (spec §3.2 `tools/registry.py`)."""

import logging

import pytest

from jarvis.tools import ToolContext, ToolRegistry


class StubSession:
    """The duck-typed slice of `VoiceSession` a `ToolContext` exposes to handlers."""

    def __init__(self, *, authorized: bool = False) -> None:
        self.session_id = "abcd1234"
        self.channel = "phone"
        self.caller = "+491555555555"
        self.authorized = authorized
        self.end_reason: str | None = None

    def authorize(self) -> None:
        self.authorized = True

    def request_end(self, reason: str = "user") -> None:
        self.end_reason = reason


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def ctx():
    session = StubSession()
    return ToolContext(session=session, channel=session.channel, caller=session.caller)


PARAMS = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


async def echo(ctx: ToolContext, args: dict) -> dict:
    return {"echo": args["text"]}


# --- schemas --------------------------------------------------------------


def test_schemas_are_openai_function_tools(registry):
    registry.register("echo", "Echo the text back.", PARAMS, echo)

    assert registry.schemas() == [
        {
            "type": "function",
            "name": "echo",
            "description": "Echo the text back.",
            "parameters": PARAMS,
        }
    ]


def test_schemas_are_empty_for_a_fresh_registry(registry):
    assert registry.schemas() == []


def test_schemas_keep_registration_order(registry):
    registry.register("one", "First.", PARAMS, echo)
    registry.register("two", "Second.", PARAMS, echo)

    assert [schema["name"] for schema in registry.schemas()] == ["one", "two"]


# --- call -----------------------------------------------------------------


async def test_call_runs_the_handler_with_the_context_and_arguments(registry, ctx):
    seen: list[tuple[ToolContext, dict]] = []

    async def handler(context: ToolContext, args: dict) -> dict:
        seen.append((context, args))
        return {"ok": True}

    registry.register("echo", "Echo.", PARAMS, handler)

    result = await registry.call("echo", {"text": "hi"}, ctx)

    assert result == {"ok": True}
    assert seen == [(ctx, {"text": "hi"})]


async def test_call_returns_an_error_for_an_unknown_tool(registry, ctx):
    assert await registry.call("nope", {}, ctx) == {"error": "unknown tool: nope"}


async def test_call_converts_a_handler_exception_into_an_error_result(registry, ctx, caplog):
    async def boom(context: ToolContext, args: dict) -> dict:
        raise ValueError("no good")

    registry.register("boom", "Explode.", PARAMS, boom)

    with caplog.at_level(logging.ERROR, logger="jarvis.tools.registry"):
        result = await registry.call("boom", {}, ctx)

    assert result == {"error": "ValueError: no good"}
    assert "boom" in caplog.text


async def test_call_wraps_a_string_result_in_a_dict(registry, ctx):
    async def stringy(context: ToolContext, args: dict) -> str:
        return "done"

    registry.register("stringy", "Return a string.", PARAMS, stringy)

    assert await registry.call("stringy", {}, ctx) == {"result": "done"}


async def test_call_does_not_swallow_cancellation(registry, ctx):
    import asyncio

    async def cancelled(context: ToolContext, args: dict) -> dict:
        raise asyncio.CancelledError

    registry.register("cancelled", "Get cancelled.", PARAMS, cancelled)

    with pytest.raises(asyncio.CancelledError):
        await registry.call("cancelled", {}, ctx)


# --- context --------------------------------------------------------------


def test_context_authorized_reads_the_session_live():
    session = StubSession(authorized=False)
    ctx = ToolContext(session=session, channel="phone", caller=session.caller)

    assert ctx.authorized is False

    session.authorize()

    assert ctx.authorized is True
