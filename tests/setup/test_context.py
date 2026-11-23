"""The probes the wizard reaches the world through, and saving through the context."""

import asyncio

import httpx
import pytest

from keryx.agents.base import RunResult
from keryx.setup import context
from keryx.setup.context import Probes, openai_key_problem


class Answer:
    def __init__(self, status):
        self.status_code = status


@pytest.mark.parametrize(
    ("status", "problem"),
    [(200, None), (401, "OpenAI does not recognise that key"), (500, "OpenAI answered HTTP 500")],
)
def test_the_openai_probe_is_one_get_with_the_key_as_a_bearer(monkeypatch, status, problem):
    seen = []

    def get(url, headers, timeout):
        seen.append((url, headers))
        return Answer(status)

    monkeypatch.setattr(context.httpx, "get", get)

    assert openai_key_problem("sk-1") == problem
    assert seen == [(context.OPENAI_MODELS_URL, {"Authorization": "Bearer sk-1"})]


def test_an_unreachable_openai_says_so(monkeypatch):
    def get(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(context.httpx, "get", get)

    assert openai_key_problem("sk") == "could not reach OpenAI (ConnectError)"


def test_the_default_probes_reach_the_real_implementations(monkeypatch, settings):
    async def fake(*args, **kwargs):
        return RunResult(ok=True, spoken_summary="ready")

    monkeypatch.setattr("keryx.setup.agents.run_smoke", fake)
    monkeypatch.setattr("keryx.setup.agents.run_task", fake)
    monkeypatch.setattr("keryx.setup.agents.is_headless", lambda: True)
    monkeypatch.setattr(
        "keryx.setup.google.browser_grant",
        lambda settings, chosen, announce: ("browser", chosen, announce),
    )
    monkeypatch.setattr(
        "keryx.setup.google.finish_grant", lambda settings, address: ("finished", address)
    )
    monkeypatch.setattr("keryx.setup.google.discard", lambda settings, grant: grant == "g")
    probes = Probes()

    assert asyncio.run(probes.smoke(settings, "claude")).ok
    assert asyncio.run(probes.run_task(settings, "claude", "hi")).ok
    assert probes.google_browser_signin(settings, "offer", print) == ("browser", "offer", print)
    assert probes.google_finish(settings, "http://localhost:1/?x") == (
        "finished", "http://localhost:1/?x")
    assert probes.google_discard(settings, "g") is True
    assert probes.headless() is True


@pytest.mark.parametrize(
    ("raised", "says"),
    [
        ("error", "the browser did not come back"),
        (KeyboardInterrupt(), "stopped before the browser came back"),
    ],
)
def test_a_google_sign_in_that_fails_is_handed_back_not_raised(
    monkeypatch, settings, raised, says
):
    """A result is recorded and replayed by the wizard; a raise would run it again. And
    Ctrl-C while the browser is out stops the wait, not `keryx setup`."""
    from keryx.setup.google import GoogleSetupError

    error = GoogleSetupError(says) if raised == "error" else raised

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("keryx.setup.google.browser_grant", fail)
    monkeypatch.setattr("keryx.setup.google.finish_grant", fail)
    probes = Probes()

    result = probes.google_browser_signin(settings, "offer", print)
    assert isinstance(result, GoogleSetupError) and str(result) == says
    if raised == "error":
        assert probes.google_finish(settings, "http://localhost:1/?x") is error


def test_a_refused_save_is_said_and_changes_nothing(make_ctx):
    ctx = make_ctx([])

    assert ctx.save({"PORT": "eighty"}) is False
    assert any("PORT" in line for line in ctx.ui.lines("error"))
    assert ctx.store.stored() == {}
