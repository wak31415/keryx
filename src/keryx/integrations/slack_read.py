"""Reading Slack as the owner, when they ask about it: bounded, and reading only.

Behind the `check_slack` plugin (`plugins/slack_check.py`) and the `keryx slack` commands
a subagent runs. It works with the user token of a workspace at the `read` level, which
never leaves this process: not into a subagent's environment, not onto a command line,
not into a log or an error.

**Read-only by construction.** `HttpSlackApi.get` is the one request there is: a `GET`,
with no body and no method to choose, to a Web API method that has to be in `READ_METHODS`
or is refused before anything is sent. So nothing built on this can post, react, join or
mark anything read, whatever the token allows.

**Bounded.** One tool call or one command gets one `SlackReader`, and with it a budget of
requests (`MAX_REQUESTS`) that no question may spend past; every list is cut to a cap, no
pagination is followed past `MAX_PAGES`, every request has a timeout, and a `429` is the
status `rate_limited`, said at once, never a sleep on a live call. What was left unread is
`more`, not a longer wait.

**What comes back is context** (`slack_context.Context`): messages attributed to who
wrote them, when and where, for the caller to fence. Nothing here decides what to do
with them, and nothing here is asked to read unless the owner asked about Slack.

**Search is one function**, `SlackReader.search`, over the legacy `search.messages`. The
Real-time Search API's terms forbid storing results and Keryx keeps transcripts and
reports, so it is not used; if that changes, this is the function that does.

**`unanswered` is a best effort**, because Slack has no such question to ask. Within the
window it is (a) the direct and group conversations whose latest message is from somebody
else (not the owner, not a bot), found from a search for what was sent to them and
confirmed against each conversation's own latest message; and (b) mentions of the owner in
a channel or thread where the owner has not written since. `UNANSWERED_QUERY` and
`MENTION_QUERY` are the two searches it rests on.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from keryx.integrations.slack import is_ts, plain_text, ts_key
from keryx.integrations.slack_context import Context, ContextMessage, clip, say_when
from keryx.logging_util import quiet_http_loggers

log = logging.getLogger("keryx.slack.read")

API_URL = "https://slack.com/api/"
#: Every Web API method this module may call. Each one only reads; nothing else is sent.
READ_METHODS = frozenset({
    "users.conversations",
    "conversations.history",
    "conversations.replies",
    "conversations.info",
    "search.messages",
    "users.info",
})
REQUEST_TIMEOUT_S = 8.0
#: The most requests one tool call or one command may make, whatever it was asked.
MAX_REQUESTS = 30
#: How many at once: a few seconds on a call, without a burst Slack would throttle.
CONCURRENCY = 4
#: The most pages of any one list that are followed.
MAX_PAGES = 2
#: The most messages one answer lists, and the most a history or a thread reads.
MAX_ITEMS = 8
MAX_MESSAGES = 60
#: The most candidates looked at behind a list, each costing a request to confirm.
MAX_CANDIDATES = 6
#: How far back a question may look, in days, and how far when it does not say.
MAX_DAYS, DEFAULT_DAYS = 30, 7

#: What was sent to the owner directly, newest first: the index `unanswered` starts from.
UNANSWERED_QUERY = "to:me"
#: A mention of the member `owner`, as Slack writes one in a message.
MENTION_QUERY = "<@{owner}>"

_CONVERSATION = re.compile(r"[CDG][A-Z0-9]{2,31}")
_MEMBER = re.compile(r"[UW][A-Z0-9]{2,31}")
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
#: A member as Slack writes one inside a message: `<@U…>` or `<@U…|name>`.
_MENTION = re.compile(r"<@([UW][A-Z0-9]{2,31})(?:\|[^<>]{0,200})?>")

#: What each failure is to say, by its code. Anything else is said by its code alone.
SENTENCES = {
    "rate_limited": "Slack is limiting how often it can be asked just now. Say so in one "
                    "sentence; trying again in a minute should work.",
    "unreachable": "Slack could not be reached. Say so in one sentence.",
    "signed_out": "Slack no longer takes the token it was given. Say that reading Slack "
                  "needs `keryx auth login slack --access read` at their keyboard again.",
    "missing_scope": "The Slack app lacks a permission this needs. Say that it has to be "
                     "reinstalled at the read level, with `keryx auth login slack --access "
                     "read` at their keyboard.",
    "not_found": "Slack does not know that conversation or message. Say so in one sentence.",
    "over_budget": "That needed more of Slack than one answer may read. Say what was "
                   "found so far, and that the rest is a task to hand over.",
}
_SIGNED_OUT = frozenset({"invalid_auth", "not_authed", "token_revoked", "token_expired",
                         "account_inactive"})
_NOT_FOUND = frozenset({"channel_not_found", "thread_not_found", "message_not_found",
                        "user_not_found", "not_in_channel"})


class SlackReadError(Exception):
    """Slack could not be read: a status, and a sentence written to be spoken.

    Never a response body and never the token: `code` is one of `SENTENCES`, or Slack's
    own error code when it is one.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    @property
    def sentence(self) -> str:
        return SENTENCES.get(self.code) or (
            f"Slack refused that ({self.code}). Say so in one sentence."
        )

    def as_dict(self) -> dict[str, str]:
        return {"status": self.code, "message": self.sentence}


