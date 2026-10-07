"""Web search for the voice model itself, so a small question needs no subagent.

A Realtime session has no hosted search tool — it accepts only `function` and `mcp` tools
(verified 2026-08-24) — so search is a function tool we answer ourselves, from one of four
backends (`WEB_SEARCH`):

- `openai` asks the Responses API, which has a hosted search, and returns its answer;
- `google` asks Gemini with grounding on Google Search, and returns its answer;
- `searxng` asks a SearXNG instance (usually the owner's own) for results;
- `ddgs` asks the public search engines through the `ddgs` library: no key, no server.

The first two answer; the last two return a handful of results, which the voice model
answers from. Either way nothing goes back to the caller but plain words: no markdown, no
URLs, a site's name at most. Google's own search API (Custom Search JSON) closed to new
customers in 2025 and ends on 2027-01-01, which is why `google` means Gemini.

`WebSearcher` is the seam the tests use: no test ever reaches the network.
"""

import asyncio
import importlib.util
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Literal, Protocol

log = logging.getLogger("keryx.web_search")

Backend = Literal["openai", "google", "searxng", "ddgs"]
#: What `WEB_SEARCH` may say: a backend, `auto`, or `off`.
Choice = Literal["auto", "openai", "google", "searxng", "ddgs", "off"]

RESPONSES_URL = "https://api.openai.com/v1/responses"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
#: Search answers are spoken, so they are asked for short and stripped of anything
#: text-to-speech would mangle.
SEARCH_INSTRUCTIONS = (
    "Answer in at most two short spoken sentences. Plain speech: no markdown, no lists, "
    "no URLs, no citations. If the answer is a number or a date, say it plainly. If the "
    "search turns up nothing solid, say so."
)
REQUEST_TIMEOUT_S = 30.0
MAX_ANSWER_CHARS = 600
#: Results handed to the voice model: enough to answer from, few enough to read quickly.
MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 300

#: Citations come back as `([example.com](https://…))` however firmly we ask for none.
_CITATION_RE = re.compile(r"\s*\(?\[[^\]]*\]\([^)]*\)\)?")
_URL_RE = re.compile(r"https?://\S+")
_MARKDOWN_RE = re.compile(r"[*_`#]+")

#: One search's outcome: `{"answer": …}`, `{"results": […]}`, or `{}` for nothing.
Found = dict
Post = Callable[[str, dict, dict[str, str]], dict]
Get = Callable[[str], dict]


class WebSearcher(Protocol):
    """Anything that can look a spoken question up on the web."""

    backend: Backend

    async def search(self, query: str) -> Found:
        """`{"answer": …}` or `{"results": […]}`; `{}` when there is nothing to say."""
        ...


def speakable(text: str, limit: int = MAX_ANSWER_CHARS) -> str:
    """The text with the things a voice cannot read stripped out, cut to `limit`."""
    without_citations = _CITATION_RE.sub("", text)
    without_urls = _URL_RE.sub("", without_citations)
    collapsed = " ".join(_MARKDOWN_RE.sub("", without_urls).split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def site(url: str) -> str:
    """The site a result is from, as a person would say it: `en.wikipedia.org`."""
    host = urllib.parse.urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def result(title: str, snippet: str, url: str) -> dict[str, str]:
    """One result as the voice model sees it: never the URL, only the site."""
    return {
        "title": speakable(title, MAX_SNIPPET_CHARS),
        "snippet": speakable(snippet, MAX_SNIPPET_CHARS),
        "site": site(url),
    }


def _answer(text: str) -> Found:
    spoken = speakable(text)
    return {"answer": spoken} if spoken else {}


def _results(rows: list[dict[str, str]]) -> Found:
    kept = [row for row in rows if row["title"] or row["snippet"]][:MAX_RESULTS]
    return {"results": kept} if kept else {}


def _post(url: str, payload: dict, headers: dict[str, str]) -> dict:
    """One blocking JSON POST. Called in a worker thread, never on the event loop."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


def _get(url: str) -> dict:
    """One blocking JSON GET. Called in a worker thread, never on the event loop."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


#: What a backend's transport raises when the search did not happen.
TRANSPORT_ERRORS = (urllib.error.URLError, TimeoutError, OSError, ValueError)


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


def gemini_text(response: dict) -> str:
    """The answer out of a Gemini `generateContent` reply: the first candidate's text."""
    candidates = response.get("candidates") or [{}]
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts).strip()


class OpenAIWebSearch:
    """`WebSearcher` over the Responses API's hosted `web_search` tool."""

    backend: Backend = "openai"

    def __init__(self, api_key: str, model: str, *, post: Post = _post) -> None:
        self._api_key = api_key
        self._model = model
        self._post = post

    async def search(self, query: str) -> Found:
        payload = {
            "model": self._model,
            "tools": [{"type": "web_search"}],
            "input": query,
            "instructions": SEARCH_INSTRUCTIONS,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            response = await asyncio.to_thread(self._post, RESPONSES_URL, payload, headers)
        except TRANSPORT_ERRORS as exc:
            log.warning("web search (openai) failed: %s", type(exc).__name__)
            return {}
        return _answer(answer_text(response))


class GeminiWebSearch:
    """`WebSearcher` over Gemini, grounded on Google Search."""

    backend: Backend = "google"

    def __init__(self, api_key: str, model: str, *, post: Post = _post) -> None:
        self._api_key = api_key
        self._model = model
        self._post = post

    async def search(self, query: str) -> Found:
        payload = {
            "systemInstruction": {"parts": [{"text": SEARCH_INSTRUCTIONS}]},
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
        }
        url = f"{GEMINI_URL}/{urllib.parse.quote(self._model)}:generateContent"
        # The key in a header, never the URL's `?key=`: a URL is what an error quotes.
        headers = {"x-goog-api-key": self._api_key}
        try:
            response = await asyncio.to_thread(self._post, url, payload, headers)
        except TRANSPORT_ERRORS as exc:
            log.warning("web search (google) failed: %s", type(exc).__name__)
            return {}
        return _answer(gemini_text(response))


def searxng_results(response: dict) -> Found:
    """A SearXNG JSON reply as results, its instant answers (when it has any) first."""
    rows = [
        result("", answer.get("answer", ""), answer.get("url", ""))
        if isinstance(answer, dict)
        else result("", str(answer), "")
        for answer in response.get("answers") or []
    ]
    rows += [
        result(item.get("title", ""), item.get("content", ""), item.get("url", ""))
        for item in response.get("results") or []
    ]
    return _results(rows)


def searxng_url(base_url: str, query: str) -> str:
    """The JSON search URL on a SearXNG instance."""
    return f"{base_url.rstrip('/')}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"}
    )


