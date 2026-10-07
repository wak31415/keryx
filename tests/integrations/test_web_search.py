"""Tests for the voice model's own web search. No network: every transport is injected."""

import io
import urllib.error

import pytest

from keryx.integrations.web_search import (
    MAX_ANSWER_CHARS,
    MAX_RESULTS,
    DdgsWebSearch,
    GeminiWebSearch,
    OpenAIWebSearch,
    SearxngWebSearch,
    answer_text,
    choose,
    gemini_text,
    make_searcher,
    searxng_check,
    searxng_results,
    searxng_url,
    site,
    speakable,
)


def make_response(text: str) -> dict:
    return {
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ]
    }


def gemini_response(*texts: str) -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text} for text in texts]},
                "groundingMetadata": {
                    "webSearchQueries": ["euro 2024 winner"],
                    "groundingChunks": [{"web": {"uri": "https://example.com/x", "title": "x"}}],
                },
            }
        ]
    }


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://searx.test/search", code, "no", {}, io.BytesIO())


# --- the answer, made speakable ----------------------------------------------------------


def test_the_answer_is_read_out_of_the_message_item():
    assert answer_text(make_response("It is 42.")) == "It is 42."


def test_a_response_with_no_message_yields_nothing():
    assert answer_text({"output": [{"type": "web_search_call"}]}) == ""


def test_citations_and_urls_are_stripped_for_speech():
    """The model emits citations however firmly the instructions ask it not to."""
    raw = "The current version is 9.10.9. ([pypi.org](https://pypi.org/project/twilio/))"

    assert speakable(raw) == "The current version is 9.10.9."


def test_markdown_and_bare_urls_do_not_survive():
    assert speakable("**Big** news at https://example.com/x today") == "Big news at today"


def test_a_very_long_answer_is_cut_to_something_speakable():
    spoken = speakable("word " * 400)

    assert len(spoken) <= MAX_ANSWER_CHARS
    assert spoken.endswith("…")


def test_a_result_names_its_site_as_a_person_would():
    assert site("https://www.bbc.co.uk/sport/x?y=1") == "bbc.co.uk"
    assert site("") == ""


# --- OpenAI ------------------------------------------------------------------------------


async def test_openai_posts_the_query_and_returns_a_spoken_answer():
    posted: list[dict] = []

    def fake_post(url: str, payload: dict, headers: dict) -> dict:
        posted.append({"url": url, "payload": payload, "headers": headers})
        return make_response("Twilio's helper library is on version nine point ten.")

    searcher = OpenAIWebSearch("sk-test", "gpt-6-luna", post=fake_post)

    found = await searcher.search("what version is the twilio library")

    assert found == {"answer": "Twilio's helper library is on version nine point ten."}
    assert posted[0]["payload"]["model"] == "gpt-6-luna"
    assert posted[0]["payload"]["tools"] == [{"type": "web_search"}]
    assert posted[0]["payload"]["input"] == "what version is the twilio library"
    assert posted[0]["headers"] == {"Authorization": "Bearer sk-test"}


async def test_a_failed_search_comes_back_empty_rather_than_raising(caplog):
    def explode(url: str, payload: dict, headers: dict) -> dict:
        raise OSError("no route to host")

    searcher = OpenAIWebSearch("sk-test", "gpt-6-luna", post=explode)

    assert await searcher.search("anything") == {}
    assert "sk-test" not in caplog.text


async def test_an_empty_answer_is_nothing_to_say():
    searcher = OpenAIWebSearch("sk-test", "m", post=lambda *_: make_response("  "))

    assert await searcher.search("anything") == {}


# --- Google, through Gemini --------------------------------------------------------------


async def test_gemini_grounds_on_google_search_with_the_key_in_a_header():
    posted: list[dict] = []

    def fake_post(url: str, payload: dict, headers: dict) -> dict:
        posted.append({"url": url, "payload": payload, "headers": headers})
        return gemini_response("Spain won Euro 2024, ", "beating England two one.")

    searcher = GeminiWebSearch("g-key", "gemini-3.8-flash", post=fake_post)

    found = await searcher.search("who won euro 2024")

    assert found == {"answer": "Spain won Euro 2024, beating England two one."}
    call = posted[0]
    assert call["url"].endswith("/models/gemini-3.8-flash:generateContent")
    assert "g-key" not in call["url"]  # a URL is what an error quotes
    assert call["headers"] == {"x-goog-api-key": "g-key"}
    assert call["payload"]["tools"] == [{"google_search": {}}]
    assert call["payload"]["contents"][0]["parts"][0]["text"] == "who won euro 2024"


def test_a_gemini_reply_with_no_candidate_has_no_text():
    assert gemini_text({}) == ""
    assert gemini_text({"candidates": [{"finishReason": "SAFETY"}]}) == ""


async def test_a_failed_gemini_search_comes_back_empty():
    def refuse(url: str, payload: dict, headers: dict) -> dict:
        raise http_error(429)

    assert await GeminiWebSearch("k", "m", post=refuse).search("q") == {}


# --- SearXNG -----------------------------------------------------------------------------


SEARXNG_REPLY = {
    "query": "euro 2024 final",
    "answers": [],
    "results": [
        {
            "title": "UEFA Euro 2024 final - Wikipedia",
            "url": "https://en.wikipedia.org/wiki/UEFA_Euro_2024_final",
            "content": "The match was held at the Olympiastadion in Berlin on 14 July 2024.",
            "engine": "wikipedia",
        },
        {"title": "", "url": "https://empty.example", "content": ""},
        *[
            {"title": f"Result {n}", "url": f"https://site{n}.example/p", "content": "text"}
            for n in range(10)
        ],
    ],
}


