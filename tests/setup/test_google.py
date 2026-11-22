"""The Google sign-in: a consent link, the two ways its answer comes back, and the exchange.

No network and no socket: the token endpoint is a fake `post`, the listener's handler is
driven with a scripted connection, and `browser_signin` gets a scripted listener.
"""

import base64
import io
import json
import stat
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from keryx.integrations import google as account
from keryx.integrations.gmail import legacy_token_path
from keryx.integrations.google import SERVICES, TOKEN_URL
from keryx.setup import google
from keryx.setup.google import (
    PASTE_REDIRECT_URI,
    PENDING_FILE,
    GoogleSetupError,
    Listener,
    finish_signin,
    offer,
    start_signin,
)

ALL = tuple(SERVICES)


@pytest.fixture
def client(settings):
    settings.google_oauth_client_id = "cid"
    settings.google_oauth_client_secret = "csecret"
    return settings


def published(tmp_path, monkeypatch, **extra):
    path = tmp_path / "google_client.json"
    path.write_text(json.dumps({"installed": {"client_id": "pub", "client_secret": "ps"}, **extra}))
    monkeypatch.setattr(account, "PUBLISHED_CLIENT_FILE", path)


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        if self.body is None:
            raise ValueError("no json")
        return self.body


def id_token(email="ada@example.com") -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"header.{claims}.signature"


def granted(services=ALL, **changes) -> dict:
    return {
        "refresh_token": "1//rt",
        "access_token": "ya29.at",
        "scope": " ".join(account.requested_scopes(services)),
        "id_token": id_token(),
        **changes,
    }


class FakePost:
    def __init__(self, status=200, body=None):
        self.response = Response(status, granted() if body is None and status == 200 else body)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, data, timeout):
        self.calls.append((url, data))
        return self.response


def link_query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def redirect(url: str, **overrides) -> str:
    params = {"state": link_query(url)["state"], "code": "4/abc", **overrides}
    return PASTE_REDIRECT_URI + "/?" + "&".join(
        f"{k}={v}" for k, v in params.items() if v is not None
    )


# --- what a sign-in asks for, and with whose client ---------------------------------------


def test_no_client_at_all_says_how_to_make_one(settings):
    with pytest.raises(GoogleSetupError, match="no Google OAuth client") as raised:
        offer(settings)

    assert "--client-file" in str(raised.value) and "google.md" in str(raised.value)


def test_the_owners_own_client_offers_everything(client):
    chosen = offer(client)

    assert chosen.services == ALL and chosen.note is None and not chosen.client.published


def test_chosen_services_come_back_in_the_tables_order(client):
    assert offer(client, ["slides", "gmail"]).services == ("gmail", "slides")


def test_the_published_client_is_limited_to_what_it_offers_and_says_so(
    settings, tmp_path, monkeypatch
):
    published(tmp_path, monkeypatch, keryx_services=["calendar", "drive", "docs"])

    chosen = offer(settings)

    assert chosen.client.published and chosen.services == ("calendar", "drive", "docs")
    assert chosen.note.startswith(
        "Gmail, Sheets and Slides are not offered by Keryx's Google app yet: they need your "
        "own Google client (`keryx auth login google --client-file FILE`"
    )


def test_one_withheld_service_is_said_in_the_singular(settings, tmp_path, monkeypatch):
    published(tmp_path, monkeypatch, keryx_services=[n for n in ALL if n != "gmail"])

    note = offer(settings).note

    assert note.startswith("Gmail is not offered by Keryx's Google app yet: it needs")


def test_asking_the_published_client_for_what_it_does_not_offer_is_refused(
    settings, tmp_path, monkeypatch
):
    published(tmp_path, monkeypatch, keryx_services=["calendar"])

    with pytest.raises(GoogleSetupError, match="does not offer Gmail yet.*--client-file"):
        offer(settings, ["gmail", "calendar"])
    assert offer(settings, ["calendar"]).note is None  # asked for by name: nothing left out


def test_a_published_client_that_offers_nothing_is_refused(settings, tmp_path, monkeypatch):
    published(tmp_path, monkeypatch, keryx_services=[])

    with pytest.raises(GoogleSetupError, match="does not offer any service yet"):
        offer(settings)


# --- the link -----------------------------------------------------------------------------


