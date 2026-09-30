"""Questions about the owner's email, answered out loud in well under ten seconds.

Behind the `check_email` plugin (`jarvis.plugins.email`). Dispatching an email question to
a subagent took a median of five minutes (a Claude Code session reading the inbox one tool
call at a time); this is one Gmail pass and one model call. Two shapes:

**A day** (`day` is today or yesterday) — "what do I need to do from today's email":

1. Gmail's own search narrows the day before anything is fetched: the day's window,
   promotions/social/forums left out, plus the terms the question implied (`from:susan`,
   `is:unread`). The window is ours and always ANDed in.
2. One entry per thread. Every matching message is folded into its thread, read once,
   whole; the entry is the newest message *from someone else* inside the window. A thread
   whose later message is one the owner sent — even the next day — is already answered and
   left out: a to-do they have replied to is not a to-do.
3. A rule, not a model call, picks up to `MAX_FULL` threads worth reading in full (Gmail
   marked it important, it came to them directly, it is not a list or a no-reply sender);
   the rest ride along as a snippet.

**A search** (no `day`) — "did Susan answer about the kickoff", "when is the camera-ready
due": Gmail's search, which is good, with the terms the question implied (any Gmail
operator, `newer_than:7d` included); the newest `MAX_SEARCH_THREADS` matching threads are
read in full, answered or not — whether they replied is part of the answer, so it is said
rather than filtered.

Either way the mail goes to a Claude model through the bundled `claude` CLI — no tools, no
settings, no MCP servers, low effort — with their question, and comes back as a few spoken
sentences. On the owner's inbox a busy day was 4.5–6 s end to end (measured 2026-09-27,
Opus 5.5 at low effort; Sonnet 5 was as fast).

Read-only by construction: the token is `gmail.readonly`, and `GmailApi` has only `get`.
The credential never leaves this module: it is read from `data_dir/gmail_token.json` (0600,
written by `jarvis auth login gmail`), sent only to Google, and never logged; failures come back
as an `EmailError` with a sentence to say. Mail content is never logged either — counts only.
"""

import asyncio
import base64
import html
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from jarvis.agents.auth import child_env, redact, resolve_auth
from jarvis.config import Settings, secure_file

log = logging.getLogger("jarvis.integrations.gmail")

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_FILE = "gmail_token.json"

#: What never needs reading for "what do I have to do": Gmail's own automated tabs.
SMART_FILTER = "-category:promotions -category:social -category:forums"
#: How many matching messages one question may look at, and how many threads get their
#: full text. Past a few dozen threads a day, the list is not the bottleneck; the model is.
MAX_MESSAGES = 60
MAX_FULL = 12
#: A search reads this many of the newest matching threads, all in full. Gmail's ranking is
#: newest first, and the question is almost always about something recent.
MAX_SEARCH_THREADS = 5
MAX_BODY_CHARS = 2000
CONCURRENCY = 16
REQUEST_TIMEOUT_S = 15.0
#: The whole answer, fetch and model together. It is a wait inside a phone call.
ANSWER_TIMEOUT_S = 30.0
#: Refresh the access token this long before Google says it expires.
EXPIRY_MARGIN_S = 60.0

NO_REPLY = re.compile(
    r"no-?reply|do-?not-?reply|notifications?@|mailer-daemon|calendar-notification", re.I
)
_QUOTE_START = re.compile(
    r"^(On .{0,200}wrote:|-----Original Message-----|From: .+|_{10,})$", re.M
)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)

Day = Literal["today", "yesterday"]
ErrorCode = Literal["not_configured", "signed_out", "gmail_failed", "model_failed", "timeout"]

MESSAGES: dict[ErrorCode, str] = {
    "not_configured": "Email isn't set up on this machine yet. Say that in one sentence.",
    "signed_out": (
        "Jarvis has been signed out of Gmail, so it can't read the email. Say that in one "
        "sentence; it needs `jarvis auth login gmail` at the keyboard."
    ),
    "gmail_failed": "Gmail didn't answer just now. Say so in one sentence; trying again may work.",
    "model_failed": (
        "The email was read but the answer failed. Say so in one sentence; trying again "
        "may work, or it can be handed to Claude."
    ),
    "timeout": (
        "Reading the email took too long, so it was stopped. Say so in one sentence; "
        "it can be handed to Claude instead."
    ),
}