class Unclear(ValueError):
    """What was asked cannot be asked of Slack: said back as it is, nothing sent."""


class SlackApi(Protocol):
    """The Slack Web API, read-only: one method, which is a `GET` (a fake in the tests)."""

    async def get(self, method: str, params: Mapping[str, str]) -> dict: ...

    async def aclose(self) -> None: ...


class HttpSlackApi:
    """`SlackApi` over HTTPS with a user token. It has `get` and nothing else."""

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # At INFO the HTTP client logs each request's whole address, which here is the
        # words searched for and the conversation asked about.
        quiet_http_loggers()
        self._client = httpx.AsyncClient(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_S,
            transport=transport,
        )

    async def get(self, method: str, params: Mapping[str, str]) -> dict:
        """One `GET` of a method in `READ_METHODS`; its answer, or a `SlackReadError`."""
        if method not in READ_METHODS:
            raise SlackReadError("not_a_read")
        try:
            response = await self._client.get(method, params=dict(params))
        except httpx.HTTPError:
            raise SlackReadError("unreachable") from None
        if response.status_code == 429:
            raise SlackReadError("rate_limited")
        try:
            answer = response.json()
        except ValueError:
            raise SlackReadError("unreachable") from None
        if not isinstance(answer, dict):
            raise SlackReadError("unreachable")
        if answer.get("ok"):
            return answer
        raise SlackReadError(classify(str(answer.get("error") or "")))

    async def aclose(self) -> None:
        await self._client.aclose()


def classify(error: str) -> str:
    """Slack's error as the status a caller is handed: one of ours, or its own code."""
    if error in ("ratelimited", "rate_limited"):
        return "rate_limited"
    if error in _SIGNED_OUT:
        return "signed_out"
    if error in _NOT_FOUND:
        return "not_found"
    if error in ("missing_scope", "not_allowed_token_type"):
        return "missing_scope"
    return error if _ERROR_CODE.fullmatch(error) else "unreachable"


@dataclass(frozen=True)
class Read:
    """One message as Slack returned it, before it is attributed."""

    channel: str
    ts: str
    user: str
    text: str
    thread_ts: str = ""
    #: What the conversation is called, when the answer said (a channel's name).
    channel_name: str = ""
    #: `im`, `mpim` or `channel`, when the answer said.
    kind: str = ""
    link: str = ""
    #: A bot's or an app's, or anything that is not an ordinary message.
    automated: bool = False


def _read(item: Any, *, channel: str = "", channel_name: str = "", kind: str = "") -> Read | None:
    """One message object as a `Read`; None for anything that is not one."""
    if not isinstance(item, dict) or not is_ts(item.get("ts")):
        return None
    where = item.get("channel")
    if isinstance(where, dict):  # a search match carries its conversation
        channel = str(where.get("id") or "")
        channel_name = str(where.get("name") or "")
        kind = "im" if where.get("is_im") else "mpim" if where.get("is_mpim") else "channel"
    user = item.get("user")
    thread = item.get("thread_ts")
    link = item.get("permalink")
    return Read(
        channel=channel,
        ts=item["ts"],
        user=user if isinstance(user, str) else "",
        text=str(item.get("text") or ""),
        thread_ts=thread if is_ts(thread) else "",
        channel_name=channel_name,
        kind=kind,
        link=link if isinstance(link, str) and link.startswith("https://") else "",
        automated=bool(item.get("bot_id") or item.get("subtype")),
    )


def conversation_id(value: str) -> str:
    """`value` as a conversation's id, or `Unclear`: nothing else is put in a request."""
    value = value.strip()
    if not _CONVERSATION.fullmatch(value):
        raise Unclear("a conversation is named by its id, like C0123ABCD or D0123ABCD")
    return value


def message_ts(value: str) -> str:
    """`value` as a message's timestamp, or `Unclear`."""
    value = value.strip()
    if not is_ts(value):
        raise Unclear("a message is named by its timestamp, like 1700000000.000100")
    return value


def window(days: int | None) -> int:
    """How many days back to look: what was asked, held between one and `MAX_DAYS`."""
    try:
        wanted = int(days) if days is not None else DEFAULT_DAYS
    except (TypeError, ValueError):
        wanted = DEFAULT_DAYS
    return max(1, min(wanted, MAX_DAYS))