def test_the_link_asks_for_the_chosen_services_with_pkce_and_state(client):
    url = start_signin(client, offer(client, ["gmail", "docs"]))

    query = link_query(url)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query["client_id"] == "cid" and query["response_type"] == "code"
    assert query["scope"].split() == list(account.requested_scopes(["gmail", "docs"]))
    assert query["redirect_uri"] == PASTE_REDIRECT_URI
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert (query["access_type"], query["prompt"]) == ("offline", "consent")
    assert len(query["state"]) >= 24 and "login_hint" not in query
    assert "csecret" not in url


def test_the_verifier_and_state_wait_in_a_private_file(client):
    url = start_signin(client)

    pending = client.data_dir / PENDING_FILE
    saved = json.loads(pending.read_text())
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert saved["state"] == link_query(url)["state"] and saved["services"] == list(ALL)
    assert saved["code_verifier"] not in url


def test_a_saved_address_picks_the_account_on_googles_screen(client):
    client.user_google_email = "ada@example.com"

    assert link_query(start_signin(client))["login_hint"] == "ada@example.com"


# --- finishing ----------------------------------------------------------------------------


def test_finishing_exchanges_the_code_once_and_keeps_one_private_token(client):
    url = start_signin(client)
    post = FakePost()

    result = finish_signin(client, redirect(url), post=post)

    [(posted_to, data)] = post.calls
    assert posted_to == TOKEN_URL
    assert data == {
        "code": "4/abc",
        "client_id": "cid",
        "client_secret": "csecret",
        "redirect_uri": PASTE_REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": data["code_verifier"],
    }
    assert len(data["code_verifier"]) >= 43
    assert result.path == account.token_path(client)
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert json.loads(result.path.read_text()) == {
        "refresh_token": "1//rt",
        "client_id": "cid",
        "client_secret": "csecret",
        "token_uri": TOKEN_URL,
        "scopes": list(account.requested_scopes(ALL)),
        "email": "ada@example.com",
    }
    assert (result.email, result.connected, result.missing) == ("ada@example.com", ALL, ())
    assert result.whose == "your own Google client"
    assert not (client.data_dir / PENDING_FILE).exists()  # the verifier is used up


def test_an_address_pasted_with_its_quotes_still_finishes(client):
    url = start_signin(client)

    result = finish_signin(client, f"  '{redirect(url)}' ", post=FakePost())

    assert result.connected == ALL


def test_finishing_hands_the_agents_server_its_copy(client):
    finish_signin(client, redirect(start_signin(client)), post=FakePost())

    exported = json.loads((client.data_dir / "google" / "ada@example.com.json").read_text())
    assert exported["refresh_token"] == "1//rt" and exported["token"] is None


def test_finishing_spends_the_older_gmail_only_token(client):
    older = legacy_token_path(client)
    older.parent.mkdir(parents=True, exist_ok=True)
    older.write_text("{}")

    finish_signin(client, redirect(start_signin(client)), post=FakePost())

    assert not older.exists()


def test_what_the_owner_unticked_is_not_connected_and_is_said(client):
    scopes = [s for s in account.requested_scopes(ALL) if s != account.CALENDAR_EVENTS]
    url = start_signin(client)

    result = finish_signin(
        client, redirect(url), post=FakePost(body=granted(scope=" ".join(scopes)))
    )

    assert result.connected == ("gmail", "drive", "docs", "sheets", "slides")
    assert result.missing == ("calendar",)
    assert account.read_token(client).scopes == tuple(scopes)  # exactly what was granted
    assert google.summary(result) == [
        "Signed in as ada@example.com. Connected: Gmail, Drive, Docs, Sheets and Slides.",
        "Not connected: Calendar, not granted on Google's screen; `keryx auth login google` "
        "again to add it.",
    ]


def test_leaving_drive_unticked_takes_the_document_services_with_it_and_says_why(client):
    scopes = [s for s in account.requested_scopes(ALL) if "drive" not in s]
    url = start_signin(client)

    result = finish_signin(
        client, redirect(url), post=FakePost(body=granted(scope=" ".join(scopes)))
    )

    assert result.connected == ("gmail", "calendar")
    assert google.summary(result)[1] == (
        "Not connected: Drive, Docs, Sheets and Slides, not granted on Google's screen "
        "(Docs, Sheets and Slides need Drive as well); `keryx auth login google` again to "
        "add them."
    )