#: The instructions to the model. It is heard, not read, which is why they are so strict
#: about shape; and each says what the thread handling has already done, so it is not undone.
DAY_PROMPT = (
    "You pick out what matters from a day of someone's email, for them to hear read aloud "
    "on the phone. If they asked something specific, answer that, in a sentence or three, "
    "and nothing else. Each thread appears once, as its newest message from someone else; "
    "threads they have already answered are left out. Some messages come with their full "
    "text, the rest only with a snippet: trust the full text where you have it. Where a "
    "thread notes that they wrote in it earlier, do not list what they have plainly already "
    "handled. Reply with at most 6 items, most urgent first, one short spoken sentence each "
    "on its own line, naming who it is from: things to do, deadlines, replies owed, and "
    "anything important to know. Never list the same thing twice. Skip newsletters, "
    "receipts, calendar noise and notifications that need nothing, and do not mention "
    "them. No markdown, no bullets, no numbering, no preamble. If nothing needs attention, "
    "say so in one sentence."
)
SEARCH_PROMPT = (
    "You answer a question about someone's email from the few threads a search found, for "
    "them to hear read aloud on the phone. Answer in one to three short spoken sentences: "
    "say who and when where it matters, and whether they have already replied where the "
    "thread shows it. If the threads do not answer the question, say so plainly in one "
    "sentence and say what the search did find. Never invent anything that is not in the "
    "mail. No markdown, no bullets, no preamble."
)


class EmailError(Exception):
    """An email lookup that failed in a way worth saying out loud.

    `code` is what went wrong, `detail` is for the log (never a credential or mail content),
    and `spoken` is the sentence the voice model is handed.
    """

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        self.spoken = MESSAGES[code]
        super().__init__(detail or code)


# ------------------------------------------------------------------------------ gmail


class GmailApi(Protocol):
    """The Gmail REST API, read-only: a GET and nothing else (injectable for tests)."""

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict: ...


def token_path(settings: Settings) -> Path:
    return settings.data_dir / TOKEN_FILE


class HttpGmail(GmailApi):
    """Gmail over HTTPS with a refresh token; the access token is kept and reused.

    The token file is the one `jarvis auth login gmail` writes (the same keys a
    `google.oauth2.credentials.Credentials.to_json()` has, so either kind works). A refresh
    Google refuses (`invalid_grant`: revoked, or a Testing-mode app past its seven days) is
    `signed_out`, which the voice turns into "run jarvis auth login gmail".
    """

    def __init__(
        self,
        path: Path,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._client = client
        self._clock = clock
        self._access: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        return self._client

    async def _token(self, *, force: bool = False) -> str:
        async with self._lock:
            if self._access and not force and self._clock() < self._expires_at:
                return self._access
            try:
                creds = json.loads(self._path.read_text(encoding="utf-8"))
                data = {
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "refresh_token": creds["refresh_token"],
                    "grant_type": "refresh_token",
                }
                token_url = creds.get("token_uri") or TOKEN_URL
            except (OSError, ValueError, KeyError) as exc:
                raise EmailError("not_configured", f"unreadable {self._path.name}") from exc
            try:
                response = await self._http().post(token_url, data=data)
            except httpx.HTTPError as exc:
                raise EmailError("gmail_failed", f"token refresh: {type(exc).__name__}") from exc
            if response.status_code != 200:
                code = _json(response).get("error", response.status_code)
                if code in ("invalid_grant", "unauthorized_client", "invalid_client"):
                    raise EmailError("signed_out", f"token refresh refused: {code}")
                raise EmailError("gmail_failed", f"token refresh: {code}")
            body = _json(response)
            self._access = body.get("access_token")
            if not self._access:
                raise EmailError("gmail_failed", "token refresh returned no access token")
            lifetime = float(body.get("expires_in", 3600))
            self._expires_at = self._clock() + lifetime - EXPIRY_MARGIN_S
            return self._access

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict:
        for attempt in range(2):
            token = await self._token(force=attempt > 0)
            try:
                response = await self._http().get(
                    f"{GMAIL}/{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise EmailError("gmail_failed", f"{path}: {type(exc).__name__}") from exc
            if response.status_code == 401:
                if attempt == 0:
                    continue  # an access token revoked early: refresh once, then believe it
                raise EmailError("signed_out", f"{path}: HTTP 401 after a refresh")
            if response.status_code != 200:
                raise EmailError("gmail_failed", f"{path}: HTTP {response.status_code}")
            return _json(response)
        raise AssertionError("unreachable")  # pragma: no cover


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------- threads


@dataclass
class Email:
    """One thread, as the newest message in it from someone else."""

    thread_id: str
    message_id: str
    sender: str
    to: str
    subject: str
    date: str
    snippet: str
    labels: tuple[str, ...]
    mailing_list: bool
    #: They wrote in this thread before this message; the model is told not to re-list it.
    replied_earlier: bool
    #: They answered after this message (only a search keeps such a thread).
    replied_after: bool = False
    body: str | None = None

    def render(self) -> str:
        lines = [f"From: {self.sender}", f"Subject: {self.subject}", f"Date: {self.date}"]
        if self.replied_earlier:
            lines.append("(They wrote in this thread earlier.)")
        if self.replied_after:
            lines.append("(They have replied to this.)")
        if self.body is not None:
            lines.append(f"Full text:\n{self.body}")
        else:
            lines.append(f"Snippet: {self.snippet}")
        return "\n".join(lines)


@dataclass
class Collected:
    """What the day came to, before the model sees it."""

    threads: int
    answered: int
    emails: list[Email]


def day_window(day: Day, now: datetime | None = None) -> tuple[int, int, str]:
    """`(start, end, label)`: the local day as epoch seconds, and how to say which day it was."""
    now = (now or datetime.now()).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=1) if day == "yesterday" else midnight
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp()), f"{day}, {start:%A %d %B}"


