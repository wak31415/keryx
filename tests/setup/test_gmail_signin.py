"""Tests for the Gmail sign-in behind `jarvis auth login gmail`: a consent link, then the
redirect address exchanged once."""

import json
import stat
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis.integrations.gmail import SCOPE, TOKEN_URL, token_path
from jarvis.setup.google import (
    PENDING_FILE,
    REDIRECT_URI,
    GoogleSetupError,
    finish_signin,
    start_signin,
)


@pytest.fixture
def client(settings):
    settings.google_oauth_client_id = "cid"
    settings.google_oauth_client_secret = "csecret"
    return settings


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        if self.body is None:
            raise ValueError("no json")
        return self.body


GRANTED = {"refresh_token": "rt", "access_token": "at"}


class FakePost:
    def __init__(self, status=200, body=GRANTED):
        self.response = Response(status, body)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, data, timeout):
        self.calls.append((url, data))
        return self.response


def redirect(url: str, **overrides) -> str:
    state = parse_qs(urlparse(url).query)["state"][0]
    params = {"state": state, "code": "4/abc", "scope": SCOPE, **overrides}
    return REDIRECT_URI + "/?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)


def test_the_link_asks_for_read_only_gmail_with_pkce(client):
    url = start_signin(client)

    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["cid"]
    assert query["scope"] == [SCOPE]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    pending = client.data_dir / PENDING_FILE
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert "csecret" not in url


def test_finishing_exchanges_the_code_once_and_keeps_the_token_private(client):
    url = start_signin(client)
    post = FakePost()

    path = finish_signin(client, redirect(url), post=post)

    [(posted_to, data)] = post.calls
    assert posted_to == TOKEN_URL
    assert (data["code"], data["grant_type"]) == ("4/abc", "authorization_code")
    assert data["code_verifier"] and data["redirect_uri"] == REDIRECT_URI
    assert path == token_path(client)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "refresh_token": "rt",
        "client_id": "cid",
        "client_secret": "csecret",
        "token_uri": TOKEN_URL,
        "scopes": [SCOPE],
    }
    assert not (client.data_dir / PENDING_FILE).exists()  # the verifier is used up


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"state": "someone-else"}, "different sign-in"),
        ({"code": None}, "no code"),
        ({"error": "access_denied"}, "access_denied"),
        ({"scope": "openid"}, "was not granted"),
    ],
)
def test_a_redirect_that_does_not_fit_is_refused(client, change, says):
    url = start_signin(client)

    with pytest.raises(GoogleSetupError, match=says):
        finish_signin(client, redirect(url, **change), post=FakePost())


@pytest.mark.parametrize(
    "post", [FakePost(400, {"error": "invalid_grant"}), FakePost(200, {}), FakePost(502, None)]
)
def test_google_refusing_the_code_is_said(client, post):
    url = start_signin(client)

    with pytest.raises(GoogleSetupError, match="refused the code"):
        finish_signin(client, redirect(url), post=post)
    assert not token_path(client).exists()


def test_finishing_with_nothing_started_says_to_start(client):
    with pytest.raises(GoogleSetupError, match="run `jarvis auth login gmail` first"):
        finish_signin(client, REDIRECT_URI + "/?code=x&state=y", post=FakePost())


def test_no_oauth_client_says_where_one_goes(settings):
    with pytest.raises(GoogleSetupError, match="no Google OAuth client"):
        start_signin(settings)
