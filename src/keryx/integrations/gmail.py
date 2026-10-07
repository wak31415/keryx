"""Questions about the owner's email, answered out loud in well under ten seconds.

Behind the `check_email` plugin (`keryx.plugins.email`). Dispatching an email question to
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
operator, `newer_than:7d` included). Gmail lists newest first and ranks nothing else, so a
query of alternatives (`kickoff OR signature`) is run one alternative at a time and the
lists taken in turn: otherwise one broad word fills every slot with newer mail and the
thread the question is about never makes the cut (#80). Up to `MAX_SEARCH_CANDIDATES`
threads go to the model, the `MAX_SEARCH_THREADS` most promising (`worth_reading`) in
full, answered or not — whether they replied is part of the answer, so it is said rather
than filtered. A query of several terms ANDed that finds nothing is loosened to each of its
terms on its own, and the model is told that only part of it matched.

Either way the mail goes to a Claude model through the bundled `claude` CLI — no tools, no
settings, no MCP servers, low effort — with their question, and comes back as a few spoken
sentences. On the owner's inbox a busy day was 4.5–6 s end to end (measured 2026-09-27,
Opus 5.5 at low effort; Sonnet 5 was as fast).

Read-only by construction: the token is `gmail.readonly`, and `GmailApi` has only `get`.
The credential never leaves this module: it is read from `data_dir/gmail_token.json` (0600,
written by `keryx auth login gmail`), sent only to Google, and never logged; failures come back
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
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from keryx.agents.auth import child_env, redact, resolve_auth
from keryx.config import Settings, secure_file

log = logging.getLogger("keryx.integrations.gmail")

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
#: One search lists this many messages for each query it runs.
SEARCH_LISTED = 25
#: A search shows the model up to this many threads, and reads the most promising
#: `MAX_SEARCH_THREADS` of them in full; the rest ride along as a snippet.
MAX_SEARCH_CANDIDATES = 12
MAX_SEARCH_THREADS = 5
#: A query of alternatives runs as at most this many queries; the rest share the last.
MAX_ALTERNATIVES = 6
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
#: Terms that narrow a search without saying what it is about. Loosening keeps them on every
#: query; searched on their own, `newer_than:30d` would match everything recent.
_FILTER_TERM = re.compile(
    r"^-|^(newer_than|older_than|newer|older|after|before|is|in|label|category|has|"
    r"larger|smaller):",
    re.I,
)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)

Day = Literal["today", "yesterday"]
ErrorCode = Literal["not_configured", "signed_out", "gmail_failed", "model_failed", "timeout"]

MESSAGES: dict[ErrorCode, str] = {
    "not_configured": "Email isn't set up on this machine yet. Say that in one sentence.",
    "signed_out": (
        "You have been signed out of Gmail, so you can't read the email. Say that in one "
        "sentence; it needs `keryx auth login gmail` at the keyboard."
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
    "You answer a question about someone's email from the threads a search found, for "
    "them to hear read aloud on the phone. Answer in one to three short spoken sentences: "
    "say who and when where it matters, and whether they have already replied where the "
    "thread shows it. Some threads come with their full text, the rest only with a "
    "snippet: trust the full text where you have it, and use a snippet when it is what "
    "they asked about. If nothing found is about their question, say so plainly in one "
    "sentence, naming the words that were searched for so they can correct a misheard "
    "one, and do not go through the unrelated mail instead. If the search as a whole "
    "matched nothing, say that first. Never invent anything that is not in the mail. No "
    "markdown, no bullets, no preamble."
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

    The token file is the one `keryx auth login gmail` writes (the same keys a
    `google.oauth2.credentials.Credentials.to_json()` has, so either kind works). A refresh
    Google refuses (`invalid_grant`: revoked, or a Testing-mode app past its seven days) is
    `signed_out`, which the voice turns into "run keryx auth login gmail".
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


def _terms(query: str) -> list[str]:
    """`query` split into Gmail's terms, each whole: a word, a quoted phrase, a () or {} group."""
    terms: list[str] = []
    current, depth, quoted = "", 0, False
    for char in query:
        if char == '"':
            quoted = not quoted
        elif not quoted and char in "({":
            depth += 1
        elif not quoted and char in ")}":
            depth = max(depth - 1, 0)
        if char.isspace() and not quoted and depth == 0:
            if current:
                terms.append(current)
            current = ""
        else:
            current += char
    if current:
        terms.append(current)
    return terms


def _clauses(query: str) -> list[list[str]]:
    """`query` as Gmail reads it: clauses ANDed together, each one term or several ORed.

    OR binds tighter than the AND between words, so `from:ann OR ann kickoff OR grant` is
    `(from:ann OR ann) (kickoff OR grant)` — two clauses of two.
    """
    clauses: list[list[str]] = []
    joining = False
    for term in _terms(query):
        if term == "OR":
            joining = bool(clauses)
        elif joining:
            clauses[-1].append(term)
            joining = False
        else:
            clauses.append([term])
    return clauses


def _alternatives(clause: list[str]) -> list[str]:
    """What a clause matches either of: its ORed terms, or the inside of `{a b}` or `(a OR b)`."""
    if len(clause) > 1:
        return clause
    term = clause[0]
    if term.startswith("{") and term.endswith("}"):
        return _terms(term[1:-1]) or clause
    if term.startswith("(") and term.endswith(")"):
        inner = _clauses(term[1:-1])
        if len(inner) == 1:
            return _alternatives(inner[0])
    return clause


def _capped(alternatives: list[str]) -> list[str]:
    """At most `MAX_ALTERNATIVES` queries' worth: the last one ORs whatever is left over."""
    head = alternatives[: MAX_ALTERNATIVES - 1]
    rest = alternatives[MAX_ALTERNATIVES - 1 :]
    return head + ([" OR ".join(rest)] if rest else [])


