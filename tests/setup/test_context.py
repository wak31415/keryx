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

    async def address(settings):
        return "a@b.c"

    monkeypatch.setattr("keryx.setup.agents.run_smoke", fake)
    monkeypatch.setattr("keryx.setup.agents.run_task", fake)
    monkeypatch.setattr("keryx.setup.agents.is_headless", lambda: True)
    monkeypatch.setattr("keryx.setup.google.gmail_address", address)
    monkeypatch.setattr("keryx.setup.google.run_google_setup", lambda settings, echo: True)
    probes = Probes()

    assert asyncio.run(probes.smoke(settings, "claude")).ok
    assert asyncio.run(probes.run_task(settings, "claude", "hi")).ok
    assert asyncio.run(probes.gmail_address(settings)) == "a@b.c"
    assert probes.workspace_signin(settings, print) is True
    assert probes.headless() is True


def test_a_refused_save_is_said_and_changes_nothing(make_ctx):
    ctx = make_ctx([])

    assert ctx.save({"PORT": "eighty"}) is False
    assert any("PORT" in line for line in ctx.ui.lines("error"))
    assert ctx.store.stored() == {}


def test_the_voice_llms_probes_reach_the_same_code_the_command_line_does(monkeypatch):
    """The wizard's `Probes` is `voice_llm.Probes` too: llama.cpp's download, a model's
    pull, and a server's models — through a proxy only for one off this machine."""
    from keryx.endpoints import Endpoint
    from keryx.localmodels import pull, runtimes
    from keryx.setup import context

    seen: list = []
    monkeypatch.setattr(runtimes, "install_llama_cpp",
                        lambda cache, machine, get: seen.append(("llama", cache)) or "/bin/ls")
    monkeypatch.setattr(pull, "pull", lambda settings, name, report: seen.append(name) or 0)
    monkeypatch.setattr(context, "probe",
                        lambda where, get: seen.append(get.keywords["trust_env"]))
    probes = context.Probes()

    assert probes.download_llama_cpp("cache", None, lambda done, total: None) == "/bin/ls"
    assert probes.pull(None, "qwen3.5-4b", None) == 0
    probes.remote_models(Endpoint.parse("http://127.0.0.1:8091/v1"))
    probes.remote_models(Endpoint.parse("https://api.openai.com/v1"))
    assert seen == [("llama", "cache"), "qwen3.5-4b", False, True]
