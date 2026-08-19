"""The wiring every entry point shares: one `AppState` per process.

The phone server, the wake-word runner and the task manager all need the same handful
of long-lived objects — settings, the event bus, the live-session registry, the tool
registry, a way to make a realtime provider, the stream-token store, and the task store
and manager behind the tools. Bundling them here keeps `create_app` and the CLI free of
construction logic and gives tests one seam to swap a fake provider (or a fake subagent
runner) in.

`build_app_state` also starts the Notifier, which is what turns a finished task into
something the user actually hears: an announcement into the live sessions, a text, or a
call back (spec §3.3).
"""

from dataclasses import dataclass, field

from jarvis.config import Settings
from jarvis.events import EventBus
from jarvis.inline_waits import InlineWaits
from jarvis.notify.notifier import Notifier
from jarvis.notify.twilio_out import TwilioOut
from jarvis.realtime.base import ProviderFactory
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.session import SessionRegistry
from jarvis.stream_tokens import StreamTokenStore
from jarvis.tasks.agent_runner import AgentRunner, ClaudeAgentRunner, FakeAgentRunner
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.store import TaskStore
from jarvis.tools import ToolRegistry
from jarvis.tools.builtin import register_builtin_tools

TASK_DB_NAME = "tasks.db"


@dataclass
class AppState:
    """Everything a running Jarvis process shares.

    `provider_factory` makes one fresh (unconnected) realtime provider per session.
    `twilio_out` exists even without Twilio credentials — its `configured` flag is what
    says whether anything can actually be sent.
    """

    settings: Settings
    bus: EventBus
    sessions: SessionRegistry
    registry: ToolRegistry
    provider_factory: ProviderFactory
    stream_tokens: StreamTokenStore = field(default_factory=StreamTokenStore)
    inline_waits: InlineWaits = field(default_factory=InlineWaits)
    store: TaskStore | None = None
    manager: TaskManager | None = None
    notifier: Notifier | None = None
    twilio_out: TwilioOut | None = None


def build_app_state(settings: Settings) -> AppState:
    """The production wiring: a real OpenAI provider per session and a live task stack."""
    settings.ensure_dirs()
    bus = EventBus()
    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    manager = TaskManager(store, _build_runner(settings), bus, settings)

    registry = ToolRegistry()
    inline_waits = InlineWaits()
    register_builtin_tools(
        registry, manager=manager, settings=settings, inline_waits=inline_waits
    )

    state = AppState(
        settings=settings,
        bus=bus,
        sessions=SessionRegistry(),
        registry=registry,
        provider_factory=lambda: OpenAIRealtimeClient(
            settings.openai_api_key, settings.openai_realtime_model
        ),
        stream_tokens=StreamTokenStore(),
        inline_waits=inline_waits,
        store=store,
        manager=manager,
    )
    state.twilio_out = TwilioOut(settings)
    state.notifier = Notifier(
        bus,
        store,
        state.sessions,
        state.twilio_out,
        settings,
        state.stream_tokens,
        state.inline_waits,
    )
    state.notifier.start()
    return state


def _build_runner(settings: Settings) -> AgentRunner:
    """The real Agent SDK runner, or the scripted one behind `--fake-agents`."""
    return FakeAgentRunner() if settings.fake_agents else ClaudeAgentRunner(settings)


async def shutdown_app_state(state: AppState) -> None:
    """Take the notifier off the bus, stop the manager, close the store. Idempotent."""
    if state.notifier is not None:
        state.notifier.stop()
    if state.manager is not None:
        await state.manager.shutdown()
    if state.store is not None:
        await state.store.close()