def test_everything_granted_is_one_line(client):
    result = finish_signin(client, redirect(start_signin(client)), post=FakePost())

    assert google.summary(result) == [
        "Signed in as ada@example.com. Connected: Gmail, Calendar, Drive, Docs, Sheets and Slides."
    ]


def test_nothing_usable_granted_fails_and_keeps_no_token(client):
    url = start_signin(client)
    post = FakePost(body=granted(scope="openid https://www.googleapis.com/auth/userinfo.email"))

    with pytest.raises(GoogleSetupError, match="nothing was granted"):
        finish_signin(client, redirect(url), post=post)
    assert account.read_token(client) is None


def test_the_redirects_scope_is_believed_when_the_token_answer_names_none(client):
    url = start_signin(client)
    only_gmail = "%20".join(account.requested_scopes(["gmail"]))
    body = {key: value for key, value in granted().items() if key != "scope"}

    result = finish_signin(client, redirect(url, scope=only_gmail), post=FakePost(body=body))

    assert result.connected == ("gmail",)


def test_with_no_scope_anywhere_what_was_asked_for_is_what_was_granted(client):
    url = start_signin(client, offer(client, ["calendar"]))
    body = {key: value for key, value in granted().items() if key != "scope"}

    assert finish_signin(client, redirect(url), post=FakePost(body=body)).connected == ("calendar",)


def test_a_client_with_no_secret_sends_none(settings, tmp_path):
    source = tmp_path / "public.json"
    source.write_text('{"installed": {"client_id": "public"}}')
    settings.google_client_secrets_file = source
    post = FakePost()

    finish_signin(settings, redirect(start_signin(settings)), post=post)

    assert "client_secret" not in post.calls[0][1]
    assert account.read_token(settings).client_secret == ""


def test_the_address_comes_from_userinfo_when_the_id_token_does_not_say(client):
    asked = []

    def get(url, headers, timeout):
        asked.append((url, headers))
        return Response(200, {"email": "bob@example.com"})

    url = start_signin(client)
    post = FakePost(body=granted(id_token="not-a-jwt"))

    assert finish_signin(client, redirect(url), post=post, get=get).email == "bob@example.com"
    assert asked == [(google.USERINFO_URL, {"Authorization": "Bearer ya29.at"})]


@pytest.mark.parametrize(
    "get",
    [
        lambda *a, **k: Response(401, {}),
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ],
)
def test_a_sign_in_nobody_can_name_is_refused(client, get):
    url = start_signin(client)
    post = FakePost(body=granted(id_token=None))

    with pytest.raises(GoogleSetupError, match="did not say which account"):
        finish_signin(client, redirect(url), post=post, get=get)
    assert account.read_token(client) is None


def test_no_access_token_and_no_id_token_asks_nobody(client):
    url = start_signin(client)
    post = FakePost(body=granted(id_token="e30.e30.x", access_token=None))

    with pytest.raises(GoogleSetupError, match="did not say which account"):
        finish_signin(client, redirect(url), post=post, get=None)


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"state": "someone-else"}, "different sign-in"),
        ({"state": "st%C3%A9"}, "different sign-in"),
        ({"state": None}, "different sign-in"),
        ({"code": None}, "no code"),
        ({"error": "access_denied"}, "Google said: access_denied"),
    ],
)
def test_a_redirect_that_does_not_fit_is_refused(client, change, says):
    url = start_signin(client)
    post = FakePost()

    with pytest.raises(GoogleSetupError, match=says):
        finish_signin(client, redirect(url, **change), post=post)
    assert post.calls == []


@pytest.mark.parametrize(
    "post", [FakePost(400, {"error": "invalid_grant"}), FakePost(200, {}), FakePost(502, None)]
)
def test_google_refusing_the_code_is_said(client, post):
    url = start_signin(client)

    with pytest.raises(GoogleSetupError, match="refused the code"):
        finish_signin(client, redirect(url), post=post)
    assert account.read_token(client) is None


def test_google_being_unreachable_is_said_without_the_request(client):
    def post(url, data, timeout):
        raise httpx.ConnectError(f"cannot connect with {data['client_secret']}")

    with pytest.raises(GoogleSetupError) as raised:
        finish_signin(client, redirect(start_signin(client)), post=post)

    assert str(raised.value) == "could not reach Google (ConnectError)"


def test_a_client_that_changed_since_the_link_is_refused(client):
    url = start_signin(client)
    client.google_oauth_client_id = "another"

    with pytest.raises(GoogleSetupError, match="client changed since the link"):
        finish_signin(client, redirect(url), post=FakePost())