def build_query(start: int, end: int, terms: str | None) -> str:
    """The Gmail search: the day's window and the smart filter, ANDed with their terms."""
    extra = " ".join((terms or "").split())
    return f"after:{start} before:{end} {SMART_FILTER} {extra}".strip()


def _headers(message: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])
    }


def _sent(message: dict) -> bool:
    return "SENT" in message.get("labelIds", [])


def fold_thread(thread: dict, start: int, end: int) -> tuple[Email | None, bool]:
    """`(entry, answered)` for one thread.

    The entry is the newest message from someone else that arrived inside the window. The
    thread is answered — no entry, `answered` True — when the owner wrote after it, however
    much later. A thread with nothing from anyone else in the window (it matched on a
    message they sent) is `(None, False)`: not a to-do, and not one they answered either.
    """
    messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
    incoming = [
        m
        for m in messages
        if not _sent(m) and start * 1000 <= int(m.get("internalDate", 0)) < end * 1000
    ]
    if not incoming:
        return None, False
    newest = incoming[-1]
    later = [m for m in messages if int(m.get("internalDate", 0)) > int(newest["internalDate"])]
    if any(_sent(m) for m in later):
        return None, True
    earlier = messages[: messages.index(newest)]
    h = _headers(newest)
    email = Email(
        thread_id=thread.get("id", ""),
        message_id=newest.get("id", ""),
        sender=h.get("from", ""),
        to=h.get("to", ""),
        subject=h.get("subject", ""),
        date=h.get("date", ""),
        snippet=newest.get("snippet", ""),
        labels=tuple(newest.get("labelIds", [])),
        mailing_list="list-unsubscribe" in h,
        replied_earlier=any(_sent(m) for m in earlier),
    )
    return email, False


def fold_search(thread: dict) -> Email | None:
    """A searched thread as its newest message from someone else (their own if there is none).

    Nothing is filtered out: whether they already replied is part of the answer to a
    question, so `replied_after` says it instead.
    """
    messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
    if not messages:
        return None
    incoming = [m for m in messages if not _sent(m)]
    newest = incoming[-1] if incoming else messages[-1]
    position = messages.index(newest)
    h = _headers(newest)
    return Email(
        thread_id=thread.get("id", ""),
        message_id=newest.get("id", ""),
        sender=h.get("from", ""),
        to=h.get("to", ""),
        subject=h.get("subject", ""),
        date=h.get("date", ""),
        snippet=newest.get("snippet", ""),
        labels=tuple(newest.get("labelIds", [])),
        mailing_list="list-unsubscribe" in h,
        replied_earlier=any(_sent(m) for m in messages[:position]),
        replied_after=any(_sent(m) for m in messages[position + 1 :]),
    )


