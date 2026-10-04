import httpx
import pytest

from keryx.endpoints import (
    OPENAI_BASE_URL,
    PLACEHOLDER_BEARER,
    Endpoint,
    probe,
    warnings,
)


@pytest.mark.parametrize(
    ("given", "base"),
    [
        ("http://gpu-box:11434", "http://gpu-box:11434/v1"),
        ("http://gpu-box:11434/", "http://gpu-box:11434/v1"),
        ("http://127.0.0.1:8765/v1", "http://127.0.0.1:8765/v1"),
        ("https://gw.example.com/openai/v1/", "https://gw.example.com/openai/v1"),
        ("  https://api.openai.com/v1  ", OPENAI_BASE_URL),
    ],
)
def test_parse_is_always_a_v1_root(given, base):
    assert Endpoint.parse(given).base_url == base


@pytest.mark.parametrize("bad", ["", "gpu-box:11434", "ftp://box/v1", "http://"])
def test_parse_refuses_what_is_not_an_http_address(bad):
    with pytest.raises(ValueError):
        Endpoint.parse(bad)


def test_an_address_carrying_a_key_is_refused_and_not_quoted():
    with pytest.raises(ValueError) as raised:
        Endpoint.parse("https://me:s3cret@llm.example.com")
    assert "s3cret" not in str(raised.value) and "setting of its own" in str(raised.value)


def test_root_drops_v1_for_anthropic_base_url():
    assert Endpoint.parse("http://box:8080").root == "http://box:8080"


def test_ws_url_follows_the_scheme():
    local = Endpoint.parse("http://127.0.0.1:8765")
    remote = Endpoint.parse("https://voice.example.com/v1")
    assert local.ws_url("realtime", model="m") == "ws://127.0.0.1:8765/v1/realtime?model=m"
    assert remote.ws_url("/realtime") == "wss://voice.example.com/v1/realtime"


def test_headers_carry_a_bearer_only_with_a_key():
    assert Endpoint.parse("http://box").headers() == {}
    assert Endpoint.parse("http://box", api_key="k").headers() == {"Authorization": "Bearer k"}


def test_bearer_is_the_placeholder_without_a_key():
    assert Endpoint.parse("http://box").bearer == PLACEHOLDER_BEARER
    assert Endpoint.parse("http://box", api_key="k").bearer == "k"


def test_key_is_not_in_repr():
    assert "s3cret" not in repr(Endpoint.parse("http://box", api_key="s3cret"))


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8765",
        "http://127.0.0.1",
        "http://[::1]:8080",
        "http://192.168.1.20",
        "http://10.0.0.5",
        "http://172.16.3.4",
        "http://169.254.1.1",
        "http://100.101.102.103",
        "http://gpu-box.tail1234.ts.net",
        "http://studio.local",
        "http://nas.lan",
        "http://gpu-box",
    ],
)
def test_private_hosts(url):
    assert Endpoint.parse(url).is_private


@pytest.mark.parametrize("url", ["https://api.openai.com", "http://8.8.8.8", "http://example.com"])
def test_public_hosts(url):
    assert not Endpoint.parse(url).is_private


def test_is_openai():
    assert Endpoint.parse(OPENAI_BASE_URL).is_openai
    assert not Endpoint.parse("http://127.0.0.1:8765").is_openai


def test_no_warnings_for_a_local_server_without_a_key():
    assert warnings(Endpoint.parse("http://127.0.0.1:11434"), carries_voice=True) == []


def test_public_without_a_key_and_over_http_warns_twice():
    found = warnings(Endpoint.parse("http://example.com"))
    assert len(found) == 2
    assert "no key" in found[0] and "clear text" in found[1]


def test_public_over_https_with_a_key_is_quiet():
    assert warnings(Endpoint.parse("https://example.com", api_key="k")) == []


def test_voice_over_plain_http_on_the_lan_warns_about_the_pin():
    found = warnings(Endpoint.parse("http://192.168.1.20:8765"), carries_voice=True)
    assert len(found) == 1 and "PIN" in found[0]
    # The same address for an agent is a LAN server like any other.
    assert warnings(Endpoint.parse("http://192.168.1.20:8765")) == []


def test_voice_over_a_tailnet_is_encrypted_already():
    assert warnings(Endpoint.parse("http://gpu.tail1.ts.net:8765"), carries_voice=True) == []
    assert warnings(Endpoint.parse("http://100.90.1.2:8765"), carries_voice=True) == []


class _Response:
    def __init__(self, status: int, body: object = None) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _getter(response, seen=None):
    def get(url, *, headers, timeout):
        if seen is not None:
            seen.update(url=url, headers=headers)
        if isinstance(response, Exception):
            raise response
        return response

    return get


def test_probe_lists_the_models_and_sends_the_key():
    seen: dict = {}
    body = {"data": [{"id": "qwen3-coder"}, {"id": "gpt-oss-20b"}]}
    endpoint = Endpoint.parse("http://box", api_key="k")
    result = probe(endpoint, get=_getter(_Response(200, body), seen))
    assert result.ok and result.models == ("qwen3-coder", "gpt-oss-20b")
    assert seen == {"url": "http://box/v1/models", "headers": {"Authorization": "Bearer k"}}


def test_probe_reads_ollamas_own_shape_too():
    body = {"models": [{"name": "llama3:8b"}, {"model": "qwen3:30b"}]}
    assert probe(Endpoint.parse("http://box"), get=_getter(_Response(200, body))).models == (
        "llama3:8b",
        "qwen3:30b",
    )


@pytest.mark.parametrize(
    ("response", "says"),
    [
        (httpx.ConnectError("refused"), "could not reach box (ConnectError)"),
        (_Response(401), "does not accept that key"),
        (_Response(503), "HTTP 503"),
        (_Response(200, ValueError("not json")), "did not answer with a model list"),
    ],
)
def test_probe_failures_are_sentences(response, says):
    result = probe(Endpoint.parse("http://box"), get=_getter(response))
    assert not result.ok and says in (result.problem or "")
