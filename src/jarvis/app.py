"""The wiring every entry point shares: one `AppState` per process.

The phone server, the wake-word runner and (from task 9 on) the task manager all need
the same handful of long-lived objects — settings, the event bus, the live-session
registry, the tool registry, a way to make a realtime provider and the stream-token
store. Bundling them here keeps `create_app` and the CLI free of construction logic and
gives tests one seam to swap a fake provider in.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis.config import Settings
from jarvis.events import EventBus
from jarvis.realtime.base import RealtimeProvider
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.session import SessionRegistry
from jarvis.stream_tokens import StreamTokenStore
from jarvis.tools import ToolRegistry

ProviderFactory = Callable[[], RealtimeProvider]


@dataclass
class AppState:
    """Everything a running Jarvis process shares.

    `provider_factory` makes one fresh (unconnected) realtime provider per session.
    The trailing fields are filled in by later tasks — the task store and manager, the
    notifier and the outbound Twilio client — and stay None until then.
    """

    settings: Settings
    bus: EventBus
    sessions: SessionRegistry
    registry: ToolRegistry
    provider_factory: ProviderFactory
    stream_tokens: StreamTokenStore = field(default_factory=StreamTokenStore)
    store: Any | None = None
    manager: Any | None = None
    notifier: Any | None = None
    twilio_out: Any | None = None


def build_app_state(settings: Settings) -> AppState:
    """The production wiring: a real OpenAI provider per session, empty registries."""
    return AppState(
        settings=settings,
        bus=EventBus(),
        sessions=SessionRegistry(),
        registry=ToolRegistry(),  # task 10 fills this with the real tools
        provider_factory=lambda: OpenAIRealtimeClient(
            settings.openai_api_key, settings.openai_realtime_model
        ),
        stream_tokens=StreamTokenStore(),
    )