def worth_reading(email: Email, me: str) -> int:
    """How much a thread deserves its full text: higher is sooner; 0 is not at all."""
    to = {address.lower() for _, address in getaddresses([email.to])}
    personal = not email.mailing_list and not NO_REPLY.search(email.sender)
    score = 0
    if "IMPORTANT" in email.labels:
        score += 4
    if me and me in to:
        score += 2
    if personal:
        score += 1
    return score if (personal or "IMPORTANT" in email.labels) else 0


def body_text(payload: dict) -> str:
    """A message's own words: its text/plain part, else its HTML stripped; no quoted history."""
    plain: list[str] = []
    rich: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            text = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            (plain if mime == "text/plain" else rich).append(text)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        text = "\n".join(plain)
    else:
        text = html.unescape(_TAG.sub(" ", _SCRIPT.sub(" ", "\n".join(rich))))
    if match := _QUOTE_START.search(text):
        text = text[: match.start()]
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS].rstrip() + " …"
    return text


async def collect(
    api: GmailApi, day: Day, terms: str | None, *, now: datetime | None = None
) -> tuple[Collected, str]:
    """The day's unanswered threads, the promising ones with their full text; and its label."""
    start, end, label = day_window(day, now)
    listed, profile = await asyncio.gather(
        api.get("messages", {"q": build_query(start, end, terms), "maxResults": MAX_MESSAGES}),
        api.get("profile"),
    )
    me = str(profile.get("emailAddress", "")).lower()
    thread_ids = list(dict.fromkeys(m["threadId"] for m in listed.get("messages", [])))
    gate = asyncio.Semaphore(CONCURRENCY)

    async def fetch(path: str, params: Mapping[str, Any]) -> dict:
        async with gate:
            return await api.get(path, params)

    wanted = ["From", "To", "Subject", "Date", "List-Unsubscribe"]
    threads = await asyncio.gather(
        *(
            fetch(f"threads/{tid}", {"format": "metadata", "metadataHeaders": wanted})
            for tid in thread_ids
        )
    )
    folded = [fold_thread(thread, start, end) for thread in threads]
    emails = [email for email, _ in folded if email is not None]
    answered = sum(1 for _, was_answered in folded if was_answered)

    ranked = sorted(
        (email for email in emails if worth_reading(email, me)),
        key=lambda email: worth_reading(email, me),
        reverse=True,
    )[:MAX_FULL]
    full = await asyncio.gather(
        *(fetch(f"messages/{email.message_id}", {"format": "full"}) for email in ranked)
    )
    for email, message in zip(ranked, full, strict=True):
        email.body = body_text(message.get("payload", {}))
    return Collected(threads=len(thread_ids), answered=answered, emails=emails), label


async def search(api: GmailApi, query: str) -> list[Email]:
    """The newest `MAX_SEARCH_THREADS` threads matching `query`, each read in full."""
    listed = await api.get("messages", {"q": " ".join(query.split()), "maxResults": 25})
    thread_ids = list(dict.fromkeys(m["threadId"] for m in listed.get("messages", [])))
    thread_ids = thread_ids[:MAX_SEARCH_THREADS]
    wanted = ["From", "To", "Subject", "Date", "List-Unsubscribe"]
    threads = await asyncio.gather(
        *(
            api.get(f"threads/{tid}", {"format": "metadata", "metadataHeaders": wanted})
            for tid in thread_ids
        )
    )
    emails = [email for email in map(fold_search, threads) if email is not None]
    full = await asyncio.gather(
        *(api.get(f"messages/{email.message_id}", {"format": "full"}) for email in emails)
    )
    for email, message in zip(emails, full, strict=True):
        email.body = body_text(message.get("payload", {}))
    return emails


# ------------------------------------------------------------------------------ model


class Summariser(Protocol):
    """One model call: some email and a question in, a few spoken sentences out."""

    async def summarise(self, system: str, prompt: str) -> str: ...


Runner = Callable[[Sequence[str], str, dict[str, str]], Any]


