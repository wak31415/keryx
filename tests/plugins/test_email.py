"""The `check_email` plugin: a question about their email, answered behind the PIN."""

import dataclasses

import pytest

from keryx import plugins
from keryx.integrations.gmail import MESSAGES, EmailError, EmailReader, token_path
from keryx.plugins.email import check_email_tool
from keryx.tools.builtin_common import PIN_REQUIRED_MESSAGE
from keryx.trust import TrustLevel
from plugins.helpers import call, loading, names, offered, refusal, turn_on

NAME = "check_email"


class FakeEmail:
    """Stands in for `EmailReader`: records the question, answers or fails as told."""

    def __init__(self, fail: EmailError | None = None) -> None:
        self.fail = fail
        self.asked: list[tuple[str, str | None, str | None]] = []

    async def ask(self, question, *, query=None, day=None):
        self.asked.append((question, query, day))
        if self.fail is not None:
            raise self.fail
        return {"status": "ok", "scope": "search", "answer": "Ann needs the numbers.", "threads": 1}


def offer(settings, reader):
    path = plugins.write_config(settings, NAME, {})
    with loading(settings):
        return offered(check_email_tool(path, reader=reader), settings)


async def test_it_hands_the_question_over_and_says_the_answer(settings):
    email = FakeEmail()
    registry = offer(settings, email)

    result = await call(registry, NAME, {"question": "did Ann write?", "gmail_query": "from:ann"})

    assert result["answer"] == "Ann needs the numbers."
    assert email.asked == [("did Ann write?", "from:ann", None)]


async def test_it_refuses_a_day_it_does_not_read_and_an_empty_ask(settings):
    email = FakeEmail()
    registry = offer(settings, email)

    older = await call(registry, NAME, {"question": "x", "day": "last week"})
    empty = await call(registry, NAME, {})

    assert "today or yesterday" in older["error"]
    assert "question is required" in empty["error"]
    assert email.asked == []


async def test_a_whole_day_is_asked_for_by_name(settings):
    email = FakeEmail()

    await call(offer(settings, email), NAME, {"question": "todo?", "day": "Today"})

    assert email.asked == [("todo?", None, "today")]


async def test_failing_is_a_status_and_a_sentence(settings):
    registry = offer(settings, FakeEmail(fail=EmailError("signed_out", "invalid_grant")))

    result = await call(registry, NAME, {"question": "anything today?"})

    assert result == {"status": "signed_out", "message": MESSAGES["signed_out"]}


async def test_it_needs_the_pin_on_the_phone(settings):
    email = FakeEmail()

    result = await call(offer(settings, email), NAME, {"question": "x"}, trust=TrustLevel.NONE)

    assert result == {"status": "pin_required", "message": PIN_REQUIRED_MESSAGE}
    assert email.asked == []


def test_the_schema_asks_for_the_question_and_offers_two_days(settings):
    schema = offer(settings, FakeEmail()).schemas()[0]

    assert schema["parameters"]["required"] == ["question"]
    assert schema["parameters"]["properties"]["day"]["enum"] == ["today", "yesterday"]
    assert "Needs the PIN" in schema["description"]


# --- installed and loaded ----------------------------------------------------------------


def test_not_signed_in_the_file_is_refused_with_the_command(settings):
    turn_on(settings, NAME)

    assert refusal(settings, NAME) == "not signed in to Gmail: `keryx auth login gmail`"


def test_signed_in_without_the_claude_cli_it_says_how_to_get_it(settings, monkeypatch):
    token_path(settings).write_text("{}")
    monkeypatch.setattr("keryx.agents.registry.installed", lambda agent, settings=None: False)
    turn_on(settings, NAME)

    assert "the claude CLI is not installed" in refusal(settings, NAME)


def test_signed_in_with_the_cli_it_loads_with_its_model_and_effort(settings, monkeypatch):
    token_path(settings).write_text("{}")
    monkeypatch.setattr("keryx.agents.registry.installed", lambda agent, settings=None: True)
    from keryx.agents.registry import BACKENDS

    spec = dataclasses.replace(BACKENDS["claude"], find_cli=lambda: "/bin/claude")
    monkeypatch.setitem(BACKENDS, "claude", spec)
    turn_on(settings, NAME, model="claude-sonnet-5-5", effort="medium")
    built = []
    real = EmailReader.__init__

    def spy(self, api, summariser, **kwargs):
        built.append((summariser._model, summariser._effort))
        real(self, api, summariser, **kwargs)

    monkeypatch.setattr(EmailReader, "__init__", spy)

    assert NAME in names(settings)
    assert built == [("claude-sonnet-5-5", "medium")]


def test_an_effort_it_does_not_know_is_refused_on_write(settings):
    with pytest.raises(plugins.PluginConfigError, match="effort must be one of"):
        plugins.write_config(settings, NAME, {"effort": "max"})