@pytest.mark.parametrize("left", [None, "{not json", '{"state": "s"}'])
def test_finishing_with_nothing_started_says_to_start(client, left):
    if left is not None:
        client.data_dir.mkdir(parents=True, exist_ok=True)
        (client.data_dir / PENDING_FILE).write_text(left)

    with pytest.raises(GoogleSetupError, match="run `keryx auth login google` first"):
        finish_signin(client, PASTE_REDIRECT_URI + "/?code=x&state=y", post=FakePost())


# --- the listener -------------------------------------------------------------------------


class Connection:
    """One scripted HTTP request, as the socket the handler reads and writes."""

    def __init__(self, request: str):
        self._in, self.out = io.BytesIO(request.encode()), io.BytesIO()
        self.timeout = None

    def makefile(self, mode, *args, **kwargs):
        return self._in

    def sendall(self, data):
        self.out.write(data)

    def settimeout(self, timeout):
        self.timeout = timeout


class Server:
    """What the handler and `Listener` use of an `HTTPServer`."""

    server_address = ("127.0.0.1", 49152)

    def __init__(self, requests=()):
        self.requests = list(requests)
        self.closed = False
        self.timeouts = []

    def handle_request(self):
        self.timeouts.append(self.timeout)
        if self.requests:
            serve(self, self.requests.pop(0))

    def server_close(self):
        self.closed = True


def serve(server, target: str, method="GET") -> str:
    connection = Connection(f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    google._Callback(connection, ("127.0.0.1", 5), server)
    return connection.out.getvalue().decode()


def waiting(state="st") -> Server:
    server = Server()
    Listener(server)
    server.state = state
    return server


def test_the_redirect_with_this_sign_ins_state_is_taken_and_answered_plainly():
    server = waiting()

    reply = serve(server, "/?state=st&code=4/abc&scope=a%20b")

    assert server.answer == {"state": "st", "code": "4/abc", "scope": "a b"}
    assert reply.startswith("HTTP/1.0 200") and "text/plain" in reply
    assert "You can close this tab" in reply and "4/abc" not in reply


def test_a_refusal_is_taken_too_and_the_page_says_so():
    server = waiting()

    reply = serve(server, "/?state=st&error=access_denied")

    assert server.answer["error"] == "access_denied" and "did not grant access" in reply


@pytest.mark.parametrize(
    ("target", "status"),
    [
        ("/?state=other&code=x", 400),
        ("/?state=st%C3%A9&code=x", 400),  # not even ASCII: refused, not an error
        ("/?code=x", 400),
        ("/favicon.ico", 404),
        ("/oauth2callback?state=st&code=x", 404),
    ],
)
def test_anything_else_is_refused_and_does_not_end_the_wait(target, status):
    server = waiting()

    assert serve(server, target).startswith(f"HTTP/1.0 {status}")
    assert server.answer is None


def test_nothing_is_answered_before_a_sign_in_is_waiting():
    server = Server()
    Listener(server)

    assert serve(server, "/?state=&code=x").startswith("HTTP/1.0 400")
    assert server.answer is None


def test_only_a_get_is_served():
    server = waiting()

    assert serve(server, "/?state=st&code=x", method="POST").startswith("HTTP/1.0 501")
    assert server.answer is None


def test_the_request_line_is_never_logged(capsys):
    serve(waiting(), "/?state=st&code=4/secret-code")

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_a_stalled_request_cannot_hold_the_listener():
    assert google._Callback.timeout == google.CALLBACK_READ_TIMEOUT_S


def test_the_listener_waits_past_strays_for_the_right_request():
    server = Server(["/favicon.ico", "/?state=wrong&code=x", "/?state=st&code=4/abc"])
    listener = Listener(server)

    assert listener.redirect_uri == "http://127.0.0.1:49152"
    assert listener.wait("st", 60)["code"] == "4/abc"
    assert len(server.timeouts) == 3 and all(0 < t <= 60 for t in server.timeouts)
    listener.close()
    assert server.closed


def test_a_browser_that_never_comes_back_times_out():
    ticks = iter([0.0, 10.0, 400.0])

    with pytest.raises(GoogleSetupError, match="did not come back within 5 minutes.*--headless"):
        Listener(Server()).wait("st", 300, clock=lambda: next(ticks))


def test_a_port_that_cannot_be_bound_says_to_paste_instead(monkeypatch):
    def refuse(address, handler):
        assert address == ("127.0.0.1", 0)  # this machine only, a port the OS picks
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(google, "_Server", refuse)

    with pytest.raises(GoogleSetupError, match="could not listen on 127.0.0.1.*--headless"):
        google.listen()


def test_the_listener_is_bound_to_loopback_only(monkeypatch):
    monkeypatch.setattr(google, "_Server", lambda address, handler: Server())

    assert isinstance(google.listen(), Listener)


def test_a_request_that_breaks_puts_nothing_on_the_terminal(capsys):
    google._Server.handle_error(Server(), None, ("127.0.0.1", 5))

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    ("opens", "opened"),
    [(lambda url: True, True), (lambda url: False, False), (None, False)],
)
def test_opening_the_browser_says_whether_it_did(monkeypatch, opens, opened):
    import webbrowser

    def fail(url):
        raise webbrowser.Error("no browser")

    monkeypatch.setattr(webbrowser, "open", opens or fail)

    assert google.open_browser("https://example.com") is opened


