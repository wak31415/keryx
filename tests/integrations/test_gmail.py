"""Tests for `check_email`'s integration: a day, a search, answered mail, the model call.

No network: Gmail is a fake keyed by API path (and, for `HttpGmail`, an httpx
MockTransport); the model is a fake summariser, or a fake runner standing in for the
`claude` CLI. Mail in here is invented.
"""

import asyncio
import base64
import json
import logging
import stat
import sys
from datetime import datetime

import httpx
import pytest

from jarvis.integrations import gmail
from jarvis.integrations.gmail import (
    DAY_PROMPT,
    MAX_BODY_CHARS,
    SEARCH_PROMPT,
    SMART_FILTER,
    ClaudeCliSummariser,
    Email,
    EmailError,
    EmailReader,
    HttpGmail,
    body_text,
    build_email_reader,
    build_query,
    collect,
    day_window,
    fold_search,
    fold_thread,
    search,
    token_path,
    worth_reading,
)

NOW = datetime(2026, 9, 24, 15, 0).astimezone()
START, END, _ = day_window("yesterday", NOW)
ME = "owner@example.org"


def at(hours: float) -> str:
    """An internalDate `hours` after the start of yesterday, in Gmail's milliseconds."""
    return str(int((START + hours * 3600) * 1000))


def message(
    mid: str,
    thread: str,
    hours: float,
    *,
    sender: str = "Ann Lee <ann@example.org>",
    to: str = ME,
    subject: str = "The grant report",
    snippet: str = "Could you send the numbers by Friday?",
    labels: tuple[str, ...] = ("INBOX",),
    mailing_list: bool = False,
    sent: bool = False,
) -> dict:
    headers = [
        {"name": "From", "value": ME if sent else sender},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Wed, 23 Sep 2026 10:00:00 -0400"},
    ]
    if mailing_list:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:x@example.org>"})
    return {
        "id": mid,
        "threadId": thread,
        "internalDate": at(hours),
        "labelIds": [*labels, "SENT"] if sent else list(labels),
        "snippet": snippet,
        "payload": {"headers": headers},
    }


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def plain(text: str) -> dict:
    return {
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")},
    }


class FakeGmail:
    """Gmail by path: the listing, the profile, whole threads and full messages."""

    def __init__(self, threads: dict[str, list[dict]], *, bodies: dict | None = None) -> None:
        self.threads = threads
        self.bodies = bodies or {}
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "messages":
            return {
                "messages": [
                    {"id": m["id"], "threadId": tid}
                    for tid, msgs in self.threads.items()
                    for m in msgs
                ]
            }
        if path == "profile":
            return {"emailAddress": ME}
        if path.startswith("threads/"):
            tid = path.removeprefix("threads/")
            return {"id": tid, "messages": list(reversed(self.threads[tid]))}
        mid = path.removeprefix("messages/")
        return {"payload": self.bodies.get(mid, plain(f"full text of {mid}"))}

    def fetched(self, prefix: str) -> list[str]:
        return [path for path, _ in self.calls if path.startswith(prefix)]


class FakeSummariser:
    def __init__(self, answer="Ann needs the grant numbers by Friday.", delay=0.0):
        self.answer = answer
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    async def summarise(self, system, prompt):
        self.calls.append((system, prompt))
        await asyncio.sleep(self.delay)
        return self.answer


@pytest.fixture
def fixed_day(monkeypatch):
    """The reader asks for "yesterday" as of NOW, whatever the machine's clock says."""
    real = gmail.day_window
    monkeypatch.setattr(gmail, "day_window", lambda day, now=None: real(day, NOW))


async def collected(threads, **kw):
    return await collect(FakeGmail(threads, **kw), "yesterday", None, now=NOW)


# ------------------------------------------------------------------ threads, once each


async def test_a_thread_is_one_entry_its_newest_message_from_someone_else():
    threads = {
        "t1": [
            message("m1", "t1", 1, snippet="first"),
            message("m2", "t1", 2, snippet="second"),
            message("m3", "t1", 3, snippet="third"),
        ]
    }

    result, label = await collected(threads)

    assert [email.message_id for email in result.emails] == ["m3"]
    assert result.threads == 1
    assert label.startswith("yesterday, ")