async def _run_cli(argv: Sequence[str], stdin: str, env: dict[str, str]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env},
    )
    try:
        out, err = await process.communicate(stdin.encode())
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class ClaudeCliSummariser(Summariser):
    """The bundled `claude` CLI in print mode, stripped to one model call.

    No tools, no settings, no MCP servers, no session written: nothing but the model. It
    signs in the way every Claude subagent does (`CLAUDE_AUTH`: an API key, a token, or the
    stored subscription login), handed over in the environment only.
    """

    def __init__(
        self,
        cli: str,
        settings: Settings,
        *,
        model: str,
        effort: str,
        run: Callable[..., Any] = _run_cli,
    ) -> None:
        self._cli = cli
        self._settings = settings
        self._model = model
        self._effort = effort
        self._run = run

    async def summarise(self, system: str, prompt: str) -> str:
        from jarvis.agents.claude import CLAUDE_AUTH

        auth = resolve_auth(CLAUDE_AUTH, self._settings, probe=False)
        argv = [
            self._cli, "-p",
            "--model", self._model,
            "--effort", self._effort,
            "--output-format", "json",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--tools", "",
            "--no-session-persistence",
            "--system-prompt", system,
        ]  # fmt: skip
        code, out, err = await self._run(argv, prompt, child_env(auth))
        try:
            result = json.loads(out) if out.strip() else {}
        except ValueError:
            result = {}
        text = str(result.get("result") or "").strip()
        if code != 0 or result.get("is_error") or not text:
            why = redact((err or str(result.get("result") or ""))[-300:], [auth.secret])
            raise EmailError("model_failed", f"claude exited {code}: {why}")
        return text


# ----------------------------------------------------------------------------- answer


class EmailReader:
    """The whole answer: a day or a search, one model call, a few spoken sentences."""

    def __init__(
        self,
        api: GmailApi,
        summariser: Summariser,
        *,
        timeout_s: float = ANSWER_TIMEOUT_S,
    ) -> None:
        self._api = api
        self._summariser = summariser
        self._timeout_s = timeout_s

    async def ask(
        self, question: str, *, query: str | None = None, day: Day | None = None
    ) -> dict:
        """Answer `question`: over one day's unanswered mail when `day` is given, else over
        what a Gmail search for `query` (or the question's own words) finds."""
        try:
            return await asyncio.wait_for(
                self._answer(question, query, day), self._timeout_s
            )
        except TimeoutError as exc:
            raise EmailError("timeout", f"over {self._timeout_s:.0f}s") from exc

    async def _answer(self, question: str, query: str | None, day: Day | None) -> dict:
        started = time.monotonic()
        asked = f"Their question: {question.strip()}\n\n" if question.strip() else ""
        if day is not None:
            collected, label = await collect(self._api, day, query)
            emails, system = collected.emails, DAY_PROMPT
            header = f"Email from {label}, {len(emails)} unanswered threads."
            if not emails:
                answer = (
                    f"Nothing in {day}'s email needs attention"
                    + (" that matches that" if query else "")
                    + (", and everything else is already answered." if collected.answered else ".")
                )
            counts = {"threads": collected.threads, "already_answered": collected.answered}
        else:
            terms = query or question
            emails, system = await search(self._api, terms), SEARCH_PROMPT
            header = f"The {len(emails)} newest threads a search for [{terms}] found."
            if not emails:
                answer = "No email matches that search."
            counts = {"threads": len(emails)}
            label = "search"
        fetched = time.monotonic() - started
        if emails:
            prompt = asked + header + "\n\n" + "\n\n---\n\n".join(e.render() for e in emails)
            answer = await self._summariser.summarise(system, prompt)
        log.info(
            "check_email (%s): %d threads, %d read in full; gmail %.1fs, total %.1fs",
            "day" if day else "search",
            len(emails),
            sum(1 for email in emails if email.body is not None),
            fetched,
            time.monotonic() - started,
        )
        return {"status": "ok", "scope": label, "answer": answer, **counts}


def email_problem(settings: Settings) -> str | None:
    """Why no reader can be built on this machine, in a sentence; None when one can."""
    if not token_path(settings).is_file():
        return "not signed in to Gmail: `jarvis auth login gmail`"
    from jarvis.agents.registry import BACKENDS, install_command, installed

    if not installed("claude") or BACKENDS["claude"].find_cli() is None:
        return f"the claude CLI is not installed: `{install_command('claude')}`"
    return None


def build_email_reader(settings: Settings, *, model: str, effort: str) -> EmailReader:
    """The production reader, answering with `model` at `effort` (the plugin's settings).

    It needs a Gmail sign-in and the `claude` CLI; ask `email_problem` first.
    """
    from jarvis.agents.registry import BACKENDS

    path = token_path(settings)
    cli = BACKENDS["claude"].find_cli()
    assert cli is not None, "email_problem says so first"
    secure_file(path)
    summariser = ClaudeCliSummariser(cli, settings, model=model, effort=effort)
    return EmailReader(HttpGmail(path), summariser)