# --- the whole sign-in, with a browser on this machine ------------------------------------


class ScriptedListener:
    redirect_uri = "http://127.0.0.1:49152"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.closed = answer or {}, error, False

    def wait(self, state, timeout_s):
        self.waited = (state, timeout_s)
        if self.error:
            raise self.error
        return {"state": state, "code": "4/abc", **self.answer}

    def close(self):
        self.closed = True


def browser(monkeypatch, listener, opens=True):
    opened = []
    monkeypatch.setattr(google, "listen", lambda: listener)
    monkeypatch.setattr(google, "open_browser", lambda url: opened.append(url) or opens)
    return opened


def test_a_browser_sign_in_listens_opens_shows_the_link_and_exchanges(client, monkeypatch):
    listener = ScriptedListener()
    opened = browser(monkeypatch, listener)
    shown, post = [], FakePost()

    result = google.browser_signin(
        client, announce=lambda url, did: shown.append((url, did)), post=post
    )

    [(url, did)] = shown
    assert opened == [url] and did is True
    assert link_query(url)["redirect_uri"] == "http://127.0.0.1:49152"
    assert listener.waited == (link_query(url)["state"], google.CALLBACK_TIMEOUT_S)
    assert post.calls[0][1]["redirect_uri"] == "http://127.0.0.1:49152"
    assert result.connected == ALL and listener.closed
    assert not (client.data_dir / PENDING_FILE).exists()  # nothing waits on disk


def test_the_link_is_shown_even_when_no_browser_opens(client, monkeypatch):
    browser(monkeypatch, ScriptedListener(), opens=False)
    shown = []

    google.browser_signin(client, announce=lambda url, did: shown.append(did), post=FakePost())

    assert shown == [False]


def test_the_listener_is_shut_when_the_wait_fails(client, monkeypatch):
    listener = ScriptedListener(error=GoogleSetupError("the browser did not come back"))
    browser(monkeypatch, listener)

    with pytest.raises(GoogleSetupError, match="did not come back"):
        google.browser_signin(client, announce=lambda url, did: None, post=FakePost())
    assert listener.closed and account.read_token(client) is None


def test_a_refusal_in_the_browser_is_said(client, monkeypatch):
    browser(monkeypatch, ScriptedListener(answer={"error": "access_denied"}))

    with pytest.raises(GoogleSetupError, match="Google said: access_denied"):
        google.browser_signin(client, announce=lambda url, did: None, post=FakePost())


# --- the client file ----------------------------------------------------------------------


def test_install_client_file_refuses_what_it_cannot_read(settings, tmp_path):
    with pytest.raises(GoogleSetupError, match="could not read"):
        google.install_client_file(settings, tmp_path / "missing.json")


def test_install_client_file_keeps_a_private_copy(settings, tmp_path):
    source = tmp_path / "client_secret_1.json"
    source.write_text('{"installed": {"client_id": "i", "client_secret": "s"}}')

    path = google.install_client_file(settings, source)

    assert path == settings.config_dir / "google_client_secret.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_each_service_gets_a_line_saying_whether_it_is_connected():
    lines = google.service_lines(["gmail"], ["gmail", "calendar"])

    assert lines == [
        (True, "Gmail: connected — agents read, label, draft and send mail, and email answers "
               "on a call"),
        (False, "Calendar: not connected"),
    ]