async def test_a_thread_they_answered_is_left_out_even_answered_the_next_day():
    threads = {
        "answered": [message("a1", "answered", 2), message("a2", "answered", 30, sent=True)],
        "open": [message("o1", "open", 3)],
    }

    result, _ = await collected(threads)

    assert [email.thread_id for email in result.emails] == ["open"]
    assert result.answered == 1


async def test_a_new_message_after_their_reply_is_a_to_do_again_and_says_so():
    threads = {
        "t": [
            message("m1", "t", 1),
            message("m2", "t", 2, sent=True),
            message("m3", "t", 5, snippet="One more thing"),
        ]
    }

    result, _ = await collected(threads)

    [email] = result.emails
    assert (email.message_id, email.replied_earlier) == ("m3", True)
    assert "(They wrote in this thread earlier.)" in email.render()


async def test_a_thread_that_matched_only_on_their_own_message_is_neither():
    result, _ = await collected({"t": [message("m1", "t", 2, sent=True)]})

    assert (result.emails, result.answered) == ([], 0)


def test_a_reply_that_arrived_after_the_day_is_not_that_days_entry():
    thread = {
        "id": "t",
        "messages": [message("m1", "t", 2, snippet="in"), message("m2", "t", 30, snippet="out")],
    }

    email, answered = fold_thread(thread, START, END)

    assert (email.message_id, answered) == ("m1", False)


def test_nothing_from_anyone_else_in_the_window_is_no_entry():
    assert fold_thread({"id": "t", "messages": []}, START, END) == (None, False)


# ------------------------------------------------------------------------ full text


async def test_the_promising_threads_get_their_full_text_and_the_rest_a_snippet():
    threads = {
        "person": [message("p1", "person", 1)],
        "list": [message("l1", "list", 2, mailing_list=True, sender="News <news@x.org>")],
        "robot": [message("r1", "robot", 3, sender="GitHub <noreply@github.com>")],
    }
    api = FakeGmail(threads, bodies={"p1": plain("Hi,\nthe numbers by Friday please.")})

    result, _ = await collect(api, "yesterday", None, now=NOW)

    read = {email.thread_id: email.body for email in result.emails}
    assert read == {"person": "Hi,\nthe numbers by Friday please.", "list": None, "robot": None}
    assert api.fetched("messages/") == ["messages/p1"]


async def test_the_most_promising_are_read_first_when_there_are_too_many(monkeypatch):
    monkeypatch.setattr(gmail, "MAX_FULL", 2)
    threads = {
        "plain": [message("a", "plain", 1, to="someone-else@example.org")],
        "important": [message("b", "important", 2, labels=("INBOX", "IMPORTANT"))],
        "direct": [message("c", "direct", 3)],
    }
    api = FakeGmail(threads)

    await collect(api, "yesterday", None, now=NOW)

    assert sorted(api.fetched("messages/")) == ["messages/b", "messages/c"]


def test_an_important_mailing_list_is_still_worth_reading():
    email = Email("t", "m", "News <n@x.org>", ME, "s", "d", "", ("IMPORTANT",), True, False)

    assert worth_reading(email, ME) > 0


async def test_the_search_is_the_day_the_smart_filter_and_their_terms():
    api = FakeGmail({})

    await collect(api, "yesterday", "  from:ann   is:unread ", now=NOW)

    params = dict(api.calls)["messages"]
    assert params["q"] == f"after:{START} before:{END} {SMART_FILTER} from:ann is:unread"
    assert build_query(1, 2, None) == f"after:1 before:2 {SMART_FILTER}"


def test_today_and_yesterday_are_local_days():
    today, _, label = day_window("today", NOW)
    yesterday, end, _ = day_window("yesterday", NOW)

    assert end == today and today - yesterday == 86400
    assert label == f"today, {datetime.fromtimestamp(today):%A %d %B}"


# ------------------------------------------------------------------------ body text


def test_the_body_is_the_plain_part_without_the_quoted_reply():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            plain("Yes, Friday works.\n\nOn Tue, Sep 22, Ann wrote:\n> can you do Friday?"),
            {"mimeType": "text/html", "body": {"data": b64(b"<b>x</b>")}},
        ],
    }

    assert body_text(payload) == "Yes, Friday works."


