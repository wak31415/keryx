"""The owner's own voice tools: loaded from `DATA_DIR/tools` afresh for every call."""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.tools import ToolContext, ToolRegistry
from jarvis.tools.custom import custom_tool, load_custom_tools, register_custom_tools
from jarvis.trust import TrustLevel

GOOD = '''
from jarvis.tools.custom import custom_tool

@custom_tool(description="The moon's phase tonight, in one sentence.", needs_pin=False)
async def moon_phase(ctx, args):
    return {"phase": "waxing"}
'''


@dataclass
class StubSession:
    trust: TrustLevel = TrustLevel.FULL
    keypressed: bool = False
    session_id: str = "sess1234"

    @property
    def authorized(self) -> bool:
        return self.trust is TrustLevel.FULL


@pytest.fixture
def tools(settings: Settings) -> Path:
    settings.ensure_dirs()
    return settings.custom_tools_dir


def write(directory: Path, name: str, source: str) -> Path:
    path = directory / name
    path.write_text(source, encoding="utf-8")
    path.chmod(0o600)
    return path


def tool_source(function: str, *, body: str = 'return {"ok": True}', **options: str) -> str:
    extra = "".join(f", {key}={value}" for key, value in options.items())
    return (
        "from jarvis.tools.custom import custom_tool\n\n"
        f'@custom_tool(description="Does {function}."{extra})\n'
        f"async def {function}(ctx, args):\n    {body}\n"
    )


async def call(registry: ToolRegistry, name: str, args: dict | None = None, **session) -> dict:
    stub = StubSession(**session)
    return await registry.call(name, args or {}, ToolContext(stub, "phone", None))


def pinned(tmp_path: Path) -> Settings:
    settings = Settings(
        _env_file=None, openai_api_key="test", data_dir=tmp_path / "d", pin="424242"
    )
    settings.ensure_dirs()
    return settings


def loaded(settings: Settings, taken: tuple[str, ...] = ()) -> ToolRegistry:
    registry = ToolRegistry()
    for name in taken:
        registry.register(name, "built in", {"type": "object"}, lambda ctx, args: None)
    register_custom_tools(registry, settings)
    return registry


# --- loading --------------------------------------------------------------------------


def test_ensure_dirs_makes_the_tools_directory_owner_only(settings, tools):
    assert tools == settings.data_dir / "tools"
    assert tools.stat().st_mode & 0o777 == 0o700


def test_a_missing_directory_is_no_tools(tmp_path):
    result = load_custom_tools(tmp_path / "nowhere")

    assert result.tools == [] and result.errors == []


def test_a_decorated_function_is_a_tool_named_after_it(tools):
    write(tools, "moon.py", GOOD)

    ((path, tool),) = load_custom_tools(tools).tools

    assert path.name == "moon.py"
    assert tool.name == "moon_phase"
    assert tool.needs_pin is False
    assert tool.parameters == {"type": "object", "properties": {}}


def test_one_file_may_define_several_tools_and_name_them(tools):
    write(tools, "pair.py", tool_source("first") + tool_source("second", name='"renamed"'))

    names = [tool.name for _, tool in load_custom_tools(tools).tools]

    assert names == ["first", "renamed"]


def test_an_underscore_file_is_left_alone(tools):
    write(tools, "_draft.py", "raise RuntimeError('never imported')")

    assert load_custom_tools(tools).errors == []


def test_a_broken_file_costs_that_file_and_nothing_else(tools):
    write(tools, "a_broken.py", "def oops(:\n")
    write(tools, "b_moon.py", GOOD)

    result = load_custom_tools(tools)

    assert [tool.name for _, tool in result.tools] == ["moon_phase"]
    ((path, why),) = result.errors
    assert path.name == "a_broken.py" and why.startswith("SyntaxError")


def test_a_file_that_defines_nothing_is_reported(tools):
    write(tools, "empty.py", "X = 1\n")

    ((_, why),) = load_custom_tools(tools).errors

    assert "defines no tool" in why


def test_a_built_in_name_can_never_be_taken(tools):
    write(tools, "sneaky.py", tool_source("submit_pin"))

    result = load_custom_tools(tools, taken=["submit_pin"])

    assert result.tools == []
    assert "already a tool's name" in result.errors[0][1]


def test_the_first_file_keeps_a_name_two_files_want(tools):
    write(tools, "a.py", tool_source("twice"))
    write(tools, "b.py", tool_source("twice"))

    result = load_custom_tools(tools)

    assert [path.name for path, _ in result.tools] == ["a.py"]
    assert [path.name for path, _ in result.errors] == ["b.py"]


@pytest.mark.parametrize(
    ("options", "complaint"),
    [
        ({"name": '"has space"'}, "not a valid tool name"),
        ({"needs_pin": '"no"'}, "needs_pin must be True or False"),
        ({"parameters": '{"type": "string"}'}, "JSON-schema object"),
        ({"timeout_s": "0"}, "timeout_s must be positive"),
    ],
)
def test_an_invalid_tool_is_refused_with_why(tools, options, complaint):
    write(tools, "bad.py", tool_source("bad", **options))

    ((_, why),) = load_custom_tools(tools).errors

    assert complaint in why


def test_an_empty_description_is_refused(tools):
    write(tools, "bad.py", tool_source("bad").replace('"Does bad."', '"  "'))

    ((_, why),) = load_custom_tools(tools).errors

    assert "no description" in why


def test_a_file_others_could_write_is_refused_unread(tools):
    path = write(tools, "open.py", "raise RuntimeError('must not run')")
    path.chmod(0o666)

    ((_, why),) = load_custom_tools(tools).errors

    assert "writable by other users" in why


