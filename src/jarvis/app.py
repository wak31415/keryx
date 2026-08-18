"""The wiring every entry point shares: one `AppState` per process.

The phone server, the wake-word runner and the task manager all need the same handful
of long-lived objects — settings, the event bus, the live-session registry, the tool
registry, a way to make a realtime provider, the stream-token store, and the task store
and manager behind the tools. Bundling them here keeps `create_app` and the CLI free of
construction logic and gives tests one seam to swap a fake provider (or a fake subagent
runner) in.

`build_app_state` also installs the *interim* task announcer: until the Notifier arrives
in task 11, a finished task is spoken into every live session from here.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis.config import Settings
from jarvis.events import EventBus, TaskCompleted, TaskFailed
from jarvis.realtime.base import RealtimeProvider
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.session import SessionRegistry
from jarvis.stream_tokens import StreamTokenStore
from jarvis.tasks.agent_runner import AgentRunner, ClaudeAgentRunner, FakeAgentRunner
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.store import TaskStore
from jarvis.tools import ToolRegistry
from jarvis.tools.builtin import register_builtin_tools

ProviderFactory = Callable[[], RealtimeProvider]

TASK_DB_NAME = "tasks.db"


@dataclass
class AppState:
    """Everything a running Jarvis process shares.

    `provider_factory` makes one fresh (unconnected) realtime provider per session.
    `notifier` and `twilio_out` are filled in by task 11 and stay None until then;
    `interim_unsubscribe` takes the stop-gap announcer back out of the bus, which is
    how the Notifier will replace it.
    """

    settings: Settings
    bus: EventBus
    sessions: SessionRegistry
    registry: ToolRegistry
    provider_factory: ProviderFactory
    stream_tokens: StreamTokenStore = field(default_factory=StreamTokenStore)
    store: TaskStore | None = None
    manager: TaskManager | None = None
    interim_unsubscribe: Callable[[], None] | None = None
    notifier: Any | None = None
    twilio_out: Any | None = None


def build_app_state(settings: Settings) -> AppState:
    """The production wiring: a real OpenAI provider per session and a live task stack."""
    settings.ensure_dirs()
    bus = EventBus()
    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    manager = TaskManager(store, _build_runner(settings), bus, settings)

    registry = ToolRegistry()
    register_builtin_tools(registry, manager=manager, settings=settings)

    state = AppState(
        settings=settings,
        bus=bus,
        sessions=SessionRegistry(),
        registry=registry,
        provider_factory=lambda: OpenAIRealtimeClient(
            settings.openai_api_key, settings.openai_realtime_model
        ),
        stream_tokens=StreamTokenStore(),
        store=store,
        manager=manager,
    )
    state.interim_unsubscribe = install_interim_announcer(state)
    return state


def _build_runner(settings: Settings) -> AgentRunner:
    """The real Agent SDK runner, or the scripted one behind `--fake-agents`."""
    return FakeAgentRunner() if settings.fake_agents else ClaudeAgentRunner(settings)


def install_interim_announcer(state: AppState) -> Callable[[], None]:
    """Speak finished tasks into every live session; returns a callable that removes it.

    A stop-gap for task 11: the Notifier does this *and* the SMS and the call-back, and
    marks the task announced. Replacing it is `state.interim_unsubscribe()` plus wiring
    the Notifier to the same two events.
    """

    async def announce(text: str) -> None:
        for session in state.sessions.live():
            await session.announce(text)

    async def on_completed(event: TaskCompleted) -> None:
        await announce(f"Task {event.task_id} finished: {event.summary}")

    async def on_failed(event: TaskFailed) -> None:
        await announce(f"Task {event.task_id} failed: {event.error}")

    removals = [
        state.bus.subscribe(TaskCompleted, on_completed),
        state.bus.subscribe(TaskFailed, on_failed),
    ]

    def remove() -> None:
        for unsubscribe in removals:
            unsubscribe()

    return remove


async def shutdown_app_state(state: AppState) -> None:
    """Stop the task manager and close the task store. Safe to call more than once."""
    if state.manager is not None:
        await state.manager.shutdown()
    if state.store is not None:
        await state.store.close()
