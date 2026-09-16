"""`restart_service`: the voice model asking Jarvis to restart itself.

PIN-gated like a dispatch, because it takes the phone channel off the air. It never
restarts during the call — `RestartCoordinator.request` waits for the line to clear, since
a restart drops every call in progress, including the one that asked for it.
"""

from jarvis.config import Settings
from jarvis.restart.coordinator import RestartCoordinator
from jarvis.tools.builtin_common import _task_id, _text, pin_gate
from jarvis.tools.registry import ToolContext, ToolRegistry


def register_restart_tools(
    registry: ToolRegistry,
    *,
    settings: Settings,
    restarter: RestartCoordinator | None = None,
) -> None:
    """Register `restart_service`, only when there is a coordinator to do it."""
    # --- restart_service ---------------------------------------------------

    if restarter is not None:

        async def restart_service(ctx: ToolContext, arguments: dict) -> dict:
            """Restart the service, and let it phone back when it is up (spec §3.3)."""
            refusal = pin_gate(ctx, settings)
            if refusal is not None:
                return refusal
            # Only ever a number we already trust: the one calling, or the owner's. A
            # restart is not a way to make Jarvis dial a stranger.
            return await restarter.request(
                reason=_text(arguments, "reason"),
                number=ctx.caller if ctx.channel == "phone" else None,
                origin_channel=ctx.channel,
                origin_session_id=ctx.session.session_id,
                task_id=_task_id(arguments),
            )

        registry.register(
            "restart_service",
            "Restart Jarvis itself — the service behind this call — when they ask for one, "
            "or when work they asked for has changed Jarvis's own code and only a restart "
            "loads it. The restart drops this call, so it waits until the call has ended "
            "and then rings them back by itself to say whether it worked; the answer tells "
            "you what to say. Never reach for it to fix something you were not asked to fix.",
            {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why it is being restarted, in a few words — they "
                        "hear this back on the confirmation call.",
                    },
                    "task_id": {
                        "type": "integer",
                        "description": "The task whose change this restart is loading, if "
                        "it is loading one. Pass it: the confirmation then checks that the "
                        "change is actually running rather than only that Jarvis came back.",
                    },
                },
                "required": [],
            },
            restart_service,
        )