def test_a_directory_others_could_write_is_refused_whole(tools):
    write(tools, "moon.py", GOOD)
    tools.chmod(0o777)

    result = load_custom_tools(tools)

    assert result.tools == []
    assert result.errors == [(tools, "writable by other users (chmod go-w)")]


def test_a_file_owned_by_someone_else_is_refused(tools, monkeypatch):
    write(tools, "moon.py", GOOD)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(tools).st_uid + 1)

    assert load_custom_tools(tools).errors == [(tools, "owned by another user")]


def test_every_load_reads_the_file_again(tools):
    write(tools, "moon.py", GOOD)
    load_custom_tools(tools)
    write(tools, "moon.py", GOOD.replace("moon_phase", "moon_rise"))

    assert [tool.name for _, tool in load_custom_tools(tools).tools] == ["moon_rise"]


# --- registering and calling ----------------------------------------------------------


def test_registered_tools_come_after_the_built_in_ones(settings, tools, caplog):
    write(tools, "moon.py", GOOD)
    write(tools, "clash.py", tool_source("dispatch_task"))

    with caplog.at_level(logging.INFO, logger="jarvis.tools.custom"):
        registry = loaded(settings, taken=("dispatch_task",))

    assert registry.names() == ["dispatch_task", "moon_phase"]
    assert "custom tool clash.py refused" in caplog.text
    assert "custom tools: moon_phase" in caplog.text


async def test_a_tool_needs_the_pin_unless_it_says_otherwise(tmp_path):
    settings = pinned(tmp_path)
    write(settings.custom_tools_dir, "act.py", tool_source("act"))
    registry = loaded(settings)

    assert (await call(registry, "act", trust=TrustLevel.NONE))["status"] == "pin_required"
    assert await call(registry, "act", trust=TrustLevel.FULL) == {"ok": True}


async def test_possession_is_not_the_pin(tmp_path):
    """A call Jarvis placed to their phone still has to give the PIN for a custom tool."""
    settings = pinned(tmp_path)
    write(settings.custom_tools_dir, "act.py", tool_source("act"))

    result = await call(loaded(settings), "act", trust=TrustLevel.POSSESSION, keypressed=True)

    assert result["status"] == "pin_required"


async def test_needs_pin_false_answers_anyone(settings, tools):
    write(tools, "moon.py", GOOD)

    result = await call(loaded(settings), "moon_phase", trust=TrustLevel.NONE)

    assert result == {"phase": "waxing"}


async def test_a_plain_function_runs_in_a_thread_and_gets_its_arguments(settings, tools):
    write(
        tools,
        "echo.py",
        "import threading\n"
        "from jarvis.tools.custom import custom_tool\n\n"
        "@custom_tool(description='Echo.', needs_pin=False)\n"
        "def echo(ctx, args):\n"
        "    return {'said': args['text'], 'main': threading.current_thread()"
        " is threading.main_thread()}\n",
    )

    result = await call(loaded(settings), "echo", {"text": "hi"})

    assert result == {"said": "hi", "main": False}


async def test_a_result_that_is_not_a_dict_or_not_json_is_made_safe(settings, tools):
    write(tools, "odd.py", tool_source("odd", body="return {'when': object()}", needs_pin="False"))
    write(tools, "bare.py", tool_source("bare", body="return 7", needs_pin="False"))
    registry = loaded(settings)

    assert (await call(registry, "odd"))["when"].startswith("<object object")
    assert await call(registry, "bare") == {"result": 7}


async def test_a_slow_tool_is_cut_off_with_a_sentence(settings, tools):
    body = "import asyncio; await asyncio.sleep(5)"
    write(tools, "slow.py", tool_source("slow", body=body, needs_pin="False", timeout_s="0.05"))

    result = await asyncio.wait_for(call(loaded(settings), "slow"), 2)

    assert result == {"error": "slow did not answer in time"}


async def test_a_tool_that_raises_does_not_end_the_call(settings, tools):
    write(tools, "boom.py", tool_source("boom", body="raise ValueError('nope')", needs_pin="False"))

    assert await call(loaded(settings), "boom") == {"error": "ValueError: nope"}


def test_a_silent_tool_is_registered_silent(settings, tools):
    write(tools, "hush.py", tool_source("hush", silent="True"))

    assert loaded(settings).is_silent("hush")


def test_the_decorator_leaves_a_tool_that_says_what_it_is():
    @custom_tool(description="d")
    def thing(ctx, args):
        return {}

    assert thing.name == "thing" and thing.needs_pin is True and thing.timeout_s == 20.0


# --- the registry, per call -----------------------------------------------------------


def test_without_a_loader_every_call_shares_the_registry():
    registry = ToolRegistry()

    assert registry.for_call() is registry


def test_each_call_gets_its_own_copy_with_the_loaded_tools(settings, tools):
    registry = ToolRegistry()
    registry.register("builtin", "b", {"type": "object"}, lambda ctx, args: None)
    registry.set_loader(lambda call_tools: register_custom_tools(call_tools, settings))
    write(tools, "moon.py", GOOD)

    first = registry.for_call()
    (tools / "moon.py").unlink()
    second = registry.for_call()

    assert first.names() == ["builtin", "moon_phase"]
    assert second.names() == ["builtin"]
    assert registry.names() == ["builtin"]
    assert "moon_phase" in first and "moon_phase" not in registry


def test_a_loader_that_fails_leaves_the_call_its_built_in_tools(caplog):
    registry = ToolRegistry()
    registry.register("builtin", "b", {"type": "object"}, lambda ctx, args: None)

    def broken(call_tools: ToolRegistry) -> None:
        call_tools.register("half", "h", {"type": "object"}, lambda ctx, args: None)
        raise RuntimeError("disk gone")

    registry.set_loader(broken)

    assert registry.for_call().names() == ["builtin"]
    assert "loading the custom tools failed" in caplog.text
