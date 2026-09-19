"""The tools about the call itself: the approval bridge, the PIN, and hanging up.

`answer_approval` is the one to read carefully, and what matters is what it *cannot* do.
It cannot answer anything. The most it does is read the pending question out and put the
keypad menu in the model's mouth; `ApprovalBroker.digit` is the only thing in Jarvis that
can approve a tool call, it is reachable only after the PIN, and an unrecognised key
re-asks rather than agreeing. The keypad decides, never the transcription — a television in
the background cannot press a key.

`submit_pin` hands whatever the caller said straight to the session, which is the only
thing that ever compares a PIN; the digits are neither logged nor kept here. What it adds
on the way back is only wording: the session answers `authorized` / `invalid` / `locked` /
`not_configured`, and each of those gets the one sentence the model should spend on it. A
PIN used to cost four spoken turns on a real call and carry one fact; the messages say
what not to say as firmly as what to say.
"""

from jarvis.approvals.broker import ApprovalBroker
from jarvis.config import Settings
from jarvis.tools.builtin_common import (
    APPROVAL_KEYPAD_MESSAGE,
    APPROVAL_NONE_MESSAGE,
    APPROVAL_PHONE_ONLY_MESSAGE,
    ENDING_MESSAGE,
    PIN_INVALID_MESSAGE,
    PIN_LOCKED_MESSAGE,
    PIN_NOT_CONFIGURED_MESSAGE,
    PIN_OK_MESSAGE,
    _small_int,
    possession_gate,
)
from jarvis.tools.registry import ToolContext, ToolRegistry

#: The sentence to hand back for each `VoiceSession.submit_pin` status. Wording only: the
#: statuses themselves, and the lockout behind `locked`, are the session's business.
PIN_MESSAGES = {
    "authorized": PIN_OK_MESSAGE,
    "invalid": PIN_INVALID_MESSAGE,
    "locked": PIN_LOCKED_MESSAGE,
    "not_configured": PIN_NOT_CONFIGURED_MESSAGE,
}


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
        # What their screen is waiting on names their projects and their commands, so a
        # call that has proved nothing does not hear it. A call Jarvis placed does: it is
        # usually the call the broker placed *about* one.
        if (refusal := possession_gate(ctx, settings, keypress=False)) is not None:
            return refusal
        waiting = approvals.pending_requests()
        if not waiting:
            return {"status": "none", "message": APPROVAL_NONE_MESSAGE}
        return {"status": "waiting", "requests": waiting}

    async def answer_approval(ctx: ToolContext, arguments: dict) -> dict:
        """Offer the keypad menu for one pending prompt. It answers nothing by itself.

        Two gates before the menu is even read out. Possession, because a call Jarvis
        placed to the owner's own number is the call this feature exists to make, and
        `approvals/policy.py`'s allowlist is already the "routine and reversible" filter
        on what a keypad may ever run — the denylist still wins over it, and `--disable`
        wins over everything. And the phone, because the keypad is where the answer has
        to come from. No keypress ack: the answer *is* a keypress, and an answering
        machine that cannot press one cannot approve anything either.
        """
        assert approvals is not None  # only registered when there is one
        refusal = possession_gate(ctx, settings, keypress=False)
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
            "The prompts Claude Code is waiting on, on the owner's screen. Call it when they ask "
            "what is waiting, or when a call opened because something was.",
            {"type": "object", "properties": {}},
            list_pending_approvals,
        )
        registry.register(
            "answer_approval",
            "Start answering one prompt Claude Code is waiting on. It does not answer "
            "anything: it hands you back the keypad menu for that request, which you read "
            "out, and they decide by pressing a key. Never tell them it is done until the "
            "machine says so — a spoken yes is not an answer, and you must never choose "
            "for them. Needs a phone call, and the PIN unless Jarvis rang them.",
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
        """Hand a spoken PIN to the session; only it ever sees the digits.

        The answer comes back as the session wrote it, plus the sentence for that status.
        A `message` the session already set wins, so nothing here can talk over it.
        """
        result = await ctx.session.submit_pin(str(arguments.get("pin") or ""))
        message = PIN_MESSAGES.get(str(result.get("status")))
        if message is not None:
            result.setdefault("message", message)
        return result

    registry.register(
        "submit_pin",
        "Check the PIN the caller just said, to unlock dispatching work on the phone. "
        "Pass the digits exactly as you heard them, with nothing else. Never say them back "
        "out loud, and do not announce that you are checking — it answers at once. The "
        "answer is authorized, invalid (with the attempts left) or locked; when it is "
        "authorized, say nothing about the PIN and carry straight on with their request.",
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
        # Silent: the goodbye came before the call, and a turn generated over this answer
        # is a second goodbye racing a hangup that is already under way.
        silent=True,
    )
