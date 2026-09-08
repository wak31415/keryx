"""Tests for the voice model's own web search. No network: the transport is injected."""

from jarvis.integrations.web_search import (
    MAX_ANSWER_CHARS,
    OpenAIWebSearch,
    answer_text,
    speakable,
)


def make_response(text: str) -> dict:
    return {
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ]
    }


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


async def test_search_posts_the_query_and_returns_a_spoken_answer():
    posted: list[dict] = []

    def fake_post(url: str, payload: dict, api_key: str) -> dict:
        posted.append({"url": url, "payload": payload, "key": api_key})
        return make_response("Twilio's helper library is on version nine point ten.")

    searcher = OpenAIWebSearch("sk-test", "gpt-5.4-mini", post=fake_post)

    answer = await searcher.search("what version is the twilio library")

    assert answer == "Twilio's helper library is on version nine point ten."
    assert posted[0]["payload"]["model"] == "gpt-5.4-mini"
    assert posted[0]["payload"]["tools"] == [{"type": "web_search"}]
    assert posted[0]["payload"]["input"] == "what version is the twilio library"
    assert posted[0]["key"] == "sk-test"


async def test_a_failed_search_comes_back_empty_rather_than_raising():
    def explode(url: str, payload: dict, api_key: str) -> dict:
        raise OSError("no route to host")

    searcher = OpenAIWebSearch("sk-test", "gpt-5.4-mini", post=explode)

    assert await searcher.search("anything") == ""