class SlackReader:
    """One question's worth of reading: a budget, a cache of names, and the reads.

    Made for one tool call or one command and closed when it has answered (`aclose`), as
    the Google tools close theirs. `owner` is the member the token belongs to.
    """

    def __init__(
        self,
        api: SlackApi,
        owner: str,
        *,
        budget: int = MAX_REQUESTS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api = api
        self.owner = owner
        self._left = budget
        self._clock = clock
        self._names: dict[str, str] = {}
        self._gate = asyncio.Semaphore(CONCURRENCY)
        #: Something was left unread because a bound was reached.
        self.more = False

    async def aclose(self) -> None:
        await self._api.aclose()

    async def _get(self, method: str, **params: Any) -> dict:
        """One request against the budget; past it, `over_budget` and nothing is sent."""
        if self._left <= 0:
            self.more = True
            raise SlackReadError("over_budget")
        self._left -= 1
        async with self._gate:
            return await self._api.get(
                method, {key: str(value) for key, value in params.items() if value is not None}
            )

    def _oldest(self, days: int) -> float:
        return self._clock() - days * 86400

    # --- the reads -----------------------------------------------------------------------

    async def search(self, query: str, *, days: int, count: int = 40) -> list[Read]:
        """Messages matching `query` in the last `days`, newest first.

        The one function search goes through: `search.messages` today. The query is what
        the caller built or the owner said; the window is added here, as a date.
        """
        after = datetime.fromtimestamp(self._oldest(days)) - timedelta(days=1)
        answer = await self._get(
            "search.messages", query=f"{query} after:{after:%Y-%m-%d}", count=count,
            sort="timestamp", sort_dir="desc",
        )
        found = answer.get("messages")
        matches = found.get("matches") if isinstance(found, dict) else None
        if isinstance(found, dict) and isinstance(found.get("total"), int):
            self.more = self.more or found["total"] > count
        oldest = self._oldest(days)
        reads = [read for item in matches or [] if (read := _read(item)) is not None]
        return [read for read in reads if float(read.ts) >= oldest]

    async def history(self, channel: str, *, days: int | None = None, limit: int = MAX_MESSAGES,
                      oldest: str | None = None) -> list[Read]:
        """The latest messages of `channel`, newest first, at most `limit`."""
        since = oldest or (f"{self._oldest(days):.6f}" if days is not None else None)
        answer = await self._get("conversations.history", channel=channel, limit=limit,
                                 oldest=since)
        self.more = self.more or bool(answer.get("has_more"))
        items = answer.get("messages") if isinstance(answer.get("messages"), list) else []
        return [read for item in items if (read := _read(item, channel=channel)) is not None]

    async def replies(self, channel: str, ts: str, *, limit: int = MAX_MESSAGES,
                      oldest: str | None = None) -> list[Read]:
        """The thread hanging from `ts` in `channel`, oldest first, at most `limit`."""
        answer = await self._get("conversations.replies", channel=channel, ts=ts, limit=limit,
                                 oldest=oldest)
        self.more = self.more or bool(answer.get("has_more"))
        items = answer.get("messages") if isinstance(answer.get("messages"), list) else []
        return [read for item in items if (read := _read(item, channel=channel)) is not None]

    async def conversations(self, types: str = "im,mpim") -> list[dict]:
        """The owner's conversations of `types`, for at most `MAX_PAGES` pages."""
        found: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            answer = await self._get("users.conversations", types=types, limit=200,
                                     exclude_archived="true", cursor=cursor)
            channels = answer.get("channels")
            found += [each for each in channels or [] if isinstance(each, dict)]
            meta = answer.get("response_metadata")
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
            if not cursor:
                return found
        self.more = True
        return found

    async def name(self, member: str) -> str:
        """What to call `member`: their name, asked once a call, else their id."""
        if not _MEMBER.fullmatch(member or ""):
            return "somebody"
        if member == self.owner:
            return "you"
        if member not in self._names:
            try:
                answer = await self._get("users.info", user=member)
            except SlackReadError:
                self._names[member] = member  # a name is a nicety: its id will do
            else:
                user = answer.get("user") if isinstance(answer.get("user"), dict) else {}
                profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
                self._names[member] = clip(
                    user.get("real_name") or profile.get("display_name") or user.get("name")
                    or member, 80,
                )
        return self._names[member]

    # --- context: what a read becomes ----------------------------------------------------

    async def attributed(self, read: Read) -> ContextMessage:
        """`read` as context: who, when and where in words, and mentions by name."""
        who = await self.name(read.user) if read.user else "an app"
        if read.kind == "im":
            where = "a direct message"
        elif read.kind == "mpim":
            where = "a group message"
        elif read.channel_name:
            where = f"#{read.channel_name}"
        else:
            where = read.channel or "Slack"
        text = plain_text(read.text)
        for member in dict.fromkeys(_MENTION.findall(text)):
            named = await self.name(member)
            text = re.sub(rf"<@{member}(?:\|[^<>]{{0,200}})?>", f"@{named}", text)
        return ContextMessage(who=who, when=say_when(read.ts), where=where, text=text,
                              link=read.link)

    async def context(self, reads: list[Read], *, limit: int = MAX_ITEMS) -> Context:
        """`reads` as a `Context`: the first `limit`, attributed, and whether more were left."""
        self.more = self.more or len(reads) > limit
        messages = [await self.attributed(read) for read in reads[:limit]]
        return Context(messages=tuple(messages), more=self.more)


# --- the questions ---------------------------------------------------------------------------


def _theirs(read: Read, owner: str) -> bool:
    """Written by somebody else: not the owner, and not a bot or an app."""
    return bool(read.user) and read.user != owner and not read.automated


async def _all(calls: list[Awaitable[Any]]) -> list[Any]:
    """Every call's answer, a failure in its place: one conversation that cannot be read
    does not cost the rest."""
    return await asyncio.gather(*calls, return_exceptions=True)


async def unanswered(reader: SlackReader, *, days: int) -> Context:
    """What is waiting on the owner, as best it can be told (see the module's docstring)."""
    owner = reader.owner
    sent, mentioned = await _all([
        reader.search(UNANSWERED_QUERY, days=days),
        reader.search(MENTION_QUERY.format(owner=owner), days=days),
    ])
    if isinstance(sent, SlackReadError) and isinstance(mentioned, SlackReadError):
        raise sent
    waiting: list[Read] = []

    # (a) Direct and group conversations whose latest message is somebody else's.
    newest: dict[str, Read] = {}
    for read in sent if isinstance(sent, list) else []:
        if read.kind in ("im", "mpim") and _theirs(read, owner):
            newest.setdefault(read.channel, read)
    candidates = list(newest.values())[:MAX_CANDIDATES]
    reader.more = reader.more or len(newest) > MAX_CANDIDATES
    latest = await _all([reader.history(read.channel, limit=1) for read in candidates])
    for read, last in zip(candidates, latest, strict=True):
        if not isinstance(last, list):
            reader.more = True  # could not be confirmed, so it is not claimed
        elif last and _theirs(last[0], owner):
            waiting.append(Read(read.channel, last[0].ts, last[0].user, last[0].text,
                                kind=read.kind, link=read.link))

    # (b) Mentions in a channel or a thread where the owner has not written since.
    mentions = [read for read in mentioned if _theirs(read, owner)
                and read.kind == "channel"] if isinstance(mentioned, list) else []
    reader.more = reader.more or len(mentions) > MAX_CANDIDATES
    mentions = mentions[:MAX_CANDIDATES]
    after = await _all([_since(reader, read) for read in mentions])
    for read, later in zip(mentions, after, strict=True):
        if not isinstance(later, list):
            reader.more = True
        elif not any(each.user == owner and ts_key(each.ts) > ts_key(read.ts)
                     for each in later):
            waiting.append(read)

    waiting.sort(key=lambda read: ts_key(read.ts), reverse=True)
    return await reader.context(waiting)


async def _since(reader: SlackReader, read: Read) -> list[Read]:
    """What was written after `read` where the owner would have answered it: its thread,
    and for a message that is not in one, the conversation as well."""
    later = await reader.replies(read.channel, read.thread_ts or read.ts, oldest=read.ts)
    if not read.thread_ts:
        later += await reader.history(read.channel, oldest=read.ts, limit=30)
    return later


async def mentions(reader: SlackReader, *, days: int) -> Context:
    """Messages that mention the owner, newest first, answered or not."""
    found = await reader.search(MENTION_QUERY.format(owner=reader.owner), days=days)
    return await reader.context([read for read in found if _theirs(read, reader.owner)])


async def search(reader: SlackReader, text: str, *, days: int) -> Context:
    """Messages matching the owner's own words, newest first."""
    words = " ".join(text.split())
    if not words:
        raise Unclear("search needs the words to look for")
    return await reader.context(await reader.search(words, days=days))


async def history(reader: SlackReader, conversation: str, *, days: int,
                  limit: int = MAX_MESSAGES) -> Context:
    """The latest messages of one conversation, oldest first, for a subagent to read."""
    reads = await reader.history(conversation_id(conversation), days=days, limit=limit)
    return await reader.context(list(reversed(reads)), limit=limit)


async def thread(reader: SlackReader, conversation: str, ts: str, *,
                 limit: int = MAX_MESSAGES) -> Context:
    """One thread, oldest first, for a subagent to read."""
    reads = await reader.replies(conversation_id(conversation), message_ts(ts), limit=limit)
    return await reader.context(reads, limit=limit)
