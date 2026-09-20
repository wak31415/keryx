"""The tools the voice model calls to get real work done (spec §3.2, §3.3).

Everything here is written for a model that is *speaking*: descriptions say when to
reach for a tool, ids are small integers, lists are short by default, and long text is
cut down before it ever reaches a text-to-speech engine. Handlers never raise — a
problem comes back as `{"error": ...}` (or a `status` the model is told how to relay),
so a bad task id is a sentence the assistant can say rather than a dropped call.

The PIN gate guards everything that *acts* on the phone: it is refused with
`{"status": "pin_required"}` until the session is authorized. Caller id is spoofable, so
nothing the caller says or does outlives the call before the PIN. Reading is the other
half and is not gated the same way — the four tools over what the standing briefing
already carries follow it (`read_gate`, `BRIEFING_BEFORE_PIN`), and five more answer at
any level at all (`builtin_common` says why, and why `recall` is in neither group). The
check reads `ctx.trust` live, so a PIN entered on the keypad while the model was thinking
is honoured on the very next call. The digits themselves never pass through here: the
`submit_pin` tool hands whatever the caller said straight to the session, which is the
only thing that ever compares it.

The registrations themselves live in five modules by domain — `builtin_comms`,
`builtin_billing`, `builtin_tasks`, `builtin_restart` and `builtin_session` — with
everything they share in `builtin_common`. This file is the composition root: it decides
what each of them gets, and the order they are called in *is* the order the tools are
offered to the model, so it is the order they were registered in before the split
(2026-09-02).
"""

from jarvis.approvals.broker import ApprovalBroker
from jarvis.config import Settings
from jarvis.continuity.recall import Recaller
from jarvis.inline_waits import InlineWaits
from jarvis.integrations.cluster import ClusterQuerier
from jarvis.integrations.slack import SlackSender
from jarvis.integrations.web_search import WebSearcher
from jarvis.restart.coordinator import RestartCoordinator
from jarvis.tasks.manager import TaskManager
from jarvis.tools.builtin_billing import register_billing_tools
from jarvis.tools.builtin_common import BillingFactory
from jarvis.tools.builtin_comms import register_comms_tools
from jarvis.tools.builtin_restart import register_restart_tools
from jarvis.tools.builtin_session import register_session_tools
from jarvis.tools.builtin_tasks import register_task_tools
from jarvis.tools.registry import ToolRegistry


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    manager: TaskManager,
    settings: Settings,
    inline_waits: InlineWaits,
    searcher: WebSearcher | None = None,
    slack: SlackSender | None = None,
    restarter: RestartCoordinator | None = None,
    recaller: Recaller | None = None,
    approvals: ApprovalBroker | None = None,
    billing: BillingFactory | None = None,
    cluster: ClusterQuerier | None = None,
) -> None:
    """Register every tool the voice model has, bound to this process's task manager.

    `web_search`, `send_to_slack`, `restart_service`, `recall`, `check_billing`,
    `cluster_stats` and the two approval tools are registered only when a `searcher` /
    `slack` / `restarter` / `recaller` / `billing` / `cluster` / `approvals` is supplied,
    so a process without one simply does not offer that tool. Registering
    `send_to_slack` only makes it *available*: whether it may be called is the voice
    model's decision, and both its description and the system prompt confine that to the
    turns where the owner explicitly asked for something on Slack.
    """
    register_comms_tools(registry, settings=settings, slack=slack, searcher=searcher)
    register_billing_tools(registry, billing=billing, cluster=cluster)
    register_task_tools(
        registry,
        manager=manager,
        settings=settings,
        inline_waits=inline_waits,
        recaller=recaller,
    )
    register_restart_tools(registry, settings=settings, restarter=restarter)
    register_session_tools(registry, settings=settings, approvals=approvals)