@dataclass(frozen=True)
class SearchPlan:
    """The Gmail queries one search runs.

    `exact` together match what the query matches, its widest set of alternatives split one
    to a query; `loose` each match part of it — every term on its own, with the filters
    (`newer_than:`, `is:`, a `-`) kept on each — and run only when `exact` found nothing.
    """

    exact: list[str]
    loose: list[str]


def plan_search(query: str) -> SearchPlan:
    """How to search Gmail for `query` so that no one broad term crowds out the rest."""
    query = " ".join(query.split())
    clauses = _clauses(query)
    if not clauses:
        return SearchPlan([query], [])
    options = [_alternatives(clause) for clause in clauses]
    written = [" OR ".join(clause) for clause in clauses]
    widest = max(range(len(options)), key=lambda i: len(options[i]))
    exact = [query]
    if len(options[widest]) > 1:
        rest = written[:widest] + written[widest + 1 :]
        exact = [" ".join([*rest, alt]) for alt in _capped(options[widest])]
    narrows = [len(clause) == 1 and bool(_FILTER_TERM.match(clause[0])) for clause in clauses]
    filters = [w for w, narrow in zip(written, narrows, strict=True) if narrow]
    about = [opts for opts, narrow in zip(options, narrows, strict=True) if not narrow]
    loose: list[str] = []
    if len(about) > 1:
        terms = list(dict.fromkeys(alt for opts in about for alt in opts))
        loose = [" ".join([*filters, term]) for term in _capped(terms)]
    return SearchPlan(exact, loose)


@dataclass
class Found:
    """What a search came to: the threads, and whether only part of the query matched."""

    emails: list[Email]
    loosened: bool = False


async def search(api: GmailApi, query: str, *, loosen: bool = True) -> Found:
    """Up to `MAX_SEARCH_CANDIDATES` threads matching `query`, the most promising in full.

    Each query in the plan lists its newest matches, and the lists are taken in turn, so
    every alternative gets its newest threads in before any gets a second round. With
    `loosen`, a query that matched nothing is searched again term by term (`SearchPlan`).
    """
    gate = asyncio.Semaphore(CONCURRENCY)

    async def fetch(path: str, params: Mapping[str, Any] | None = None) -> dict:
        async with gate:
            return await api.get(path, params)

    async def threads_for(queries: list[str]) -> list[str]:
        listed = await asyncio.gather(
            *(fetch("messages", {"q": q, "maxResults": SEARCH_LISTED}) for q in queries)
        )
        lists = [[m["threadId"] for m in found.get("messages", [])] for found in listed]
        return list(dict.fromkeys(tid for row in zip_longest(*lists) for tid in row if tid))

    plan = plan_search(query)
    thread_ids, profile = await asyncio.gather(threads_for(plan.exact), fetch("profile"))
    loosened = False
    if not thread_ids and loosen and plan.loose:
        thread_ids = await threads_for(plan.loose)
        loosened = bool(thread_ids)
    me = str(profile.get("emailAddress", "")).lower()
    wanted = ["From", "To", "Subject", "Date", "List-Unsubscribe"]
    threads = await asyncio.gather(
        *(
            fetch(f"threads/{tid}", {"format": "metadata", "metadataHeaders": wanted})
            for tid in thread_ids[:MAX_SEARCH_CANDIDATES]
        )
    )
    emails = [email for email in map(fold_search, threads) if email is not None]
    # Ties keep the order the lists were taken in, which is newest first within each.
    order = sorted(range(len(emails)), key=lambda i: -worth_reading(emails[i], me))
    chosen = [emails[i] for i in order[:MAX_SEARCH_THREADS]]
    full = await asyncio.gather(
        *(fetch(f"messages/{email.message_id}", {"format": "full"}) for email in chosen)
    )
    for email, message in zip(chosen, full, strict=True):
        email.body = body_text(message.get("payload", {}))
    return Found(emails, loosened)


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
        from keryx.agents.claude import CLAUDE_AUTH

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
            terms = " ".join((query or question).split())
            # The question's own words are prose, not search terms: loosened one word at a
            # time, they would bring back every email with "the" in it.
            found = await search(self._api, terms, loosen=query is not None)
            emails, system = found.emails, SEARCH_PROMPT
            full = sum(1 for email in emails if email.body is not None)
            header = (
                f"Nothing matched a search for [{terms}] as a whole, so each of its terms was "
                f"searched on its own; these {len(emails)} threads matched some of them."
                if found.loosened
                else f"A search for [{terms}] found these {len(emails)} threads."
            ) + f" The {full} most promising come with their full text."
            if not emails:
                answer = (
                    f"No email matches a search for [{terms}]. Say what was searched for, so "
                    "they can correct a misheard word, rather than that the email does not exist."
                )
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
        return "not signed in to Gmail: `keryx auth login gmail`"
    from keryx.agents.registry import BACKENDS, install_command, installed

    if not installed("claude") or BACKENDS["claude"].find_cli() is None:
        return f"the claude CLI is not installed: `{install_command('claude')}`"
    return None


def build_email_reader(settings: Settings, *, model: str, effort: str) -> EmailReader:
    """The production reader, answering with `model` at `effort` (the plugin's settings).

    It needs a Gmail sign-in and the `claude` CLI; ask `email_problem` first.
    """
    from keryx.agents.registry import BACKENDS

    path = token_path(settings)
    cli = BACKENDS["claude"].find_cli()
    assert cli is not None, "email_problem says so first"
    secure_file(path)
    summariser = ClaudeCliSummariser(cli, settings, model=model, effort=effort)
    return EmailReader(HttpGmail(path), summariser)