def test_searxng_results_are_titles_snippets_and_sites_never_urls():
    found = searxng_results(SEARXNG_REPLY)

    rows = found["results"]
    assert len(rows) == MAX_RESULTS
    assert rows[0] == {
        "title": "UEFA Euro 2024 final - Wikipedia",
        "snippet": "The match was held at the Olympiastadion in Berlin on 14 July 2024.",
        "site": "en.wikipedia.org",
    }
    assert all(row["title"] or row["snippet"] for row in rows)  # the empty one is dropped
    assert "https://" not in repr(found)


def test_searxng_instant_answers_come_first_in_either_shape():
    reply = {"answers": [{"answer": "14 July 2024", "url": "https://a.example"}, "Berlin"],
             "results": []}

    found = searxng_results(reply)

    assert [row["snippet"] for row in found["results"]] == ["14 July 2024", "Berlin"]
    assert found["results"][0]["site"] == "a.example"


def test_a_searxng_reply_with_nothing_in_it_is_nothing_to_say():
    assert searxng_results({"results": []}) == {}


async def test_searxng_asks_for_json_at_its_own_address():
    asked: list[str] = []

    def fake_get(url: str) -> dict:
        asked.append(url)
        return SEARXNG_REPLY

    found = await SearxngWebSearch("http://127.0.0.1:8888/", get=fake_get).search("euro & final")

    assert asked == ["http://127.0.0.1:8888/search?q=euro+%26+final&format=json"]
    assert found["results"][0]["site"] == "en.wikipedia.org"


async def test_a_failed_searxng_search_comes_back_empty():
    def refuse(url: str) -> dict:
        raise http_error(403)

    assert await SearxngWebSearch("http://s", get=refuse).search("q") == {}


def test_searxng_check_names_the_json_format_fix_for_a_403():
    def refuse(url: str) -> dict:
        raise http_error(403)

    assert "search.formats" in searxng_check("http://s", get=refuse)


def test_searxng_check_says_what_else_went_wrong():
    def not_found(url: str) -> dict:
        raise http_error(404)

    def down(url: str) -> dict:
        raise urllib.error.URLError("refused")

    assert searxng_check("http://s", get=not_found) == "it answered HTTP 404"
    assert "could not be reached" in searxng_check("http://s", get=down)
    assert searxng_check("http://s", get=lambda url: SEARXNG_REPLY) is None


def test_searxng_url_drops_a_trailing_slash():
    assert searxng_url("http://s/", "x") == "http://s/search?q=x&format=json"


# --- ddgs --------------------------------------------------------------------------------


async def test_ddgs_rows_become_results():
    def fake_text(query: str) -> list[dict]:
        assert query == "euro 2024"
        return [{"title": "Euro **2024**", "href": "https://www.uefa.com/x", "body": "Spain won."}]

    found = await DdgsWebSearch(text=fake_text).search("euro 2024")

    assert found == {"results": [{"title": "Euro 2024", "snippet": "Spain won.",
                                  "site": "uefa.com"}]}


async def test_a_ddgs_failure_of_any_kind_comes_back_empty():
    class RatelimitError(Exception):
        pass

    def limited(query: str) -> list[dict]:
        raise RatelimitError("202 Ratelimit")

    assert await DdgsWebSearch(text=limited).search("q") == {}
    assert await DdgsWebSearch(text=lambda q: []).search("q") == {}


# --- which one ---------------------------------------------------------------------------


NOTHING = {"openai_key": False, "gemini_key": False, "searxng_url": False, "ddgs": False}


@pytest.mark.parametrize(
    ("ready", "expected"),
    [
        ({"searxng_url": True, "gemini_key": True, "openai_key": True, "ddgs": True}, "searxng"),
        ({"gemini_key": True, "openai_key": True, "ddgs": True}, "google"),
        ({"openai_key": True, "ddgs": True}, "openai"),
        ({"ddgs": True}, "ddgs"),
    ],
)
def test_auto_takes_the_most_deliberate_backend_that_is_set_up(ready, expected):
    assert choose("auto", **{**NOTHING, **ready}) == (expected, None)


def test_auto_with_nothing_set_up_says_how_to_get_one():
    backend, why = choose("auto", **NOTHING)

    assert backend is None and "ddgs" in why and "SEARXNG_URL" in why


def test_a_named_backend_is_used_only_when_it_is_set_up():
    assert choose("openai", **{**NOTHING, "openai_key": True, "ddgs": True}) == ("openai", None)
    assert choose("google", **{**NOTHING, "ddgs": True}) == (None, "GEMINI_API_KEY is not set")
    assert choose("searxng", **NOTHING) == (None, "SEARXNG_URL is not set")
    assert choose("ddgs", **NOTHING)[1].startswith("the ddgs package is not installed")


def test_off_is_off_whatever_is_set_up():
    ready = {"openai_key": True, "gemini_key": True, "searxng_url": True, "ddgs": True}

    assert choose("off", **ready) == (None, "WEB_SEARCH is off")


def test_make_searcher_builds_each_backend_from_explicit_values():
    assert make_searcher("openai", openai_key="sk", openai_model="m").backend == "openai"
    assert make_searcher("google", gemini_key="g", google_model="m").backend == "google"
    assert make_searcher("searxng", searxng_url="http://s").backend == "searxng"
    assert make_searcher("ddgs").backend == "ddgs"
    assert make_searcher(None) is None
    assert make_searcher("openai") is None  # no key, no searcher