def searxng_problem(exc: BaseException) -> str:
    """Why a SearXNG search failed, in a sentence; 403 is the usual one, and has a fix."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 403:
        return "it answered 403: add `json` to `search.formats` in its settings.yml"
    if isinstance(exc, urllib.error.HTTPError):
        return f"it answered HTTP {exc.code}"
    return f"it could not be reached ({type(exc).__name__})"


class SearxngWebSearch:
    """`WebSearcher` over a SearXNG instance's JSON API (`search.formats` must list `json`)."""

    backend: Backend = "searxng"

    def __init__(self, base_url: str, *, get: Get = _get) -> None:
        self._base_url = base_url
        self._get = get

    async def search(self, query: str) -> Found:
        try:
            response = await asyncio.to_thread(self._get, searxng_url(self._base_url, query))
        except TRANSPORT_ERRORS as exc:
            log.warning("web search (searxng) failed: %s", searxng_problem(exc))
            return {}
        return searxng_results(response)


def searxng_check(base_url: str, *, get: Get = _get) -> str | None:
    """None when the instance answers a JSON search, else why not — `doctor` asks this."""
    try:
        get(searxng_url(base_url, "keryx"))
    except TRANSPORT_ERRORS as exc:
        return searxng_problem(exc)
    return None


def ddgs_installed() -> bool:
    return importlib.util.find_spec("ddgs") is not None


def _ddgs_text(query: str) -> list[dict]:  # pragma: no cover - the network, by definition
    """One blocking `ddgs` text search. Imported here: the package is an optional extra."""
    from ddgs import DDGS

    return DDGS(timeout=int(REQUEST_TIMEOUT_S)).text(query, max_results=MAX_RESULTS)


class DdgsWebSearch:
    """`WebSearcher` over the `ddgs` metasearch library: no key and no server."""

    backend: Backend = "ddgs"

    def __init__(self, *, text: Callable[[str], list[dict]] = _ddgs_text) -> None:
        self._text = text

    async def search(self, query: str) -> Found:
        try:
            rows = await asyncio.to_thread(self._text, query)
        except Exception as exc:  # ddgs raises its own errors, and scraping fails in many ways
            log.warning("web search (ddgs) failed: %s", type(exc).__name__)
            return {}
        return _results(
            [result(row.get("title", ""), row.get("body", ""), row.get("href", "")) for row in rows]
        )


def choose(
    choice: Choice,
    *,
    openai_key: bool,
    gemini_key: bool,
    searxng_url: bool,
    ddgs: bool,
) -> tuple[Backend | None, str | None]:
    """The backend `choice` resolves to, or None and why there is none.

    `auto` takes the first that is set up, most deliberate first: a SearXNG address or a
    Gemini key is only ever set for search, an OpenAI key may be there for the voice, and
    `ddgs` needs nothing but the package.
    """
    missing: dict[Backend, str | None] = {
        "searxng": None if searxng_url else "SEARXNG_URL is not set",
        "google": None if gemini_key else "GEMINI_API_KEY is not set",
        "openai": None if openai_key else "OPENAI_API_KEY is not set",
        "ddgs": None if ddgs else "the ddgs package is not installed (`uv sync --extra ddgs`)",
    }
    if choice == "off":
        return None, "WEB_SEARCH is off"
    if choice == "auto":
        backend = next((name for name, why in missing.items() if why is None), None)
        if backend is None:
            return None, (
                "nothing is set up: install ddgs (`uv sync --extra ddgs`), or set SEARXNG_URL, "
                "GEMINI_API_KEY or OPENAI_API_KEY"
            )
        return backend, None
    return (choice, None) if missing[choice] is None else (None, missing[choice])


def make_searcher(
    backend: Backend | None,
    *,
    openai_key: str | None = None,
    openai_model: str = "",
    gemini_key: str | None = None,
    google_model: str = "",
    searxng_url: str | None = None,
) -> WebSearcher | None:
    """The searcher for `backend` (as `choose` resolved it), from explicit values."""
    if backend == "openai" and openai_key:
        return OpenAIWebSearch(openai_key, openai_model)
    if backend == "google" and gemini_key:
        return GeminiWebSearch(gemini_key, google_model)
    if backend == "searxng" and searxng_url:
        return SearxngWebSearch(searxng_url)
    if backend == "ddgs":
        return DdgsWebSearch()
    return None


#: How each backend is named to the owner: `doctor`, the wizard, the startup log.
DESCRIPTIONS: dict[Backend, str] = {
    "openai": "OpenAI's Responses API",
    "google": "Google, through Gemini",
    "searxng": "SearXNG",
    "ddgs": "the public search engines, through ddgs (no key)",
}