def test_html_alone_is_stripped_to_its_words():
    html_body = b"<style>p{}</style><p>Deadline &amp; budget</p><script>x()</script>"
    payload = {"mimeType": "text/html", "body": {"data": b64(html_body)}}

    assert body_text(payload) == "Deadline & budget"


def test_quoted_lines_go_and_a_long_body_is_cut_short():
    assert body_text(plain("mine\n> theirs\nmine too")) == "mine\nmine too"
    long = body_text(plain("word " * 2000))
    assert len(long) <= MAX_BODY_CHARS + 2 and long.endswith(" …")


# ------------------------------------------------------------------------- a day


async def test_a_day_is_one_model_call_over_its_unanswered_threads(fixed_day):
    threads = {
        "person": [message("p1", "person", 1)],
        "list": [message("l1", "list", 2, mailing_list=True, snippet="Weekly news")],
    }
    summariser = FakeSummariser()

    result = await EmailReader(FakeGmail(threads), summariser).ask(
        "anything about the grant?", day="yesterday"
    )

    [(system, prompt)] = summariser.calls
    assert system == DAY_PROMPT
    assert prompt.startswith("Their question: anything about the grant?")
    assert "2 unanswered threads" in prompt
    assert "Full text:\nfull text of p1" in prompt and "Snippet: Weekly news" in prompt
    assert result["status"] == "ok"
    assert result["answer"] == "Ann needs the grant numbers by Friday."
    assert (result["threads"], result["already_answered"]) == (2, 0)
    assert result["scope"].startswith("yesterday, ")


async def test_an_empty_day_is_said_without_a_model_call(fixed_day):
    summariser = FakeSummariser()
    answered = {"t": [message("a1", "t", 2), message("a2", "t", 3, sent=True)]}

    empty = await EmailReader(FakeGmail({}), summariser).ask("", day="yesterday")
    replied = await EmailReader(FakeGmail(answered), summariser).ask(
        "", query="from:ann", day="yesterday"
    )

    assert empty["answer"] == "Nothing in yesterday's email needs attention."
    assert replied["answer"] == (
        "Nothing in yesterday's email needs attention that matches that, and everything "
        "else is already answered."
    )
    assert summariser.calls == []


async def test_an_answer_that_takes_too_long_is_stopped_and_said(fixed_day):
    threads = {"t": [message("m", "t", 1)]}
    reader = EmailReader(FakeGmail(threads), FakeSummariser(delay=5), timeout_s=0.05)

    with pytest.raises(EmailError) as raised:
        await reader.ask("anything?", day="yesterday")

    assert raised.value.code == "timeout"
    assert "handed to Claude" in raised.value.spoken


# --------------------------------------------------------------------------- a search


async def test_a_search_reads_the_newest_few_threads_in_full(monkeypatch):
    monkeypatch.setattr(gmail, "MAX_SEARCH_THREADS", 2)
    threads = {
        "t1": [message("a", "t1", 1), message("b", "t1", 2)],
        "t2": [message("c", "t2", 3)],
        "t3": [message("d", "t3", 4)],
    }
    api = FakeGmail(threads)

    emails = await search(api, "  from:ann   kickoff ")

    assert dict(api.calls)["messages"]["q"] == "from:ann kickoff"
    assert [email.message_id for email in emails] == ["b", "c"]
    assert all(email.body is not None for email in emails)
    assert sorted(api.fetched("messages/")) == ["messages/b", "messages/c"]


def test_a_searched_thread_they_answered_is_kept_and_says_so():
    thread = {"id": "t", "messages": [message("m1", "t", 1), message("m2", "t", 2, sent=True)]}

    email = fold_search(thread)

    assert (email.message_id, email.replied_after) == ("m1", True)
    assert "(They have replied to this.)" in email.render()


def test_a_searched_thread_of_only_their_own_mail_is_their_message():
    thread = {"id": "t", "messages": [message("m1", "t", 1, sent=True)]}

    assert fold_search(thread).message_id == "m1"
    assert fold_search({"id": "t", "messages": []}) is None


