"""The owner's own voice tools: Python files in `DATA_DIR/tools`, read at every call.

The built-in tools are Jarvis's and live in this package. These are the owner's — written
by them, or by a subagent they asked — and live beside their other data, never in the
repository, so a clone of Jarvis carries none of them and theirs are never committed.

A tool file is one self-contained module that defines one or more tools with the
decorator here::

    from jarvis.tools.custom import custom_tool

    @custom_tool(
        description="Say the phase of the moon tonight. Answer in one sentence.",
        needs_pin=False,
    )
    async def moon_phase(ctx, args):
        return {"phase": "waxing gibbous"}

The directory is read afresh at the top of every call (`ToolRegistry.for_call`), so a new
or edited tool is there from the next call, with no restart. Everything that can go wrong
with a file costs that file and is logged — never the call, and never another tool:

- a file that will not import, or defines nothing, is skipped; one that raises
  `ToolUnavailable` while it loads is skipped with the reason it gave (a plugin that is
  missing a sign-in says which);
- a name that is already taken — by any built-in tool, offered on this machine or not, or
  by an earlier file — is refused, so an owner's tool can never stand in for
  `dispatch_task` or `submit_pin`;
- a file or directory anyone but the owner could write is refused unread, because loading
  it runs its code inside the service;
- a handler that raises, hangs past `timeout_s` or returns something that is not JSON comes
  back to the model as an error it can say.

`needs_pin` is the gate, and it defaults to True: a tool that acts, or reads anything of
the owner's, answers only once the PIN is given (`pin_gate`). `needs_pin=False` answers
anyone who rings — for tools that only read what is public anyway. The finer tiers the
built-in tools use (`jarvis.trust`) are deliberately not offered here: a yes or a no is a
decision an owner can check at a glance.

`jarvis tools` prints what the directory holds and what was refused, which is how a
subagent checks its work before saying it is done.

The plugins (`jarvis.plugins`) are files of exactly this kind, written by `jarvis plugins
install`: a line that calls into the package, and a TOML file of settings beside it. While a
file loads, `LOADING_SETTINGS` holds the settings of whoever is loading it — the running
service, or the command asking — so a plugin reads the same configuration they do.
"""

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import re
import stat
import sys
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.config import Settings
from jarvis.tools.builtin import BUILTIN_TOOL_NAMES
from jarvis.tools.builtin_common import pin_gate
from jarvis.tools.registry import ToolContext, ToolRegistry

log = logging.getLogger("jarvis.tools.custom")

#: How long a custom tool may take before the caller is told it did not answer.
DEFAULT_TIMEOUT_S = 20.0
#: What OpenAI accepts as a function name.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: The modules a tool file is loaded as, so none can shadow a real one.
_MODULE_PREFIX = "jarvis_custom_tools"

CustomHandler = Callable[[ToolContext, dict], Awaitable[Any] | Any]

#: The settings of whoever is loading the directory, while a file loads; None outside a load.
LOADING_SETTINGS: ContextVar[Settings | None] = ContextVar("loading_settings", default=None)


class ToolUnavailable(Exception):
    """Raised while a tool file loads to refuse it, with the reason as the whole message.

    For a file that is fine but cannot answer on this machine yet — not signed in, nothing
    configured — so `jarvis tools` says what to do rather than printing a traceback.
    """


@dataclass(frozen=True)
class CustomTool:
    """One tool a file defines: what the model is told, and what runs."""

    name: str
    description: str
    parameters: dict
    handler: CustomHandler = field(repr=False)
    needs_pin: bool = True
    silent: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S


