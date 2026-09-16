"""The two tools that reach outside the call without dispatching anything.

`send_to_slack` is the written channel he actually asks for (Jarvis does not text — see
`SMS_ENABLED`), and registering it only makes it *available*: whether it may be called is
the model's decision, and both its description and the system prompt confine that to the
turns where he explicitly asked for something in writing.

`web_search` is the whole of the "answer it myself" half of the one routing decision Jarvis
makes. It goes through the Responses API because a Realtime session has no hosted search
tool of its own.
"""

from jarvis.config import Settings
from jarvis.integrations.slack import SlackSender
from jarvis.integrations.web_search import WebSearcher
from jarvis.tools.builtin_common import (
    SEARCH_FAILED_MESSAGE,
    SLACK_FAILED_MESSAGE,
    _text,
    pin_gate,
)
from jarvis.tools.registry import ToolContext, ToolRegistry


def register_comms_tools(
    registry: ToolRegistry,
    *,
    settings: Settings,
    slack: SlackSender | None = None,
    searcher: WebSearcher | None = None,
) -> None:
    """Register `send_to_slack` and `web_search`, each only when it has something behind it."""
    # --- send_to_slack -----------------------------------------------------

    async def send_to_slack(ctx: ToolContext, arguments: dict) -> dict:
        # Not before the PIN: it posts as his own bot, into the channel he trusts.
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
            "he already uses for this. Only call it when he has explicitly asked for "
            'something in writing — "send me that", "put it on Slack", "text me the link". '
            "Never call it unasked, however awkward the content is to say out loud, and never "
            "to repeat in writing something you have already said; if it truly will not "
            "survive being spoken, offer to send it and call this only once he accepts. "
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
            "announced — instead of dispatching a task. Anything that needs his files, "
            "his repositories, his mail, or more than a couple of sentences of work goes "
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
