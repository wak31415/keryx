"""`child_env.redact_known`: no credential Keryx knows of survives into a `detail`, a `hint`
or a server's problem — a tool or a server can quote the credential it was handed."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from keryx.child_env import redact_known
from keryx.config import load_settings
from keryx.config.store import ConfigStore
from keryx.endpoints import Endpoint
from keryx.localmodels import pull, voice
from keryx.realtime import openai as realtime_openai

SETTING = "sk-planted-setting-0001"
ENV = "env-planted-token-0002"
HF = "hf_planted0003"
KEY = "vk-planted-0004"
EVERY = (SETTING, ENV, HF, KEY)
SAID = f"denied: key={SETTING} auth={ENV} hf={HF} voice={KEY}"
#: The same without the voice key, which only the handshake is handed.
KNOWN = f"denied: key={SETTING} auth={ENV} hf={HF}"


@pytest.fixture
def planted(monkeypatch):
    """A secret setting in the store, a credential and a Hugging Face token in the
    environment; the voice server's key is handed in where it is used."""
    ConfigStore().set({"OPENAI_API_KEY": SETTING})
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", ENV)
    monkeypatch.setenv("HF_TOKEN", HF)


def survivors(text: str) -> list[str]:
    return [secret for secret in EVERY if secret in text]


def test_every_credential_keryx_knows_of_is_taken_out(planted):
    said = redact_known(SAID, extra=[KEY])

    assert survivors(said) == [] and said.count("[redacted]") == 4
    assert said.startswith("denied: key=")


def test_settings_handed_in_are_read_and_the_placeholder_is_no_secret(planted):
    settings = load_settings().model_copy(update={"voice_api_key": KEY})

    assert survivors(redact_known(SAID, settings)) == []
    assert redact_known("unset and empty", settings, extra=["", None]) == "unset and empty"


def test_settings_that_will_not_load_still_leave_the_environment_redacted(planted,
                                                                         monkeypatch):
    def broken(**overrides):
        raise ValueError("config.toml does not parse")

    monkeypatch.setattr("keryx.config.load_settings", broken)

    assert redact_known(f"{ENV} {HF}") == "[redacted] [redacted]"


def test_an_unexpected_exception_keeps_its_type_and_loses_its_credentials(planted):
    written: list[str] = []
    events = pull.JsonLines(written.append)

    def broken() -> int:
        raise PermissionError(KNOWN)

    assert events.guard(broken) == 1
    [event] = [json.loads(line) for line in written]
    assert event["message"] == pull.UNEXPECTED
    assert event["detail"].startswith("PermissionError: denied: key=[redacted]")
    assert survivors(event["detail"]) == []


def test_a_speech_download_that_quotes_a_token_is_redacted(planted):
    told: list[tuple] = []
    report = SimpleNamespace(
        error=lambda message, **kwargs: told.append((message, kwargs.get("detail"))),
        step=lambda step, message, **kwargs: _nothing(), progress=lambda done, total: None,
    )
    probes = SimpleNamespace(speech_python=lambda: "/py",
                             fetch_speech=lambda python, repos, progress: KNOWN)

    assert voice.fetch_models(load_settings(), probes, report, []) is False
    [(message, detail)] = told
    assert message == "The voice's speech models could not be downloaded."
    assert survivors(detail) == []
    assert detail.startswith("denied: key=[redacted]")


class _nothing:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class _Socket:
    def __init__(self, said: dict) -> None:
        self.said = said

    async def recv(self) -> str:
        return json.dumps(self.said)

    async def close(self) -> None:
        pass


def test_a_refused_handshake_never_quotes_a_credential_or_the_key_it_was_asked_with(planted):
    endpoint = Endpoint.parse("http://127.0.0.1:8765/v1", api_key=KEY)

    async def refusing(url, headers):
        return _Socket({"type": "error", "error": {"message": SAID}})

    async def raising(url, headers):
        raise ConnectionError(SAID)

    refused = asyncio.run(realtime_openai.handshake(endpoint, ws_connect=refusing))
    unopened = asyncio.run(realtime_openai.handshake(endpoint, ws_connect=raising))

    assert survivors(refused) == [] and refused.startswith("127.0.0.1 refused the session:")
    assert survivors(unopened) == []
    assert unopened.startswith("could not open 127.0.0.1's Realtime socket (ConnectionError: ")


def test_a_failure_anywhere_in_redaction_withholds_the_text(planted, monkeypatch):
    """`redact_known` never raises, and never falls back to what it was asked to clean."""
    def broken(text, secrets=()):
        raise RuntimeError("the redactor broke")

    def exploding():
        yield KEY
        raise RuntimeError("an extra that breaks halfway")

    assert redact_known(SAID, extra=exploding()) == "(details withheld)"
    monkeypatch.setattr("keryx.child_env.redact", broken)
    assert redact_known(SAID) == "(details withheld)"


def test_a_token_straddling_the_cut_leaves_nothing_of_itself(planted):
    """The download's words are redacted whole, then shortened: a token across the
    two-hundredth character is not cut first and half of it said."""
    told: list[tuple] = []
    report = SimpleNamespace(
        error=lambda message, **kwargs: told.append((message, kwargs.get("detail"))),
        step=lambda step, message, **kwargs: _nothing(), progress=lambda done, total: None,
    )
    said = "x" * 190 + HF + " and more after it" * 10
    probes = SimpleNamespace(speech_python=lambda: "/py",
                             fetch_speech=lambda python, repos, progress: said)

    voice.fetch_models(load_settings(), probes, report, [])

    [(_message, detail)] = told
    assert len(detail) == voice.DETAIL_CHARS and detail.endswith("…")
    assert "hf_" not in detail and "planted" not in detail
    assert detail.startswith("x" * 190 + "[redacted")  # the marker may be what is cut


def test_the_download_child_says_its_error_whole():
    from keryx.localmodels import speech_fetch

    said = speech_fetch._said(ValueError("y" * 500 + "\nsecond line"))

    assert said == "ValueError: " + "y" * 500


def test_a_server_polled_until_it_answers_gathers_the_secrets_once(planted, monkeypatch):
    from keryx import child_env
    from keryx.localmodels import servers

    gathered: list[int] = []
    asked: list[tuple] = []

    def known(settings=None):
        gathered.append(1)
        return (SETTING,)

    def realtime(endpoint, *, secrets=None):
        asked.append(secrets)
        return "no session"

    monkeypatch.setattr(child_env, "known_secrets", known)
    monkeypatch.setattr(realtime_openai, "realtime_problem", realtime)
    times = iter([0.0, 1.0, 2.0, 3.0, 99.0])

    problem = servers.wait_for(Endpoint.parse("http://127.0.0.1:8765/v1"), "realtime", 10.0,
                               sleep=lambda s: None, clock=lambda: next(times))

    assert problem == "no session" and len(asked) == 4
    assert gathered == [1] and asked == [(SETTING,)] * 4


def test_a_secrets_list_that_cannot_be_gathered_is_asked_for_each_time(monkeypatch):
    from keryx import child_env
    from keryx.localmodels import servers

    def broken(settings=None):
        raise RuntimeError("no")

    asked: list[tuple] = []
    monkeypatch.setattr(child_env, "known_secrets", broken)
    monkeypatch.setattr(realtime_openai, "realtime_problem",
                        lambda endpoint, *, secrets=None: asked.append(secrets))

    servers.wait_for(Endpoint.parse("http://127.0.0.1:8765/v1"), "realtime", 1.0,
                     clock=lambda: 0.0)
    assert asked == [None]