async def test_a_question_without_a_day_is_a_search_answered_in_a_sentence_or_three():
    summariser = FakeSummariser("Ann asked for the numbers by Friday; you already replied.")
    threads = {"t": [message("m1", "t", 1), message("m2", "t", 2, sent=True)]}
    api = FakeGmail(threads)

    result = await EmailReader(api, summariser).ask("did Ann write about the grant?")

    [(system, prompt)] = summariser.calls
    assert system == SEARCH_PROMPT
    assert dict(api.calls)["messages"]["q"] == "did Ann write about the grant?"
    assert "(They have replied to this.)" in prompt
    assert result == {
        "status": "ok",
        "scope": "search",
        "answer": "Ann asked for the numbers by Friday; you already replied.",
        "threads": 1,
    }


async def test_a_search_that_finds_nothing_is_said_without_a_model_call():
    summariser = FakeSummariser()

    result = await EmailReader(FakeGmail({}), summariser).ask("x", query="from:nobody")

    assert result["answer"] == "No email matches that search."
    assert summariser.calls == []


# ----------------------------------------------------------------------- HttpGmail


def token_file(tmp_path, **extra):
    path = tmp_path / "gmail_token.json"
    data = {"client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken", **extra}
    path.write_text(json.dumps(data))
    return path


def transport(token_responses, api_responses, seen):
    tokens, apis = list(token_responses), list(api_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return tokens.pop(0)
        return apis.pop(0)

    return httpx.MockTransport(handler)


def ok_token(value="at-1", expires=3600):
    return httpx.Response(200, json={"access_token": value, "expires_in": expires})


async def test_the_access_token_is_refreshed_once_and_reused(tmp_path):
    seen: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=transport([ok_token()], [httpx.Response(200, json={"a": 1})] * 2, seen)
    )
    api = HttpGmail(token_file(tmp_path), client=client)

    assert await api.get("profile") == {"a": 1}
    assert await api.get("profile") == {"a": 1}

    assert [r.url.host for r in seen].count("oauth2.googleapis.com") == 1
    assert seen[-1].headers["Authorization"] == "Bearer at-1"
    assert b"refresh_token=rtoken" in seen[0].content


async def test_an_expired_access_token_is_refreshed(tmp_path):
    clock = [0.0]
    seen: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=transport(
            [ok_token("at-1", 120), ok_token("at-2")], [httpx.Response(200, json={})] * 2, seen
        )
    )
    api = HttpGmail(token_file(tmp_path), client=client, clock=lambda: clock[0])

    await api.get("profile")
    clock[0] = 100.0
    await api.get("profile")

    assert seen[-1].headers["Authorization"] == "Bearer at-2"


async def test_a_401_refreshes_once_and_then_is_believed(tmp_path):
    seen: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=transport(
            [ok_token("at-1"), ok_token("at-2")],
            [httpx.Response(401), httpx.Response(200, json={"ok": True})],
            seen,
        )
    )
    assert await HttpGmail(token_file(tmp_path), client=client).get("profile") == {"ok": True}

    client = httpx.AsyncClient(
        transport=transport([ok_token(), ok_token()], [httpx.Response(401)] * 2, [])
    )
    with pytest.raises(EmailError) as raised:
        await HttpGmail(token_file(tmp_path), client=client).get("profile")
    assert raised.value.code == "signed_out"


@pytest.mark.parametrize(
    ("token_response", "api_response", "code"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), None, "signed_out"),
        (httpx.Response(500, text="nope"), None, "gmail_failed"),
        (httpx.Response(200, json={}), None, "gmail_failed"),
        (ok_token(), httpx.Response(500), "gmail_failed"),
    ],
)
async def test_every_failure_is_a_code_and_a_sentence(tmp_path, token_response, api_response, code):
    apis = [api_response] if api_response is not None else []
    client = httpx.AsyncClient(transport=transport([token_response], apis, []))

    with pytest.raises(EmailError) as raised:
        await HttpGmail(token_file(tmp_path), client=client).get("profile")

    assert raised.value.code == code
    assert raised.value.spoken


async def test_a_missing_or_broken_token_file_is_not_configured(tmp_path):
    with pytest.raises(EmailError) as raised:
        await HttpGmail(tmp_path / "missing.json").get("profile")
    assert raised.value.code == "not_configured"


