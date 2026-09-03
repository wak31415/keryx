"""The tools about the call itself: the approval bridge, the PIN, and hanging up.

`answer_approval` is the one to read carefully, and what matters is what it *cannot* do.
It cannot answer anything. The most it does is read the pending question out and put the
keypad menu in the model's mouth; `ApprovalBroker.digit` is the only thing in Jarvis that
can approve a tool call, it is reachable only after the PIN, and an unrecognised key
re-asks rather than agreeing. The keypad decides, never the transcription — a television in
the background cannot press a key.

`submit_pin` hands whatever the caller said straight to the session, which is the only
thing that ever compares a PIN; the digits are neither logged nor kept here.
"""

from jarvis.approvals.broker import ApprovalBroker
from jarvis.config import Settings
from jarvis.tools.builtin_common import (
    APPROVAL_KEYPAD_MESSAGE,
    APPROVAL_NONE_MESSAGE,
    APPROVAL_PHONE_ONLY_MESSAGE,
    ENDING_MESSAGE,
    _small_int,
    pin_gate,
)
from jarvis.tools.registry import ToolContext, ToolRegistry


def register_session_tools(
    registry: ToolRegistry,
    *,
    settings: Settings,
    approvals: ApprovalBroker | None = None,
) -> None:
    """Register the approval tools (only with a broker), `submit_pin` and `end_session`."""
    # --- submit_pin / end_session ------------------------------------------

    # --- approvals ---------------------------------------------------------

    async def list_pending_approvals(ctx: ToolContext, arguments: dict) -> dict:
        assert approvals is not None  # only registered when there is one
        waiting = approvals.pending_requests()
        if not waiting:
            return {"status": "none", "message": APPROVAL_NONE_MESSAGE}
        return {"status": "waiting", "requests": waiting}

    async def answer_approval(ctx: ToolContext, arguments: dict) -> dict:
        """Offer the keypad menu for one pending prompt. It answers nothing by itself.

        Two gates before the menu is even read out: the PIN, exactly as for dispatching
        work — this is a strictly larger capability, so it gets at least the same gate —
        and the phone, because the keypad is where the answer has to come from.
        """
        assert approvals is not None  # only registered when there is one
        refusal = pin_gate(ctx, settings)
        if refusal is not None:
            return refusal
        if ctx.channel != "phone":
            return {"status": "phone_only", "message": APPROVAL_PHONE_ONLY_MESSAGE}
        request_id = _small_int(arguments.get("request_id"))
        if request_id is None:
            return {"error": "request_id must be a request number, for example 1"}
        answer = approvals.arm(request_id, ctx.session.session_id)
        if answer.get("status") == "awaiting_keypad":
            answer["message"] = APPROVAL_KEYPAD_MESSAGE
        return answer

    if approvals is not None:
        registry.register(
            "list_pending_approvals",
            "The prompts Claude Code is waiting on, on his screen. Call it when he asks "
            "what is waiting, or when a call opened because something was.",
            {"type": "object", "properties": {}},
            list_pending_approvals,
        )
        registry.register(
            "answer_approval",
            "Start answering one prompt Claude Code is waiting on. It does not answer "
            "anything: it hands you back the keypad menu for that request, which you read "
            "out, and he decides by pressing a key. Never tell him it is done until the "
            "machine says so — a spoken yes is not an answer, and you must never choose "
            "for him. Needs the PIN and a phone call.",
            {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "integer",
                        "description": "The request number, from the call's opening "
                        "context or list_pending_approvals.",
                    }
                },
                "required": ["request_id"],
            },
            answer_approval,
        )

    # --- submit_pin --------------------------------------------------------

    async def submit_pin(ctx: ToolContext, arguments: dict) -> dict:
        """Hand a spoken PIN to the session; only it ever sees the digits."""
        return await ctx.session.submit_pin(str(arguments.get("pin") or ""))

    registry.register(
        "submit_pin",
        "Check the PIN the caller just said, to unlock dispatching work on the phone. "
        "Pass the digits exactly as you heard them, with nothing else. Never say them back "
        "out loud. The answer is authorized, invalid (with the attempts left) or locked.",
        {
            "type": "object",
            "properties": {
                "pin": {"type": "string", "description": "The digits the caller said."},
            },
            "required": ["pin"],
        },
        submit_pin,
    )

    async def end_session(ctx: ToolContext, arguments: dict) -> dict:
        ctx.session.request_end("user")
        return {"status": "ending", "message": ENDING_MESSAGE}

    registry.register(
        "end_session",
        "Hang up. Say your goodbye first and call this straight afterwards, once the user "
        "has said goodbye or has nothing more to ask: nothing you say after this call is "
        "heard. Never call it while a question is still open.",
        {"type": "object", "properties": {}, "required": []},
        end_session,
    )
