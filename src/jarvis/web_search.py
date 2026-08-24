"""Web search for the voice model itself, so a small question needs no subagent.

The Realtime API has no hosted search tool — a realtime session accepts only `function`
and `mcp` tools (verified 2026-08-24) — so search is a function tool we answer ourselves,
by asking the Responses API, which does have one. That keeps the two-way split William
wants: the voice answers what it can, and hands everything else to Claude.

`WebSearcher` is the seam the tests use: no test ever reaches the network.
"""

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Protocol

log = logging.getLogger("jarvis.web_search")

RESPONSES_URL = "https://api.openai.com/v1/responses"
#: Search answers are spoken, so they are asked for short and stripped of anything
#: text-to-speech would mangle.
SEARCH_INSTRUCTIONS = (
    "Answer in at most two short spoken sentences. Plain speech: no markdown, no lists, "
    "no URLs, no citations. If the answer is a number or a date, say it plainly. If the "
    "search turns up nothing solid, say so."
)
REQUEST_TIMEOUT_S = 30.0
MAX_ANSWER_CHARS = 600

#: Citations come back as `([example.com](https://…))` however firmly we ask for none.
_CITATION_RE = re.compile(r"\s*\(?\[[^\]]*\]\([^)]*\)\)?")
_URL_RE = re.compile(r"https?://\S+")
_MARKDOWN_RE = re.compile(r"[*_`#]+")


class WebSearcher(Protocol):
    """Anything that can answer a spoken question from the web."""

    async def search(self, query: str) -> str:
        """A short spoken answer, or a sentence saying why there is none."""
        ...


def speakable(text: str) -> str:
    """The answer with the things a voice cannot read stripped out."""
    without_citations = _CITATION_RE.sub("", text)
    without_urls = _URL_RE.sub("", without_citations)
    collapsed = " ".join(_MARKDOWN_RE.sub("", without_urls).split())
    if len(collapsed) > MAX_ANSWER_CHARS:
        collapsed = collapsed[: MAX_ANSWER_CHARS - 1].rstrip() + "…"
    return collapsed


def _post(url: str, payload: dict, api_key: str) -> dict:
    """One blocking JSON POST. Called in a worker thread, never on the event loop."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


def answer_text(response: dict) -> str:
    """The assistant text out of a Responses API reply, ignoring the tool-call items."""
    parts = [
        content.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for content in item.get("content", [])
        if content.get("type") in {"output_text", "text"}
    ]
    return "".join(parts).strip()


class OpenAIWebSearch:
    """`WebSearcher` over the Responses API's hosted `web_search` tool."""

    def __init__(self, api_key: str, model: str, *, post=_post) -> None:
        self._api_key = api_key
        self._model = model
        self._post = post

    async def search(self, query: str) -> str:
        payload = {
            "model": self._model,
            "tools": [{"type": "web_search"}],
            "input": query,
            "instructions": SEARCH_INSTRUCTIONS,
        }
        try:
            response = await asyncio.to_thread(self._post, RESPONSES_URL, payload, self._api_key)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("web search for %r failed: %s", query, exc)
            return ""
        return speakable(answer_text(response))
