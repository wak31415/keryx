"""`keryx.config.verify`: a key or credential, checked before it is saved.

Every check is faked — the wizard's OpenAI probe as a function, Anthropic as an HTTP getter
shaped like `httpx.get`, Twilio as an admin factory — except where the real checks run over
faked transports, to show a key planted in a body or an error never comes back out.
"""

import json

import httpx
import pytest

from keryx.config import verify
from keryx.config.verify import Checks, Verified
from keryx.notify.twilio_out import TwilioError

KEY = "sk-planted-key-0123456789abcdef"
SID = "AC0123456789abcdef0123456789abcdef"


class FakeGet:
    """An `httpx.get` that records what it was asked and answers as it was told."""

    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.raises is not None:
            raise self.raises
        request = httpx.Request("GET", url)
        return httpx.Response(self.status, json=self.body, request=request)


class FakeAdmin:
    def __init__(self, name="Ada's account", error=None):
        self.name, self.error = name, error

    def account_name(self):
        if self.error is not None:
            raise self.error
        return self.name


class Recorder:
    """Records what each check was handed; answers with what it was told."""

    def __init__(self, openai=None, get=None, admin=None):
        self.openai_answer = openai
        self.get = get or FakeGet(body={"data": []})
        self.admin = admin or FakeAdmin()
        self.openai_keys: list[str] = []
        self.twilio_pairs: list[tuple[str, str]] = []

    def checks(self) -> Checks:
        def openai(key):
            self.openai_keys.append(key)
            return self.openai_answer

        def twilio(sid, token):
            self.twilio_pairs.append((sid, token))
            return self.admin

        return Checks(openai_key_problem=openai, get=self.get, twilio=twilio)


def test_as_dict_is_the_contract():
    assert Verified("OPENAI_API_KEY", True, None, None).as_dict() == {
        "key": "OPENAI_API_KEY", "ok": True, "problem": None, "detail": None,
    }


def test_the_keys_that_can_be_checked():
    assert set(verify.SUPPORTED) == {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY", "TWILIO_AUTH_TOKEN",
    }
    assert verify.can_verify("OPENAI_API_KEY")
    assert not verify.can_verify("TWILIO_ACCOUNT_SID")
    assert not verify.can_verify("OPENAI_REALTIME_MODEL")


def test_an_unsupported_key_is_refused(settings):
    with pytest.raises(verify.Unsupported):
        verify.verify("TWILIO_ACCOUNT_SID", "AC1", settings, Recorder().checks())


# --- OpenAI, and Codex's key, which is an OpenAI one -----------------------------------


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "CODEX_API_KEY"])
def test_an_openai_key_that_works(settings, key):
    fake = Recorder(openai=None)

    result = verify.verify(key, KEY, settings, fake.checks())

    assert result == Verified(key, True, None, None)
    assert fake.openai_keys == [KEY]


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "CODEX_API_KEY"])
@pytest.mark.parametrize(
    ("said", "problem"),
    [("OpenAI does not recognise that key", "OpenAI does not recognise that key."),
     ("could not reach OpenAI (ConnectError)", "Could not reach OpenAI (ConnectError)."),
     ("OpenAI answered HTTP 500", "OpenAI answered HTTP 500.")],
)
def test_an_openai_key_that_does_not_is_one_sentence(settings, key, said, problem):
    result = verify.verify(key, KEY, settings, Recorder(openai=said).checks())

    assert result == Verified(key, False, problem, None)


def test_the_stored_openai_key_is_checked_when_none_is_given(settings):
    fake = Recorder()
    keyed = settings.model_copy(update={"openai_api_key": KEY})

    assert verify.verify("OPENAI_API_KEY", None, keyed, fake.checks()).ok
    assert fake.openai_keys == [KEY]


def test_a_given_key_is_checked_rather_than_the_stored_one(settings):
    fake = Recorder()
    keyed = settings.model_copy(update={"codex_api_key": "sk-stored"})

    verify.verify("CODEX_API_KEY", KEY, keyed, fake.checks())

    assert fake.openai_keys == [KEY]


@pytest.mark.parametrize(
    ("key", "field"),
    [("OPENAI_API_KEY", "openai_api_key"), ("CODEX_API_KEY", "codex_api_key"),
     ("ANTHROPIC_API_KEY", "anthropic_api_key"), ("TWILIO_AUTH_TOKEN", "twilio_auth_token")],
)
def test_nothing_to_check_is_a_result_and_asks_nobody(settings, key, field):
    fake = Recorder()
    bare = settings.model_copy(update={field: None, "twilio_account_sid": SID})

    result = verify.verify(key, None, bare, fake.checks())

    assert result == Verified(key, False, f"There's no {key} to check.", None)
    assert (fake.openai_keys, fake.get.calls, fake.twilio_pairs) == ([], [], [])