async def test_the_network_failing_is_gmail_failed(tmp_path):
    def broken(request):
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    with pytest.raises(EmailError) as raised:
        await HttpGmail(token_file(tmp_path), client=client).get("profile")
    assert raised.value.code == "gmail_failed"

    ok_then_broken = transport([ok_token()], [], [])

    def second(request):
        if request.url.host == "oauth2.googleapis.com":
            return ok_then_broken.handler(request)
        raise httpx.ReadTimeout("slow")

    client = httpx.AsyncClient(transport=httpx.MockTransport(second))
    with pytest.raises(EmailError) as raised:
        await HttpGmail(token_file(tmp_path), client=client).get("profile")
    assert raised.value.code == "gmail_failed"


async def test_no_credential_reaches_the_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    client = httpx.AsyncClient(
        transport=transport([httpx.Response(400, json={"error": "invalid_grant"})], [], [])
    )
    with pytest.raises(EmailError) as raised:
        await HttpGmail(token_file(tmp_path), client=client).get("profile")

    assert "rtoken" not in caplog.text + str(raised.value)
    assert "csecret" not in caplog.text + str(raised.value)


# ----------------------------------------------------------------- the model call


class FakeRun:
    def __init__(self, code=0, out=None, err=""):
        self.code, self.err = code, err
        self.out = json.dumps({"result": "Ann needs the numbers."}) if out is None else out
        self.calls: list[tuple[list[str], str, dict]] = []

    async def __call__(self, argv, stdin, env):
        self.calls.append((list(argv), stdin, env))
        return self.code, self.out, self.err


async def test_the_summary_is_one_bare_cli_call_on_the_claude_sign_in(settings):
    settings.anthropic_api_key = "sk-ant-key"
    settings.email_model = "claude-opus-5-5"
    run = FakeRun()

    text = await ClaudeCliSummariser("/bin/claude", settings, run=run).summarise("sys", "mail")

    [(argv, stdin, env)] = run.calls
    assert text == "Ann needs the numbers."
    assert argv[:2] == ["/bin/claude", "-p"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "sys"
    assert stdin == "mail"
    assert env == {"ANTHROPIC_API_KEY": "sk-ant-key"}
    assert "sk-ant-key" not in " ".join(argv)


@pytest.mark.parametrize(
    "run",
    [
        FakeRun(code=1, out="", err="auth failed for sk-ant-key"),
        FakeRun(out=json.dumps({"result": "boom", "is_error": True})),
        FakeRun(out="not json"),
    ],
)
async def test_a_failed_summary_is_model_failed_and_never_quotes_the_key(settings, run):
    settings.anthropic_api_key = "sk-ant-key"

    with pytest.raises(EmailError) as raised:
        await ClaudeCliSummariser("/bin/claude", settings, run=run).summarise("s", "p")

    assert raised.value.code == "model_failed"
    assert "sk-ant-key" not in raised.value.detail


async def test_the_real_runner_feeds_stdin_and_returns_what_came_out():
    code, out, err = await gmail._run_cli(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], "hello", {}
    )

    assert (code, out.strip(), err) == (0, "HELLO", "")


async def test_a_cancelled_runner_kills_its_process():
    task = asyncio.create_task(
        gmail._run_cli([sys.executable, "-c", "import time; time.sleep(30)"], "", {})
    )
    await asyncio.sleep(0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


# --------------------------------------------------------------------------- build


def test_no_sign_in_means_no_tool(settings):
    assert build_email_reader(settings) is None


def test_no_claude_cli_means_no_tool(settings, monkeypatch):
    token_path(settings).parent.mkdir(parents=True, exist_ok=True)
    token_path(settings).write_text("{}")
    monkeypatch.setattr("jarvis.agents.registry.installed", lambda agent: False)

    assert build_email_reader(settings) is None


def test_a_sign_in_and_a_cli_is_the_tool_with_its_token_kept_private(settings, monkeypatch):
    path = token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    path.chmod(0o644)
    monkeypatch.setattr("jarvis.agents.registry.installed", lambda agent: True)
    spec = gmail_backend_with_cli(monkeypatch)

    assert isinstance(build_email_reader(settings), EmailReader)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert spec.find_cli() == "/bin/claude"


def gmail_backend_with_cli(monkeypatch):
    import dataclasses

    from jarvis.agents.registry import BACKENDS

    spec = dataclasses.replace(BACKENDS["claude"], find_cli=lambda: "/bin/claude")
    monkeypatch.setitem(BACKENDS, "claude", spec)
    return spec