def custom_tool(
    name: str | None = None,
    *,
    description: str,
    parameters: dict | None = None,
    needs_pin: bool = True,
    silent: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Callable[[CustomHandler], CustomTool]:
    """Make the decorated function a voice tool, named after it unless `name` is given.

    The function takes `(ctx, args)` — the `ToolContext` of the call and the model's
    arguments — and returns a dict for the model to speak from. It may be `async` or not;
    a plain function runs in a thread, so it may block.
    """

    def wrap(handler: CustomHandler) -> CustomTool:
        return CustomTool(
            name=name or handler.__name__,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            handler=handler,
            needs_pin=needs_pin,
            silent=silent,
            timeout_s=timeout_s,
        )

    return wrap


@dataclass(frozen=True)
class Loaded:
    """What the directory held: the tools it defines and the files refused, with why."""

    tools: list[tuple[Path, CustomTool]]
    errors: list[tuple[Path, str]]


def load_custom_tools(
    directory: Path, taken: Iterable[str] = (), *, settings: Settings | None = None
) -> Loaded:
    """Every tool the `*.py` files in `directory` define, in file order.

    `taken` is the names already in use; a tool that wants one is refused. Files whose
    name starts with `_` are left alone, so a helper or a draft can sit beside the tools.
    `settings` is what `LOADING_SETTINGS` holds while the files load.
    """
    token = LOADING_SETTINGS.set(settings)
    try:
        return _load_directory(directory, taken)
    finally:
        LOADING_SETTINGS.reset(token)


def _load_directory(directory: Path, taken: Iterable[str]) -> Loaded:
    tools: list[tuple[Path, CustomTool]] = []
    errors: list[tuple[Path, str]] = []
    if not directory.is_dir():
        return Loaded(tools, errors)
    if problem := _unsafe(directory):
        return Loaded(tools, [(directory, problem)])

    names = set(taken)
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            found = _load_file(path)
        except ToolUnavailable as exc:
            errors.append((path, str(exc)))
            continue
        except Exception as exc:  # a broken file costs that file
            errors.append((path, f"{type(exc).__name__}: {exc}"))
            continue
        if not found:
            errors.append((path, "defines no tool (decorate a function with @custom_tool)"))
        for tool in found:
            if problem := _invalid(tool, names):
                errors.append((path, problem))
                continue
            names.add(tool.name)
            tools.append((path, tool))
    return Loaded(tools, errors)


def register_custom_tools(registry: ToolRegistry, settings: Settings) -> None:
    """Read `DATA_DIR/tools` into `registry`, beside whatever it already holds."""
    taken = BUILTIN_TOOL_NAMES.union(registry.names())
    loaded = load_custom_tools(settings.custom_tools_dir, taken=taken, settings=settings)
    for path, problem in loaded.errors:
        log.warning("custom tool %s refused: %s", path.name, problem)
    for _, tool in loaded.tools:
        registry.register(
            tool.name,
            tool.description,
            tool.parameters,
            _gated(tool, settings),
            silent=tool.silent,
        )
    if loaded.tools:
        log.info("custom tools: %s", ", ".join(tool.name for _, tool in loaded.tools))


def _unsafe(path: Path) -> str | None:
    """Why loading from `path` would run code somebody else could have written, if so."""
    info = path.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return "owned by another user"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "writable by other users (chmod go-w)"
    return None


def _load_file(path: Path) -> list[CustomTool]:
    """Import `path` afresh and return the tools it defines."""
    if problem := _unsafe(path):
        raise PermissionError(problem)
    module_name = f"{_MODULE_PREFIX}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("not a loadable module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return [value for value in vars(module).values() if isinstance(value, CustomTool)]


def _invalid(tool: CustomTool, taken: set[str]) -> str | None:
    """What is wrong with `tool`, if anything, as a sentence for the log."""
    if not _NAME_RE.fullmatch(tool.name):
        return f"{tool.name!r} is not a valid tool name (letters, digits, _ and -)"
    if tool.name in taken:
        return f"{tool.name!r} is already a tool's name"
    if not tool.description.strip():
        return f"{tool.name!r} has no description"
    if not isinstance(tool.needs_pin, bool):
        return f"{tool.name!r}: needs_pin must be True or False"
    if not isinstance(tool.parameters, dict) or tool.parameters.get("type") != "object":
        return f"{tool.name!r}: parameters must be a JSON-schema object"
    if tool.timeout_s <= 0:
        return f"{tool.name!r}: timeout_s must be positive"
    return None


def _gated(tool: CustomTool, settings: Settings) -> Callable[[ToolContext, dict], Awaitable[dict]]:
    """The registry handler for `tool`: its gate, its timeout, and a JSON-safe result."""

    async def handler(ctx: ToolContext, arguments: dict) -> dict:
        if tool.needs_pin and (refusal := pin_gate(ctx, settings)) is not None:
            return refusal
        try:
            result = await asyncio.wait_for(_invoke(tool.handler, ctx, arguments), tool.timeout_s)
        except TimeoutError:
            log.warning("custom tool %s took longer than %ss", tool.name, tool.timeout_s)
            return {"error": f"{tool.name} did not answer in time"}
        if not isinstance(result, dict):
            result = {"result": result}
        # A value the provider cannot serialise would fail the whole submission.
        return json.loads(json.dumps(result, default=str))

    return handler


async def _invoke(handler: CustomHandler, ctx: ToolContext, arguments: dict) -> Any:
    if inspect.iscoroutinefunction(handler):
        return await handler(ctx, arguments)
    return await asyncio.to_thread(handler, ctx, arguments)
