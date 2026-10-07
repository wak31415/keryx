"""`web_search`: the one tool that reaches outside the call without dispatching anything.

It is the whole of the "answer it myself" half of the one routing decision Keryx makes,
so it is core, not a plugin. A Realtime session has no hosted search tool of its own, so
it goes through whichever backend `WEB_SEARCH` resolves to (`integrations/web_search.py`).

The optional tools that also reach outside — Slack, email, billing, cluster stats — are
plugins (`keryx.plugins`), installed into the owner's tools directory when wanted.
"""

from keryx.integrations.web_search import WebSearcher
from keryx.tools.builtin_common import SEARCH_FAILED_MESSAGE, SEARCH_RESULTS_MESSAGE, _text
from keryx.tools.registry import ToolContext, ToolRegistry


def register_comms_tools(registry: ToolRegistry, *, searcher: WebSearcher | None = None) -> None:
    """Register `web_search`, only when there is a searcher behind it."""

    async def web_search(ctx: ToolContext, arguments: dict) -> dict:
        query = _text(arguments, "query")
        if not query:
            return {"error": "query is required: say what to look up"}
        assert searcher is not None  # only registered when there is one
        found = await searcher.search(query)
        if not found:
            return {"error": SEARCH_FAILED_MESSAGE}
        if "results" in found:
            return {**found, "instructions": SEARCH_RESULTS_MESSAGE}
        return found

    if searcher is not None:
        registry.register(
            "web_search",
            "Look something up on the web: you get a short answer, or a few results to answer "
            "from. Use it yourself for small, factual questions — a price, a date, a score, "
            "what a company announced — instead of dispatching a task. Anything that needs "
            "their files, "
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
