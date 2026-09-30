"""`web_search`: the one tool that reaches outside the call without dispatching anything.

It is the whole of the "answer it myself" half of the one routing decision Keryx makes,
so it is core, not a plugin. It goes through the Responses API because a Realtime session
has no hosted search tool of its own.

The optional tools that also reach outside — Slack, email, billing, cluster stats — are
plugins (`keryx.plugins`), installed into the owner's tools directory when wanted.
"""

from keryx.integrations.web_search import WebSearcher
from keryx.tools.builtin_common import SEARCH_FAILED_MESSAGE, _text
from keryx.tools.registry import ToolContext, ToolRegistry


def register_comms_tools(registry: ToolRegistry, *, searcher: WebSearcher | None = None) -> None:
    """Register `web_search`, only when there is a searcher behind it."""

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
