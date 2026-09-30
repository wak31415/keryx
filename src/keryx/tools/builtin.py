"""The tools the voice model calls to get real work done.

Everything here is written for a model that is *speaking*: descriptions say when to
reach for a tool, ids are small integers, lists are short by default, and long text is
cut down before it ever reaches a text-to-speech engine. Handlers never raise — a
problem comes back as `{"error": ...}` (or a `status` the model is told how to relay),
so a bad task id is a sentence the assistant can say rather than a dropped call.

The PIN gate guards everything that *acts* on the phone: it is refused with
`{"status": "pin_required"}` until the session is authorized. Caller id is spoofable, so
nothing the caller says or does outlives the call before the PIN. Reading is the other
half and is not gated the same way — the four tools over what the standing briefing
already carries follow it (`read_gate`, `BRIEFING_BEFORE_PIN`), and three more answer at
any level at all (`builtin_common` says why, and why `recall` is in neither group). The
check reads `ctx.trust` live, so a PIN entered on the keypad while the model was thinking
is honoured on the very next call. The digits themselves never pass through here: the
`submit_pin` tool hands whatever the caller said straight to the session, which is the
only thing that ever compares it.

The registrations themselves live in four modules by domain — `builtin_comms`,
`builtin_tasks`, `builtin_restart` and `builtin_session` — with everything they share in
`builtin_common`. This file is the composition root: it decides what each of them gets,
and the order they are called in *is* the order the tools are offered to the model, so it
is the order they were registered in before the split (2026-09-02).

The optional tools — `send_to_slack`, `check_email`, `check_billing`, `cluster_stats` —
are not here: they are plugins (`keryx.plugins`), custom tools the owner turns on, loaded
after these at the top of every call.
"""

from collections.abc import Sequence

from keryx.approvals.broker import ApprovalBroker
from keryx.config import Settings
from keryx.continuity.recall import Recaller
from keryx.inline_waits import InlineWaits
from keryx.integrations.web_search import WebSearcher
from keryx.restart.coordinator import RestartCoordinator
from keryx.tasks.manager import TaskManager
from keryx.tools.builtin_comms import register_comms_tools
from keryx.tools.builtin_restart import register_restart_tools
from keryx.tools.builtin_session import register_session_tools
from keryx.tools.builtin_tasks import register_task_tools
from keryx.tools.registry import ToolRegistry

#: Every name a built-in tool is ever registered under, offered on this machine or not. The
#: owner's own tools may take none of them (`keryx.tools.custom`), so a restart or a search
#: turned on later can never collide with one of theirs. `tests/test_docs_sync.py` holds
#: this to the `registry.register(...)` calls in the source.
BUILTIN_TOOL_NAMES = frozenset({
    "web_search", "dispatch_task", "list_tasks", "get_task_status", "get_task_result",
    "mark_reported", "recall", "send_followup", "cancel_task", "list_projects", "request_callback",
    "restart_service", "list_pending_approvals", "answer_approval", "set_config",
    "submit_pin", "end_session",
})


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    manager: TaskManager,
    settings: Settings,
    inline_waits: InlineWaits,
    searcher: WebSearcher | None = None,
    restarter: RestartCoordinator | None = None,
    recaller: Recaller | None = None,
    approvals: ApprovalBroker | None = None,
    agents: Sequence[str] | None = None,
) -> None:
    """Register every tool the voice model has, bound to this process's task manager.

    `agents` is the coding agents `dispatch_task` may name, the default first; None is the
    default alone, which leaves the tool without an `agent` parameter.

    `web_search`, `restart_service`, `recall` and the two approval tools are registered
    only when a `searcher` / `restarter` / `recaller` / `approvals` is supplied, so a
    process without one simply does not offer that tool.
    """
    register_comms_tools(registry, searcher=searcher)
    register_task_tools(
        registry,
        manager=manager,
        settings=settings,
        inline_waits=inline_waits,
        recaller=recaller,
        agents=agents,
    )
    register_restart_tools(registry, settings=settings, restarter=restarter)
    register_session_tools(registry, settings=settings, approvals=approvals)
