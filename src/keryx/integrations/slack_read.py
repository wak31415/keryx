"""Reading Slack as the owner, when they ask about it: bounded, and reading only.

The client behind the `slack_read` plugin (`plugins/slack_read.py`) and `keryx slack read`,
both of which plan what to read in `integrations/slack_query.py` and nowhere else. It
works with the user token of a workspace at the `read` level, which never leaves this
process: not into a subagent's environment, not onto a command line, not into a log or an
error.

**Read-only by construction.** `HttpSlackApi.get` is the one request there is: a `GET`,
with no body and no method to choose, to a Web API method that has to be in `READ_METHODS`
or is refused before anything is sent. So nothing built on this can post, react, join or
mark anything read, whatever the token allows. Sending as the owner is another client
altogether (`integrations/slack_write.py`).

**Bounded.** One tool call or one command gets one `SlackReader`, and with it a budget of
requests (`MAX_REQUESTS`) that no question may spend past; every list is cut to a cap, no
pagination is followed past a few pages, every request has a timeout, and a `429` is the
status `rate_limited`, said at once, never a sleep on a live call. What was left unread is
`more`, and a part that failed is `incomplete`: neither is ever a longer wait.

**What comes back is context** (`slack_context.Context`): messages attributed to who
wrote them, when and where, for the caller to fence. Nothing here decides what to do
with them, and nothing here is asked to read unless the owner asked about Slack.

**Search is one function**, `SlackReader.search`, over the legacy `search.messages`. The
Real-time Search API's terms forbid storing results and Keryx keeps transcripts and
reports, so it is not used; if that changes, this is the function that does.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Protocol

import httpx

from keryx.integrations.slack import is_ts, plain_text
from keryx.integrations.slack_context import (
    MESSAGE_CHARS,
    NAME_CHARS,
    Context,
    ContextMessage,
    clip,
    say_when,
)
from keryx.logging_util import quiet_http_loggers

log = logging.getLogger("keryx.slack.read")

API_URL = "https://slack.com/api/"
#: Every Web API method this module may call. Each one only reads; nothing else is sent.
READ_METHODS = frozenset({
    "users.conversations",
    "conversations.history",
    "conversations.replies",
    "search.messages",
    "users.info",
    "users.list",
})
REQUEST_TIMEOUT_S = 8.0
#: The most requests one tool call or one command may make, whatever it was asked.
MAX_REQUESTS = 30
#: How many of those are kept for names, so that whatever the reads cost there is
#: still somebody to attribute a message to: a read may not spend into them.
NAME_REQUESTS = 6
#: How many at once: a few seconds on a call, without a burst Slack would throttle.
CONCURRENCY = 4
#: The most pages of any one list of messages or conversations that are followed, and of
#: the member list, which is the longest of them.
MAX_PAGES = 2
DIRECTORY_PAGES = 3
#: How many of a list one page asks for: Slack's most for these methods.
LIST_LIMIT = 999
#: The most messages one answer on a call lists, and the most a command may be asked for.
MAX_ITEMS = 8
MAX_MESSAGES = 60
#: The most of one conversation or thread read behind an answer, a page at a time.
PAGE_MESSAGES = 200
#: The most matches one search asks for.
SEARCH_COUNT = 40
#: The most candidates looked at behind a list, each costing a request to confirm.
MAX_CANDIDATES = 6
#: How far back a question may look, in days, and how far when it does not say.
MAX_DAYS, DEFAULT_DAYS = 30, 7
#: The most members named inside one message's text; the rest are "someone".
MAX_NAMED = 5
#: The most of a message's raw text that is looked at before it is clipped to a line.
RAW_CHARS = 4 * MESSAGE_CHARS

_MEMBER = re.compile(r"[UW][A-Z0-9]{2,31}")
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
#: A member as Slack writes one inside a message: `<@U…>` or `<@U…|name>`. One pattern,
#: and every use of it is a single pass with a callback: the text is somebody else's.
_MENTION = re.compile(r"<@([UW][A-Z0-9]{2,31})(?:\|[^<>]{0,200})?>")
#: The message subtypes that are Slack talking, not a person: somebody joined, a topic
#: was set, a bot posted. Anything else with a subtype (a file shared, a reply also sent
#: to the channel, a /me) is a person writing.
SYSTEM_SUBTYPES = frozenset({
    "bot_message", "bot_add", "bot_remove",
    "channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
    "channel_archive", "channel_unarchive", "channel_convert_to_private",
    "channel_convert_to_public", "channel_posting_permissions",
    "group_join", "group_leave", "group_topic", "group_purpose", "group_name",
    "group_archive", "group_unarchive",
    "pinned_item", "unpinned_item", "reminder_add", "joiner_notification",
    "joiner_notification_for_inviter", "sh_room_created", "huddle_thread",
    "ekm_access_denied",
})

#: What each failure is to say, by its code. Anything else is said by its code alone.
SENTENCES = {
    "rate_limited": "Slack is limiting how often it can be asked just now. Say so in one "
                    "sentence; trying again in a minute should work.",
    "unreachable": "Slack could not be reached. Say so in one sentence.",
    "signed_out": "Slack no longer takes the token it was given. Say that reading Slack "
                  "needs `keryx auth login slack --access read` at their keyboard again.",
    "missing_scope": "The Slack app lacks a permission this needs. Say that it has to be "
                     "reinstalled at the level they chose, with `keryx auth login slack` "
                     "at their keyboard.",
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


@dataclass(frozen=True)
class Candidate:
    """One thing a name could have meant: what Slack calls it, and a `ref` to say which."""

    label: str
    ref: str = ""

    def line(self) -> str:
        said = clip(self.label, NAME_CHARS)
        return f"- [{self.ref}] {said}" if self.ref else f"- {said}"


class Unclear(ValueError):
    """What was asked cannot be read as it stands: said back in one sentence, nothing
    guessed and nothing sent. `candidates` are what a name could have meant, when that is
    why; `status` is `unclear`, or `no_match` for a name nothing answers to."""

    def __init__(self, sentence: str, candidates: tuple[Candidate, ...] = (),
                 status: str = "unclear") -> None:
        self.candidates = candidates
        self.status = status
        super().__init__(sentence)


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
    #: A bot's or an app's, or Slack's own notice: not a person writing.
    automated: bool = False
    #: Who has replied in the thread under it, as far as Slack lists them, and whether
    #: there are repliers it did not list.
    reply_users: tuple[str, ...] = ()
    repliers_unlisted: bool = False

    @property
    def root(self) -> str:
        """The message its thread hangs from: itself, when it is not a reply."""
        return self.thread_ts or self.ts

    @property
    def ref(self) -> str:
        """What names its thread to a later call: the conversation and the thread's root."""
        return f"{self.channel}:{self.root}" if self.channel else ""


