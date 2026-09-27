"""The three tools that reach outside the call without dispatching anything.

`send_to_slack` is the written channel they actually ask for (Jarvis does not text — see
`SMS_ENABLED`), and registering it only makes it *available*: whether it may be called is
the model's decision, and both its description and the system prompt confine that to the
turns where they explicitly asked for something in writing.

`web_search` is the whole of the "answer it myself" half of the one routing decision Jarvis
makes. It goes through the Responses API because a Realtime session has no hosted search
tool of its own.

`check_email` answers a question about their email — a whole day, or a search — from one
Gmail pass and one model call (`jarvis/integrations/gmail.py`), rather than a subagent
reading the inbox for minutes.
It needs the PIN, as `recall` does: the mail is not in the briefing, and the question
steers what is read.
"""

from jarvis.config import Settings
from jarvis.integrations.gmail import EmailError, EmailReader
from jarvis.integrations.slack import SlackSender
from jarvis.integrations.web_search import WebSearcher
from jarvis.tools.builtin_common import (
    SEARCH_FAILED_MESSAGE,
    SLACK_FAILED_MESSAGE,
    _text,
    log,
    pin_gate,
)
from jarvis.tools.registry import ToolContext, ToolRegistry


def register_comms_tools(
    registry: ToolRegistry,
    *,
    settings: Settings,
    slack: SlackSender | None = None,
    searcher: WebSearcher | None = None,
    email: EmailReader | None = None,
) -> None:
    """Register `send_to_slack`, `web_search` and `check_email`, each only when it has
    something behind it."""
    # --- send_to_slack -----------------------------------------------------

    async def send_to_slack(ctx: ToolContext, arguments: dict) -> dict:
        # Not before the PIN: it posts as their own bot, into the channel they trust.
        if (refusal := pin_gate(ctx, settings)) is not None:
            return refusal
        message = _text(arguments, "message")
        if not message:
            return {"error": "message is required: say what to send"}
        assert slack is not None  # only registered when there is one
        if not await slack.send(message):
            return {"error": SLACK_FAILED_MESSAGE}
        return {"status": "sent"}

    if slack is not None:
        registry.register(
            "send_to_slack",
            f"Send {settings.owner_label} a message on Slack, in the direct-message channel "
            "they already use for this. Only call it when they have explicitly asked for "
            'something in writing — "send me that", "Slack me that", "put it on Slack", '
            '"text me the link". Never call it unasked, however awkward the content is to say '
            "out loud, and never volunteer a written copy of something you have already said; "
            "but when they ask for what you just said in writing, that is exactly what to "
            "send, with this tool. If something truly will not survive being spoken, offer to "
            "send it and call this only once they accept. "
            "For anything a subagent produced (a file, a plot, a report), dispatch the "
            "sending to Claude instead: it can attach the file itself.",
            {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send, written to be read rather "
                        "than heard.",
                    }
                },
                "required": ["message"],
            },
            send_to_slack,
        )

    # --- web_search --------------------------------------------------------

    async def web_search(ctx: ToolContext, arguments: dict) -> dict:
        query = _text(arguments, "query")
        if not query:
            return {"error": "query is required: say what to look up"}
        assert searcher is not None  # only registered when there is one
        answer = await searcher.search(query)
        if not answer:
            return {"error": SEARCH_FAILED_MESSAGE}
        return {"answer": answer}

    if searcher is not None:
        registry.register(
            "web_search",
            "Look something up on the web and get a short spoken answer. Use it yourself "
            "for small, factual questions — a price, a date, a score, what a company "
            "announced — instead of dispatching a task. Anything that needs their files, "
            "their repositories, their mail, or more than a couple of sentences of work goes "
            "to dispatch_task instead.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look up, as a full question.",
                    }
                },
                "required": ["query"],
            },
            web_search,
        )

    # --- check_email -------------------------------------------------------

    async def check_email(ctx: ToolContext, arguments: dict) -> dict:
        # Behind the PIN: the mail is not in the briefing, and the question steers the read.
        if (refusal := pin_gate(ctx, settings)) is not None:
            return refusal
        assert email is not None  # only registered when there is one
        question = _text(arguments, "question")
        query = _text(arguments, "gmail_query") or None
        day = _text(arguments, "day").lower() or None
        if day not in (None, "today", "yesterday"):
            return {"error": "day is today or yesterday; for anything else, use gmail_query"}
        if not (question or query or day):
            return {"error": "question is required: say what they want to know"}
        try:
            return await email.ask(question, query=query, day=day)
        except EmailError as exc:
            log.warning("check_email failed: %s (%s)", exc.code, exc.detail)
            return {"status": exc.code, "message": exc.spoken}

    if email is not None:
        registry.register(
            "check_email",
            "Answer a question about their email in about five seconds, rather than "
            'dispatching it: "anything I need to do from today\'s email", "what came in '
            'yesterday", "did Susan answer about the kickoff", "when is the camera-ready '
            'due", "anything from the bank this week". With day it reads that whole day, one '
            "entry per thread with the threads they already answered left out; without it, "
            "it searches Gmail and reads the few newest matches in full. Say \"one moment\", "
            "then say the answer as it comes back and stop. Replying, attachments, and "
            "anything that needs more than a handful of emails go to dispatch_task instead.",
            {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What they asked, in their words.",
                    },
                    "gmail_query": {
                        "type": "string",
                        "description": "Gmail search terms for what they asked about: "
                        "from:name, subject:word, is:unread, newer_than:7d, or plain words. "
                        "Leave it out to search on the question itself, or with day for "
                        "everything that day.",
                    },
                    "day": {
                        "type": "string",
                        "enum": ["today", "yesterday"],
                        "description": "Only when they asked about a whole day's email "
                        '("what do I need to do from today\'s email"). Leave it out for a '
                        "question about something in particular.",
                    },
                },
                "required": ["question"],
            },
            check_email,
        )
