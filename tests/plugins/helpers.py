"""What every plugin test shares: a settings object with a PIN, a call, and a registry.

A plugin is tested two ways. Its tool is built straight from `<name>_tool(path, fake=...)`
with a fake client, and called through `offered`, which gates it exactly as a call would;
and the template is installed into a temporary tools directory and loaded the way a call
loads it, with the real builders — none of which reach the network before they are called.
"""

import contextlib
from dataclasses import dataclass

from jarvis import plugins
from jarvis.config import Settings
from jarvis.tools import ToolContext, ToolRegistry
from jarvis.tools.custom import LOADING_SETTINGS, CustomTool, _gated, load_custom_tools
from jarvis.trust import TrustLevel


@dataclass
class StubSession:
    trust: TrustLevel = TrustLevel.FULL
    keypressed: bool = False
    session_id: str = "sess1234"

    @property
    def authorized(self) -> bool:
        return self.trust is TrustLevel.FULL


def offered(tool: CustomTool, settings: Settings) -> ToolRegistry:
    """A registry holding `tool`, gated the way a call's copy would hold it."""
    registry = ToolRegistry()
    registry.register(
        tool.name, tool.description, tool.parameters, _gated(tool, settings), silent=tool.silent
    )
    return registry


async def call(
    registry: ToolRegistry, name: str, args: dict | None = None, *, trust=TrustLevel.FULL
) -> dict:
    ctx = ToolContext(StubSession(trust=trust), "phone", None)
    return await registry.call(name, args or {}, ctx)


def loaded(settings: Settings):
    """What a call would load from the tools directory now."""
    return load_custom_tools(settings.custom_tools_dir, settings=settings)


def refusal(settings: Settings, name: str) -> str | None:
    return next((why for path, why in loaded(settings).errors if path.stem == name), None)


def names(settings: Settings) -> set[str]:
    return {tool.name for _, tool in loaded(settings).tools}


@contextlib.contextmanager
def loading(settings: Settings):
    """Build a plugin's tool as if `settings`' owner were loading the tools directory."""
    token = LOADING_SETTINGS.set(settings)
    try:
        yield
    finally:
        LOADING_SETTINGS.reset(token)


def turn_on(settings: Settings, name: str, **values) -> None:
    """Write the plugin's settings and install it, the way `jarvis plugins install` does."""
    plugins.write_config(settings, name, values)
    plugins.install(settings, name)