class Page(NamedTuple):
    """One read's messages, and whether Slack had more than these."""

    reads: list[Read]
    more: bool = False


@dataclass(frozen=True)
class Place:
    """One conversation the owner is in: its id, its kind, and what it is called."""

    id: str
    #: `im`, `mpim`, `channel` (public) or `private`.
    kind: str = "channel"
    name: str = ""
    #: For a direct message: the other member.
    user: str = ""


@dataclass(frozen=True)
class Person:
    """One member of the workspace: their id and the names they go by."""

    id: str
    #: Their full name, the name they show, and their handle: whichever there are.
    names: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.names[0] if self.names else self.id


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
    repliers = item.get("reply_users")
    repliers = tuple(each for each in repliers if isinstance(each, str)) if isinstance(
        repliers, list) else ()
    count = item.get("reply_users_count")
    return Read(
        channel=channel,
        ts=item["ts"],
        user=user if isinstance(user, str) else "",
        text=str(item.get("text") or ""),
        thread_ts=thread if is_ts(thread) else "",
        channel_name=channel_name,
        kind=kind,
        link=link if isinstance(link, str) and link.startswith("https://") else "",
        automated=bool(item.get("bot_id")) or item.get("subtype") in SYSTEM_SUBTYPES,
        reply_users=repliers,
        repliers_unlisted=isinstance(count, int) and count > len(repliers),
    )


