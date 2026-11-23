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
settings, no MCP servers, low effort (`keryx.integrations.summariser`, which `find_document`
shares) — with their question, and comes back as a few spoken sentences. On the owner's
inbox a busy day was 4.5–6 s end to end (measured 2026-09-27, Opus 5.5 at low effort;
Sonnet 5 was as fast).

Read-only by construction: `GmailApi` has only `get`, whatever the token would allow. The
credential is Keryx's Google token (`data_dir/google_token.json`, 0600, written by `keryx
auth login google`), which must let Gmail be read; an install that signed in before there
was one keeps answering from its older `gmail_token.json`, which is read and never written.
The credential is sent only to Google and never logged; failures come back as an
`EmailError` with a sentence to say. Mail content is never logged either — counts only.
"""

import asyncio
import base64
import html
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import getaddresses
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from keryx.config import Settings, secure_file
from keryx.integrations import google
from keryx.integrations.google_api import GMAIL, GoogleApiError, GoogleReader
from keryx.integrations.summariser import (
    Summariser,
    SummaryError,
    build_summariser,
    summariser_problem,
)

log = logging.getLogger("keryx.integrations.gmail")

#: The Gmail-only token `keryx auth login gmail` wrote before there was one Google sign-in.
#: Still read when there is no Google token; removed by the next sign-in.
LEGACY_TOKEN_FILE = "gmail_token.json"

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
#: The whole answer, fetch and model together. It is a wait inside a phone call.
ANSWER_TIMEOUT_S = 30.0

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
        "sentence; it needs `keryx auth login google` at the keyboard."
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


def legacy_token_path(settings: Settings) -> Path:
    return settings.data_dir / LEGACY_TOKEN_FILE


def credentials_path(settings: Settings) -> Path | None:
    """The token `check_email` reads: Keryx's Google token when it reads Gmail, else the
    Gmail-only one an older sign-in left, which stays until a sign-in that does."""
    token = google.read_token(settings)
    if token is not None and token.reads_gmail:
        return google.token_path(settings)
    legacy = legacy_token_path(settings)
    return legacy if legacy.is_file() else None


class HttpGmail(GmailApi):
    """Gmail over HTTPS with a refresh token; the access token is kept and reused.

    The token file is the one `keryx auth login google` writes, or the Gmail-only one an
    older sign-in left (the same keys a `google.oauth2.credentials.Credentials.to_json()`
    has, so any of them works). The request itself is `google_api.GoogleReader`, which
    every Google voice tool shares; this is what its failures are called here. A token
    whose scopes do not let Gmail be read, and a refresh Google refuses (`invalid_grant`:
    revoked, or a Testing-mode app past its seven days), are `signed_out`, which the voice
    turns into "run keryx auth login google".
    """

    def __init__(
        self,
        path: Path,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = GoogleReader(
            path, GMAIL, needs=(google.GMAIL_READONLY,), client=client, clock=clock
        )

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict:
        try:
            return await self._api.get(path, params)
        except GoogleApiError as exc:
            raise EmailError(_email_code(exc), exc.detail) from exc


def _email_code(exc: GoogleApiError) -> ErrorCode:
    """What `check_email` calls a Google failure: three things it can say apart."""
    if exc.code == "not_signed_in":
        return "not_configured"
    # A scope the saved sign-in lacks is known before anything is sent. A 403 from Gmail
    # itself stays "didn't answer", as it always was.
    if exc.code == "signed_out" or (exc.code == "not_connected" and exc.status is None):
        return "signed_out"
    return "gmail_failed"


def signed_in(settings: Settings) -> bool:
    """Whether there is a sign-in Gmail can be read with."""
    return credentials_path(settings) is not None


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
    `(from:ann OR ann) (kickoff OR grant)` — two clauses of two. A written `AND` is that same
    AND, so it is dropped; only capitals are operators, and `or` or `and` is a plain word.
    """
    clauses: list[list[str]] = []
    joining = False
    for term in _terms(query):
        if term == "AND":
            continue
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


def _loosens(term: str) -> bool:
    """Whether `term` on its own still says what the mail is about.

    A filter or a negation (`newer_than:7d`, `-in:spam`) alone matches almost everything, and
    so does a plain `or` or `and`, so none of them is ever a loosened query.
    """
    return not _FILTER_TERM.match(term) and term.lower() not in ("or", "and")


def plan_search(query: str) -> SearchPlan:
    """How to search Gmail for `query` so that no one broad term crowds out the rest."""
    query = " ".join(query.split())
    clauses = _clauses(query)
    if not clauses or re.search(r"\bAROUND\b", query):
        # AROUND binds the terms on either side of it, so moving any of them changes it.
        return SearchPlan([query], [])
    options = [_alternatives(clause) for clause in clauses]
    written = [" OR ".join(clause) for clause in clauses]
    widest = max(range(len(options)), key=lambda i: len(options[i]))
    exact = [query]
    if len(options[widest]) > 1:
        before, after = written[:widest], written[widest + 1 :]
        exact = [" ".join([*before, alt, *after]) for alt in _capped(options[widest])]
    narrows = [all(_FILTER_TERM.match(alt) for alt in opts) for opts in options]
    filters = [w for w, narrow in zip(written, narrows, strict=True) if narrow]
    about = [opts for opts, narrow in zip(options, narrows, strict=True) if not narrow]
    loose: list[str] = []
    if len(about) > 1:
        terms = list(dict.fromkeys(alt for opts in about for alt in opts if _loosens(alt)))
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
                answer = f"No email matched a search for {terms}."
            counts = {"threads": len(emails)}
            label = "search"
        fetched = time.monotonic() - started
        if emails:
            prompt = asked + header + "\n\n" + "\n\n---\n\n".join(e.render() for e in emails)
            try:
                answer = await self._summariser.summarise(system, prompt)
            except SummaryError as exc:
                raise EmailError("model_failed", exc.detail) from exc
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
    if credentials_path(settings) is None:
        if google.read_token(settings) is not None:
            return "Gmail is not part of your Google sign-in: `keryx auth login google`"
        return "not signed in to Google: `keryx auth login google`"
    return summariser_problem(settings)


def build_email_reader(settings: Settings, *, model: str, effort: str) -> EmailReader:
    """The production reader, answering with `model` at `effort` (the plugin's settings).

    It needs a Google sign-in that reads Gmail and the `claude` CLI; ask `email_problem`
    first.
    """
    path = credentials_path(settings)
    assert path is not None, "email_problem says so first"
    secure_file(path)
    summariser = build_summariser(settings, model=model, effort=effort)
    return EmailReader(HttpGmail(path), summariser)
