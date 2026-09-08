"""The wiring every entry point shares: one `AppState` per process.

The phone server, the wake-word runner and the task manager all need the same handful
of long-lived objects — settings, the event bus, the live-session registry, the tool
registry, a way to make a realtime provider, the stream-token store, and the task store
and manager behind the tools. Bundling them here keeps `create_app` and the CLI free of
construction logic and gives tests one seam to swap a fake provider (or a fake subagent
runner) in.

It also builds the `ApprovalBroker`, which is the other direction entirely: not Jarvis
telling him what a subagent did, but a Claude Code session on his own screen that has been
waiting on him and has given up expecting an answer at the keyboard. It is built here so
the voice tools can bind to it and started only by `jarvis serve`, because starting it
binds a Unix socket and two processes cannot both own that.

`build_app_state` also starts the Notifier, which is what turns a finished task into
something the user actually hears: an announcement into the live sessions, a text, or a
call back (spec §3.3), and builds the `RestartCoordinator` that does the same for a
restart of the service itself.

It starts the `MemoryWriter` on the same bus, which is the other half of continuity: the
Notifier carries a result *outwards* while somebody is listening, and the MemoryWriter
writes down what happened so the *next* call opens knowing it. What neither route
delivered is what `Briefer` puts at the top of the next call.
"""

from dataclasses import dataclass, field

from jarvis.approvals.broker import ApprovalBroker
from jarvis.briefing import Briefer
from jarvis.config import Settings
from jarvis.events import EventBus
from jarvis.inline_waits import InlineWaits
from jarvis.integrations.billing import build_billing_reader
from jarvis.integrations.cluster import build_cluster_stats
from jarvis.integrations.slack import SlackWebApi, slack_credentials
from jarvis.integrations.web_search import OpenAIWebSearch
from jarvis.memory import MemoryWriter
from jarvis.notify.notifier import Notifier
from jarvis.notify.twilio_out import TwilioOut
from jarvis.realtime.base import ProviderFactory
from jarvis.realtime.openai import OpenAIRealtimeClient
from jarvis.recall import Recaller
from jarvis.restart.coordinator import RestartCoordinator
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
    restart: RestartCoordinator | None = None
    #: Built per session by whoever opens one, so a call knows what it was never told.
    briefer: Briefer | None = None
    memory: MemoryWriter | None = None
    #: Built here so the tools can bind to it, but it binds no socket until `start()` —
    #: which only `jarvis serve` calls, so a CLI command never takes the bridge over.
    approvals: ApprovalBroker | None = None


def build_app_state(settings: Settings) -> AppState:
    """The production wiring: a real OpenAI provider per session and a live task stack."""
    settings.ensure_dirs()
    bus = EventBus()
    store = TaskStore(settings.data_dir / TASK_DB_NAME)
    # The real Agent SDK runner, or the scripted one behind `--fake-agents`.
    runner: AgentRunner = FakeAgentRunner() if settings.fake_agents else ClaudeAgentRunner(settings)
    manager = TaskManager(store, runner, bus, settings)

    registry = ToolRegistry()
    inline_waits = InlineWaits()
    # Built before the tools, because `restart_service` is bound to the coordinator the
    # way the task tools are bound to the manager; the outbound half is the Notifier's.
    sessions = SessionRegistry()
    stream_tokens = StreamTokenStore()
    twilio_out = TwilioOut(settings)
    restart = RestartCoordinator(settings, sessions, twilio_out, stream_tokens, store)
    approvals = ApprovalBroker(settings, sessions, twilio_out, stream_tokens)

    # No Slack app configured anywhere is not an error: the tool is simply not offered.
    credentials = slack_credentials(settings.slack_bot_token, settings.slack_channel_id)
    slack = SlackWebApi(*credentials) if credentials else None
    register_builtin_tools(
        registry,
        manager=manager,
        settings=settings,
        inline_waits=inline_waits,
        searcher=OpenAIWebSearch(settings.openai_api_key, settings.openai_web_search_model),
        slack=slack,
        # A factory, not a reader: the model may ask for either provider on any call, and
        # "no admin key for that one" is a `BillingError` the tool speaks rather than a
        # missing tool. Nothing is built or contacted until it is actually asked for.
        billing=lambda provider: build_billing_reader(settings, provider),
        # Built unconditionally, and contacts nothing until it is asked: a missing ssh
        # guard is a sentence the tool speaks, not a tool that silently is not there.
        cluster=build_cluster_stats(settings),
        restarter=restart,
        recaller=Recaller(settings.data_dir, manager),
        approvals=approvals,
    )

    state = AppState(
        settings=settings,
        bus=bus,
        sessions=sessions,
        registry=registry,
        provider_factory=lambda: OpenAIRealtimeClient(
            settings.openai_api_key, settings.openai_realtime_model
        ),
        stream_tokens=stream_tokens,
        inline_waits=inline_waits,
        store=store,
        manager=manager,
    )
    state.twilio_out = twilio_out
    state.restart = restart
    state.approvals = approvals
    state.briefer = Briefer(settings, manager)
    state.memory = MemoryWriter(bus, manager, settings)
    state.memory.start()
    state.notifier = Notifier(
        bus,
        store,
        sessions,
        twilio_out,
        settings,
        stream_tokens,
        inline_waits,
        restart,
    )
    state.notifier.start()
    return state


async def shutdown_app_state(state: AppState) -> None:
    """Take the bus subscribers off, stop the manager, close the store. Idempotent."""
    if state.notifier is not None:
        state.notifier.stop()
    if state.memory is not None:
        state.memory.stop()
    if state.approvals is not None:
        await state.approvals.stop()
    if state.restart is not None:
        await state.restart.shutdown()
    if state.manager is not None:
        await state.manager.shutdown()
    if state.store is not None:
        await state.store.close()