def _place(item: Any) -> Place | None:
    """One conversation object as a `Place`; None for anything that is not one."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        return None
    if item.get("is_im"):
        user = item.get("user")
        return Place(item["id"], "im", user=user if isinstance(user, str) else "")
    name = item.get("name")
    kind = "mpim" if item.get("is_mpim") else "private" if item.get("is_private") else "channel"
    return Place(item["id"], kind, name=name if isinstance(name, str) else "")


def _person(item: Any) -> Person | None:
    """One member object as a `Person`; None for a bot, a deleted member or anything else."""
    if not isinstance(item, dict) or not _MEMBER.fullmatch(str(item.get("id") or "")):
        return None
    if item.get("deleted") or item.get("is_bot") or item["id"] == "USLACKBOT":
        return None
    profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
    names = (item.get("real_name") or profile.get("real_name"), profile.get("display_name"),
             item.get("name"))
    return Person(item["id"], tuple(dict.fromkeys(
        clip(name, NAME_CHARS) for name in names if isinstance(name, str) and name.strip()
    )))


def window(days: Any) -> int:
    """How many days back to look: what was asked, held between one and `MAX_DAYS`."""
    try:
        wanted = int(days) if days is not None else DEFAULT_DAYS
    except (TypeError, ValueError):
        wanted = DEFAULT_DAYS
    return max(1, min(wanted, MAX_DAYS))


class SlackReader:
    """One question's worth of reading: a budget, what it has learned, and the reads.

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
        #: What a read has to leave for names: none of a budget too small to share.
        self._kept = NAME_REQUESTS if budget >= MAX_REQUESTS else 0
        self._clock = clock
        self._names: dict[str, str] = {}
        self._all_names: dict[str, tuple[str, ...]] = {}
        #: A name lookup was refused for asking too much: no more are made this call.
        self._names_off = False
        self._places: list[Place] | None = None
        self._people: list[Person] | None = None
        self._gate = asyncio.Semaphore(CONCURRENCY)
        #: The owner's conversations, or the member list, ran past the pages that are
        #: read: a name found there may not be the only one of its kind.
        self.places_cut = False
        self.people_cut = False
        #: Something was left unread because a bound was reached.
        self.more = False
        #: Part of what was asked could not be read at all: the answer is not the whole.
        self.incomplete = False

    async def aclose(self) -> None:
        await self._api.aclose()

    async def _get(self, method: str, *, naming: bool = False, **params: Any) -> dict:
        """One request against the budget; past it, `over_budget` and nothing is sent.
        A read stops short of the requests kept for names; `naming` may spend those."""
        if self._left <= (0 if naming else self._kept):
            self.more = True
            raise SlackReadError("over_budget")
        self._left -= 1
        async with self._gate:
            return await self._api.get(
                method, {key: str(value) for key, value in params.items() if value is not None}
            )

    def oldest(self, days: int) -> float:
        """The start of a window of `days`, as a time."""
        return self._clock() - days * 86400

    # --- the reads -----------------------------------------------------------------------

    async def search(self, query: str, *, days: int, before: str = "",
                     count: int = SEARCH_COUNT) -> Page:
        """Messages matching `query` in the last `days` (and before `before`), newest first.

        The one function search goes through: `search.messages` today. The query is what
        the planner built from the owner's words; the window is added here, as dates, and
        held again on what comes back, because a date is a day and the window is a moment.
        """
        after = datetime.fromtimestamp(self.oldest(days)) - timedelta(days=1)
        dated = f"{query} after:{after:%Y-%m-%d}"
        if before:
            until = datetime.fromtimestamp(float(before)) + timedelta(days=1)
            dated += f" before:{until:%Y-%m-%d}"
        answer = await self._get("search.messages", query=dated, count=count,
                                 sort="timestamp", sort_dir="desc")
        found = answer.get("messages")
        matches = found.get("matches") if isinstance(found, dict) else None
        total = found.get("total") if isinstance(found, dict) else None
        oldest = self.oldest(days)
        reads = [read for item in matches or [] if (read := _read(item)) is not None]
        kept = [read for read in reads if float(read.ts) >= oldest
                and (not before or float(read.ts) < float(before))]
        return Page(kept, isinstance(total, int) and total > count)

    async def history(self, channel: str, *, limit: int, oldest: str | None = None,
                      latest: str | None = None) -> Page:
        """The latest messages of `channel` between `oldest` and `latest`, newest first."""
        answer = await self._get("conversations.history", channel=channel, limit=limit,
                                 oldest=oldest, latest=latest)
        items = answer.get("messages") if isinstance(answer.get("messages"), list) else []
        return Page([read for item in items if (read := _read(item, channel=channel)) is not None],
                    bool(answer.get("has_more")))

    async def replies(self, channel: str, ts: str, *, limit: int = PAGE_MESSAGES,
                      oldest: str | None = None, latest: str | None = None,
                      pages: int = MAX_PAGES) -> Page:
        """The thread hanging from `ts` in `channel`, oldest first, for at most `pages`
        pages of `limit`."""
        reads: list[Read] = []
        cursor = None
        for _ in range(pages):
            answer = await self._get("conversations.replies", channel=channel, ts=ts,
                                     limit=limit, oldest=oldest, latest=latest, cursor=cursor)
            items = answer.get("messages") if isinstance(answer.get("messages"), list) else []
            reads += [read for item in items if (read := _read(item, channel=channel)) is not None]
            meta = answer.get("response_metadata")
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
            if not (answer.get("has_more") and cursor):
                return Page(reads, bool(answer.get("has_more")))
        return Page(reads, True)

    async def _listed(self, method: str, key: str, pages: int, **params: Any) -> tuple[list, bool]:
        """Every item of one cursored list, for at most `pages` pages; and whether it
        was cut short."""
        found: list = []
        cursor = None
        for _ in range(pages):
            answer = await self._get(method, limit=LIST_LIMIT, cursor=cursor, **params)
            items = answer.get(key)
            found += items if isinstance(items, list) else []
            meta = answer.get("response_metadata")
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
            if not cursor:
                return found, False
        return found, True

    async def places(self) -> list[Place]:
        """Every conversation the owner is in, asked once a call: channels, private
        channels, group messages and direct messages."""
        if self._places is None:
            items, cut = await self._listed(
                "users.conversations", "channels", MAX_PAGES,
                types="public_channel,private_channel,mpim,im", exclude_archived="true",
            )
            self.more, self.places_cut = self.more or cut, cut
            self._places = [place for item in items if (place := _place(item)) is not None]
        return self._places

    async def place(self, channel: str) -> Place:
        """The conversation with id `channel`, by its name when the owner's list has it."""
        try:
            found = next((each for each in await self.places() if each.id == channel), None)
        except SlackReadError:
            found = None  # what it is called is a nicety: its id still reads it
        return found or Place(channel, "im" if channel.startswith("D") else "channel")

    async def people(self) -> list[Person]:
        """The workspace's members, asked once a call, for at most `DIRECTORY_PAGES`."""
        if self._people is None:
            items, cut = await self._listed("users.list", "members", DIRECTORY_PAGES)
            self.more, self.people_cut = self.more or cut, cut
            self._people = [person for item in items if (person := _person(item)) is not None]
            for person in self._people:
                self._names.setdefault(person.id, _nameable(person.label, person.id))
        return self._people

    async def names_of(self, member: str) -> tuple[str, ...]:
        """Everything `member` goes by, an app's bot user included (which the member list
        leaves out): asked of Slack once a call, and () when it will not say."""
        if not _MEMBER.fullmatch(member or ""):
            return ()
        if member not in self._all_names:
            try:
                answer = await self._get("users.info", naming=True, user=member)
            except SlackReadError:
                return ()
            user = answer.get("user") if isinstance(answer.get("user"), dict) else {}
            profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
            names = (user.get("real_name"), profile.get("real_name"),
                     profile.get("display_name"), user.get("name"))
            self._all_names[member] = tuple(dict.fromkeys(
                clip(name, NAME_CHARS) for name in names
                if isinstance(name, str) and name.strip()))
        return self._all_names[member]

    async def name(self, member: str) -> str:
        """What to call `member`: `you` for the owner and nobody else, their name asked
        once a call, else their id. One refusal for asking too much ends the asking."""
        if not _MEMBER.fullmatch(member or ""):
            return "somebody"
        if member == self.owner:
            return "you"
        if member not in self._names:
            if self._names_off:
                return member
            try:
                answer = await self._get("users.info", naming=True, user=member)
            except SlackReadError as error:
                # A name is a nicety: its id will do. And once Slack says it is being
                # asked too much, or the budget is spent, no more names are asked for.
                self._names_off = error.code in ("rate_limited", "over_budget")
                self._names[member] = member
            else:
                person = _person(answer.get("user"))
                self._names[member] = _nameable(person.label if person else member, member)
        return self._names[member]

    # --- context: what a read becomes ----------------------------------------------------

    async def attributed(self, read: Read) -> ContextMessage:
        """`read` as context: who, when and where in words, and mentions by name.

        The text is cut to a line before anything is looked for in it, and the members it
        mentions are named in one pass, the first few of them: what a message costs to
        handle never depends on what its author put in it.
        """
        who = await self.name(read.user) if read.user else "an app"
        if read.kind == "im":
            where = "a direct message"
        elif read.kind == "mpim":
            where = "a group message"
        elif read.channel_name:
            where = f"#{read.channel_name}"
        else:
            where = read.channel or "Slack"
        text = clip(plain_text(read.text[:RAW_CHARS]))
        members = list(dict.fromkeys(_MENTION.findall(text)))[:MAX_NAMED]
        named = {member: await self.name(member) for member in members}
        text = _MENTION.sub(lambda found: "@" + named.get(found.group(1), "someone"), text)
        return ContextMessage(who=who, when=say_when(read.ts), where=where, text=text,
                              link=read.link, ref=read.ref)

    async def context(self, reads: list[Read], *, limit: int = MAX_ITEMS,
                      before: str = "") -> Context:
        """`reads` as a `Context`: the first `limit`, attributed, with what was left out
        and what could not be read said beside them."""
        self.more = self.more or len(reads) > limit
        messages = [await self.attributed(read) for read in reads[:limit]]
        return Context(messages=tuple(messages), more=self.more, incomplete=self.incomplete,
                       before=before if self.more else "")


_NOT_A_LETTER = re.compile(r"[\W_]+")


def _nameable(name: str, member: str) -> str:
    """`name` as it may be put beside a message: never one that starts with the word
    kept for the owner (`you,` or `You (the owner)` as much as `you`), which is quoted
    as somebody's name instead."""
    name = clip(name, NAME_CHARS) or member
    first = _NOT_A_LETTER.sub(" ", name.casefold()).split()[:1]
    return f"a member named \"{name}\"" if first == ["you"] else name
