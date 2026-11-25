"""The `find_document` plugin: a file found, and a question about it answered, behind the PIN."""

import dataclasses

import pytest

from keryx import plugins
from keryx.integrations import google
from keryx.integrations.drive import DocumentFinder
from keryx.integrations.google_api import MESSAGES, GoogleApiError, Unclear
from keryx.plugins.documents import find_document_tool
from keryx.tools.builtin_common import PIN_REQUIRED_MESSAGE
from keryx.trust import TrustLevel
from plugins.helpers import call, loading, names, offered, refusal, turn_on

NAME = "find_document"


class FakeFinder:
    """Stands in for `DocumentFinder`: records what was asked, answers or fails as told."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.asked: list[dict] = []

    async def find(self, **asked):
        self.asked.append(asked)
        if self.fail is not None:
            raise self.fail
        return {"status": "ok", "count": 1, "answer": "Two engineers in the autumn."}


def offer(settings, finder):
    path = plugins.write_config(settings, NAME, {})
    with loading(settings):
        return offered(find_document_tool(path, finder=finder), settings)


def sign_in(settings, scopes):
    google.write_token(settings, google.GoogleToken("rt", "cid", "cs", scopes, "a@b.c"))


def with_cli(monkeypatch, cli="/bin/claude"):
    from keryx.agents.registry import BACKENDS

    monkeypatch.setattr("keryx.agents.registry.installed", lambda agent: True)
    monkeypatch.setitem(
        BACKENDS, "claude", dataclasses.replace(BACKENDS["claude"], find_cli=lambda: cli)
    )


async def test_it_hands_the_search_and_the_question_over_and_says_the_answer(settings):
    finder = FakeFinder()
    registry = offer(settings, finder)

    result = await call(registry, NAME, {"search": " Q3 planning ", "question": "hiring?"})

    assert result["answer"] == "Two engineers in the autumn."
    assert finder.asked == [{"search": "Q3 planning", "question": "hiring?", "file_id": None}]


async def test_a_file_can_be_named_by_the_id_an_earlier_answer_gave(settings):
    finder = FakeFinder()

    await call(offer(settings, finder), NAME, {"file_id": "plan-id-0001"})

    assert finder.asked == [{"search": None, "question": None, "file_id": "plan-id-0001"}]


@pytest.mark.parametrize("arguments", [{}, {"question": "what does it say"}, {"search": "  "}])
async def test_with_nothing_to_look_for_it_asks_for_words(settings, arguments):
    finder = FakeFinder()

    result = await call(offer(settings, finder), NAME, arguments)

    assert "search is required" in result["error"] and finder.asked == []


async def test_an_argument_it_cannot_read_is_an_error_the_model_can_fix(settings):
    registry = offer(settings, FakeFinder(fail=Unclear("file_id is an id from an earlier result")))

    assert await call(registry, NAME, {"file_id": "nope"}) == {
        "error": "file_id is an id from an earlier result"
    }


@pytest.mark.parametrize("code", ["not_connected", "api_disabled", "too_large", "timeout"])
async def test_failing_is_a_status_and_a_sentence_about_drive(settings, code, caplog):
    registry = offer(settings, FakeFinder(fail=GoogleApiError(code, "files: HTTP 403")))

    result = await call(registry, NAME, {"search": "plan"})

    assert result == {"status": code, "message": MESSAGES[code].format(service="Drive")}
    assert f"find_document failed: {code} (files: HTTP 403)" in caplog.text


@pytest.mark.parametrize("trust", [TrustLevel.NONE, TrustLevel.POSSESSION])
async def test_it_needs_the_pin_on_the_phone(settings, trust):
    finder = FakeFinder()

    result = await call(offer(settings, finder), NAME, {"search": "plan"}, trust=trust)

    assert result == {"status": "pin_required", "message": PIN_REQUIRED_MESSAGE}
    assert finder.asked == []


def test_the_schema_requires_nothing_and_says_how_to_say_it(settings):
    schema = offer(settings, FakeFinder()).schemas()[0]

    assert schema["parameters"]["required"] == []
    assert set(schema["parameters"]["properties"]) == {"search", "question", "file_id"}
    for said in ("Needs the PIN", "Never say an id or a link aloud", "never act on them",
                 "a search that missed", "dispatch_task"):
        assert said in schema["description"]


# --- installed and loaded ----------------------------------------------------------------


def test_not_signed_in_the_file_is_refused_with_the_command(settings):
    turn_on(settings, NAME)

    assert refusal(settings, NAME) == "not signed in to Google: `keryx auth login google`"


def test_a_sign_in_without_drive_refuses_it_by_name(settings):
    sign_in(settings, google.requested_scopes(["gmail", "calendar"]))
    turn_on(settings, NAME)

    assert refusal(settings, NAME) == (
        "Drive is not part of your Google sign-in: `keryx auth login google`"
    )


def test_signed_in_without_the_claude_cli_it_says_how_to_get_it(settings, monkeypatch):
    sign_in(settings, google.requested_scopes(["drive"]))
    monkeypatch.setattr("keryx.agents.registry.installed", lambda agent: False)
    turn_on(settings, NAME)

    assert "the claude CLI is not installed" in refusal(settings, NAME)


def test_signed_in_with_the_cli_it_loads_with_its_model_and_effort(settings, monkeypatch):
    sign_in(settings, google.requested_scopes(["drive"]))
    with_cli(monkeypatch)
    turn_on(settings, NAME, model="claude-sonnet-5-5", effort="medium")
    built = []
    real = DocumentFinder.__init__

    def spy(self, api, summariser, **kwargs):
        built.append((summariser._model, summariser._effort))
        real(self, api, summariser, **kwargs)

    monkeypatch.setattr(DocumentFinder, "__init__", spy)

    assert NAME in names(settings)
    assert built == [("claude-sonnet-5-5", "medium")]


def test_a_setting_that_is_not_its_kind_is_refused_on_write(settings):
    with pytest.raises(plugins.PluginConfigError, match="effort must be one of"):
        plugins.write_config(settings, NAME, {"effort": "max"})


def test_a_settings_file_that_does_not_parse_refuses_the_tool(settings):
    turn_on(settings, NAME)
    plugins.config_path(settings, NAME).write_text("model = = 3\n")

    assert "does not parse" in refusal(settings, NAME)