def test_the_read_only_loaders_stand_in_openai_key_is_not_a_key(settings):
    from keryx.config.settings import PLACEHOLDER_KEY

    fake = Recorder()
    standing_in = settings.model_copy(update={"openai_api_key": PLACEHOLDER_KEY})

    assert not verify.verify("OPENAI_API_KEY", None, standing_in, fake.checks()).ok
    assert fake.openai_keys == []


# --- Anthropic ---------------------------------------------------------------------------


def test_an_anthropic_key_that_works_is_one_get_that_follows_no_redirect(settings):
    get = FakeGet(body={"data": [{"id": "claude-opus-5-5"}]})

    result = verify.verify("ANTHROPIC_API_KEY", KEY, settings, Recorder(get=get).checks())

    assert result == Verified("ANTHROPIC_API_KEY", True, None, None)
    [(url, kwargs)] = get.calls
    assert url.startswith("https://api.anthropic.com/v1/models")
    assert kwargs["headers"] == {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
    assert kwargs["follow_redirects"] is False
    assert kwargs["timeout"] == verify.TIMEOUT_S


@pytest.mark.parametrize(
    ("get", "problem"),
    [(FakeGet(401, {"error": KEY}), "Anthropic didn't accept the API key."),
     (FakeGet(403, {"error": KEY}), "Anthropic didn't accept the API key."),
     (FakeGet(529, {"error": KEY}), "Anthropic answered with HTTP 529."),
     (FakeGet(raises=httpx.ConnectError(KEY)), "Keryx couldn't reach Anthropic (ConnectError)."),
     (FakeGet(200, {"no": "list"}), "Anthropic didn't answer with a list of models.")],
)
def test_an_anthropic_key_that_does_not_is_one_sentence(settings, get, problem):
    result = verify.verify("ANTHROPIC_API_KEY", KEY, settings, Recorder(get=get).checks())

    assert result == Verified("ANTHROPIC_API_KEY", False, problem, None)


# --- Twilio ------------------------------------------------------------------------------


def test_a_twilio_token_is_checked_with_the_stored_sid_and_names_the_account(settings):
    fake = Recorder(admin=FakeAdmin("Ada's account"))
    stored = settings.model_copy(update={"twilio_account_sid": SID})

    result = verify.verify("TWILIO_AUTH_TOKEN", KEY, stored, fake.checks())

    assert result == Verified("TWILIO_AUTH_TOKEN", True, None, "Ada's account")
    assert fake.twilio_pairs == [(SID, KEY)]


def test_the_stored_twilio_token_is_checked_when_none_is_given(settings):
    fake = Recorder()
    stored = settings.model_copy(update={"twilio_account_sid": SID, "twilio_auth_token": KEY})

    assert verify.verify("TWILIO_AUTH_TOKEN", None, stored, fake.checks()).ok
    assert fake.twilio_pairs == [(SID, KEY)]


def test_a_twilio_token_without_a_stored_sid_asks_nobody(settings):
    fake = Recorder()
    bare = settings.model_copy(update={"twilio_account_sid": None})

    result = verify.verify("TWILIO_AUTH_TOKEN", KEY, bare, fake.checks())

    assert result == Verified(
        "TWILIO_AUTH_TOKEN", False,
        "There's no TWILIO_ACCOUNT_SID saved to check the token with.", None,
    )
    assert fake.twilio_pairs == []


@pytest.mark.parametrize(
    ("error", "problem"),
    [(TwilioError(KEY, status=401), "Twilio didn't accept the account SID and auth token."),
     (TwilioError(KEY, status=404), "Twilio didn't accept the account SID and auth token."),
     (TwilioError(KEY, status=500), "Twilio answered with HTTP 500."),
     (TwilioError(KEY, cause="ConnectionError"), "Keryx couldn't reach Twilio (ConnectionError)."),
     (TwilioError(KEY), "Keryx couldn't reach Twilio (TwilioError).")],
)
def test_a_twilio_token_that_does_not_work_is_one_sentence(settings, error, problem):
    stored = settings.model_copy(update={"twilio_account_sid": SID})
    fake = Recorder(admin=FakeAdmin(error=error))

    result = verify.verify("TWILIO_AUTH_TOKEN", KEY, stored, fake.checks())

    assert result == Verified("TWILIO_AUTH_TOKEN", False, problem, None)


def test_a_pair_checks_the_sid_given_not_the_one_saved(settings):
    fake = Recorder(admin=FakeAdmin("Ada's other account"))
    other = "AC" + "f" * 32
    stored = settings.model_copy(update={"twilio_account_sid": SID})

    result = verify.verify("TWILIO_AUTH_TOKEN", KEY, stored, fake.checks(), account_sid=other)

    assert result == Verified("TWILIO_AUTH_TOKEN", True, None, "Ada's other account")
    assert fake.twilio_pairs == [(other, KEY)]


def test_a_pair_with_no_sid_asks_nobody(settings):
    fake = Recorder()
    stored = settings.model_copy(update={"twilio_account_sid": SID})

    result = verify.verify("TWILIO_AUTH_TOKEN", KEY, stored, fake.checks(), account_sid="")

    assert result == Verified(
        "TWILIO_AUTH_TOKEN", False, "There's no TWILIO_ACCOUNT_SID to check the token with.",
        None,
    )
    assert fake.twilio_pairs == []


def test_a_pair_that_twilio_refuses_is_one_sentence(settings):
    fake = Recorder(admin=FakeAdmin(error=TwilioError(KEY, status=401)))

    result = verify.verify("TWILIO_AUTH_TOKEN", KEY, settings, fake.checks(), account_sid=SID)

    assert result == Verified(
        "TWILIO_AUTH_TOKEN", False, "Twilio didn't accept the account SID and auth token.", None
    )


def test_only_the_twilio_token_is_checked_as_a_pair(settings):
    with pytest.raises(verify.Unsupported):
        verify.verify("OPENAI_API_KEY", KEY, settings, Recorder().checks(), account_sid=SID)


def test_an_unexpected_failure_is_a_cause_never_its_text(settings):
    def openai(key):
        raise RuntimeError(f"boom {key}")

    checks = Checks(openai_key_problem=openai, get=FakeGet(), twilio=lambda s, t: FakeAdmin())
    result = verify.verify("OPENAI_API_KEY", KEY, settings, checks)

    assert result == Verified(
        "OPENAI_API_KEY", False, "Keryx couldn't check the key (RuntimeError).", None
    )


# --- the real checks, over faked transports -------------------------------------------------


def echoing(status):
    """httpx's transport, faked: every request answered with `status` and the key quoted."""

    def handle(self, request):
        return httpx.Response(status, json={"error": {"message": f"bad key {KEY}"}},
                              request=request)

    return handle


def twilio_answering(status, body):
    """The Twilio library's HTTP client, faked: every request answered with `status`."""
    from twilio.http.response import Response

    def request(self, method, url, *args, **kwargs):
        return Response(status, json.dumps(body))

    return request


@pytest.mark.parametrize("status", [401, 500])
def test_a_planted_key_never_comes_back_out_of_the_real_checks(settings, monkeypatch, status):
    from twilio.http.http_client import TwilioHttpClient

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", echoing(status))
    monkeypatch.setattr(TwilioHttpClient, "request", twilio_answering(
        status, {"code": 20003, "message": f"Authenticate {KEY}", "status": status}))
    stored = settings.model_copy(update={"twilio_account_sid": SID})

    for key in verify.SUPPORTED:
        result = verify.verify(key, KEY, stored)
        printed = json.dumps(result.as_dict())

        assert not result.ok
        assert KEY not in printed
        assert result.problem.endswith(".")


def test_the_real_twilio_check_names_the_account(settings, monkeypatch):
    from twilio.http.http_client import TwilioHttpClient

    monkeypatch.setattr(TwilioHttpClient, "request", twilio_answering(
        200, {"sid": SID, "friendly_name": "Ada's account", "status": "active"}))
    stored = settings.model_copy(update={"twilio_account_sid": SID})

    result = verify.verify("TWILIO_AUTH_TOKEN", KEY, stored)

    assert result == Verified("TWILIO_AUTH_TOKEN", True, None, "Ada's account")


def test_the_real_twilio_check_says_a_refusal(settings, monkeypatch):
    from twilio.http.http_client import TwilioHttpClient

    monkeypatch.setattr(TwilioHttpClient, "request", twilio_answering(
        401, {"code": 20003, "message": "Authenticate", "status": 401}))
    stored = settings.model_copy(update={"twilio_account_sid": SID})

    result = verify.verify("TWILIO_AUTH_TOKEN", KEY, stored)

    assert result.problem == "Twilio didn't accept the account SID and auth token."
