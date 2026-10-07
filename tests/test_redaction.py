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
