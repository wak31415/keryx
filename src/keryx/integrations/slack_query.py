"""What to read from Slack: the filters one read takes, and the one plan made from them.

The `slack_read` voice tool and `keryx slack read` take the same filters, and both come
here: `parse` is the one place they are validated, and `answer` the one place a read is
planned. Every filter is optional and they combine; there are no modes anywhere else.

| Filters given | What is read |
|---|---|
| none | what is waiting on the owner (`only=waiting`) |
| `only=waiting` | two searches, then each candidate confirmed against its conversation |
| `only=mentions` | one search for the owner's mention |
| `thread` | that thread, in order |
| `conversation` | that conversation's latest messages, in order |
| `text`, or `from` with no place | one search |

What each may cost, at most, of the reader's budget (`MAX_REQUESTS`, of which
`NAME_REQUESTS` are kept for names): resolving names, 5 (the owner's conversations for
two pages, the member list for three, each asked once a call); what is waiting, 20 (two
searches, then up to `MAX_CANDIDATES` of each half confirmed, one request for a direct
conversation and two for a mention); a thread, 2; a conversation, mentions or a search, 1.

`conversation`, `from` and `days` narrow any of them; `text` narrows a thread or what is
waiting by the words themselves. A search is narrowed twice: by the modifiers Slack
documents (`in:`, `from:<@…>`), built only from ids and names Slack gave, and again on
what comes back, which is the half that is trusted. The window is held here too.

**Nothing is guessed.** A name is matched against the owner's own conversations and the
member list. One match is used; several are `Unclear` with the candidates, each with a
`ref` that says which; none is `Unclear` with the status `no_match`. Filters that
contradict each other are `Unclear` as well, in one sentence.

**No string from the caller reaches a method name, a path or a shell.** A conversation is
an id matched against a pattern (`slack_context.REF`) or one looked up in a list Slack
returned; a message is a timestamp; the words searched for are one query parameter.

**`waiting` is a best effort**, because Slack has no such question to ask. Within the
window it is (a) the direct and group conversations whose latest message is somebody
else's (not the owner's, not a bot's), which the owner has not answered in its thread
either; and (b) mentions of the owner in a channel or thread where the owner has not
written since. What cannot be confirmed is not claimed: a conversation that could not be
read, or one with more after the mention than was read, is left out and said to be.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from keryx.integrations.slack import ts_key
from keryx.integrations.slack_context import NAME_CHARS, REF, Context, clip
from keryx.integrations.slack_read import (
    DEFAULT_DAYS,
    MAX_CANDIDATES,
    MAX_ITEMS,
    MAX_MESSAGES,
    PAGE_MESSAGES,
    RAW_CHARS,
    Candidate,
    Page,
    Person,
    Place,
    Read,
    SlackReader,
    SlackReadError,
    Unclear,
    window,
)

log = logging.getLogger("keryx.slack.query")

WAITING, MENTIONS = "waiting", "mentions"
ONLY = (WAITING, MENTIONS)
#: What a set of filters comes to: the five reads there are.
THREAD, PLACE, SEARCH = "thread", "conversation", "search"
#: The most of the owner's words that are searched for, and of a name that is looked up.
TEXT_CHARS = 200
#: How many of a conversation's latest messages are looked at for its last word: a few,
#: so that one of Slack's own notices after it is stepped over.
LATEST = 5
#: How much of a conversation after a mention is read for the owner's answer.
SINCE = 50

#: What was sent to the owner directly, newest first: the index `waiting` starts from.
TO_ME_QUERY = "to:me"
#: A mention of the member `owner`, as Slack writes one in a message.
MENTION_QUERY = "<@{owner}>"

_MEMBER = re.compile(r"[UW][A-Z0-9]{2,31}")
#: A conversation's name as it may be put after `in:`: one bare word.
_BARE = re.compile(r"[\w.-]{1,80}")
_NOT_WORDS = re.compile(r"[\W_]+")
#: Words in a name as it is said that are not part of the name.
_FILLER = frozenset({"the", "channel", "conversation"})
_ME = frozenset({"me", "myself", "i", "mine"})

SEVERAL = (
    "More than one thing on their Slack could be {said}. Ask which they mean, naming the "
    "candidates in a few words, and call again with that one's ref or full name."
)
NO_SUCH = (
    "Nothing on their Slack is called {said}, among what could be read. Say so in a few "
    "words, saying the name as you heard it, so they can correct a misheard one."
)
NO_DIRECT = (
    "They have no direct conversation with {said} to read. Say so in one sentence."
)


@dataclass(frozen=True)
class Filters:
    """One read's filters, validated. Every one is optional, and they combine."""

    #: One place: a channel's name, a person's name, or a `ref`. Empty: everywhere.
    conversation: str = ""
    #: One thread, by the `ref` of a message in it.
    thread: str = ""
    #: Words to look for.
    text: str = ""
    #: A person's name: only what they wrote.
    sender: str = ""
    #: `waiting`, `mentions`, or neither.
    only: str = ""
    days: int = DEFAULT_DAYS
    #: The most messages to hand back.
    limit: int = MAX_ITEMS
    #: Only what is older than this timestamp: how a command reads further back.
    before: str = ""

    @property
    def kind(self) -> str:
        """Which of the five reads these filters come to."""
        if self.thread:
            return THREAD
        if self.only:
            return self.only
        if self.text or not self.conversation:
            return SEARCH
        return PLACE


def parse(raw: Mapping[str, Any], *, limit: int = MAX_ITEMS) -> Filters:
    """`raw` (a tool call's arguments, or a command line's options) as `Filters`.

    The one place they are validated. Raises `Unclear`, in a sentence, for a filter that
    is not one and for filters that contradict each other.
    """
    def said(key: str) -> str:
        value = raw.get(key)
        return " ".join(str(value).split()) if value not in (None, "") else ""

    only = said("only").lower()
    if only and only not in ONLY:
        raise Unclear(f"only is one of {', '.join(ONLY)}, or left out")
    thread = said("thread")
    threaded = REF.fullmatch(thread)
    if thread and not (threaded and threaded.group(2)):
        raise Unclear("a thread is named by the ref of a message in it, as an earlier "
                      "result gave it")
    conversation, sender = said("conversation"), said("from")
    if max(len(conversation), len(sender)) > NAME_CHARS:
        raise Unclear("a conversation or a person is named in a few words")
    before = said("before")
    if before:
        found = REF.fullmatch(before)
        before = found.group(2) if found and found.group(2) else before
        if not re.fullmatch(r"\d{1,12}\.\d{1,9}", before):
            raise Unclear("before is a message's timestamp, or the ref of one")
    text = clip(said("text"), TEXT_CHARS)
    if thread and only:
        raise Unclear("a thread is read whole: `only` looks across conversations, so name "
                      "one or the other")
    if threaded and (place := REF.fullmatch(conversation)) and place.group(1) != (
            threaded.group(1)):
        raise Unclear("that thread is not in that conversation: name one or the other")
    if not (conversation or thread or text or sender or only):
        only = WAITING
    if before and only == WAITING:
        raise Unclear("before reads further back in a conversation, a thread or a search: "
                      "what is waiting is always the latest")
    try:
        most = int(raw["limit"]) if raw.get("limit") not in (None, "") else limit
    except (TypeError, ValueError):
        raise Unclear("limit is a number") from None
    return Filters(
        conversation=conversation, thread=thread, text=text, sender=sender, only=only,
        days=window(raw.get("days")), limit=max(1, min(most, MAX_MESSAGES)), before=before,
    )


# --- names: what a conversation or a person was called -----------------------------------


def words(name: str) -> list[str]:
    return _NOT_WORDS.sub(" ", name.casefold()).split()


def _term(said: str) -> tuple[list[str], str]:
    """The words of a name as it was said, and which kind it asked for by its first
    character: `#` a channel, `@` a person, else either."""
    kind = {"#": "channel", "@": "person"}.get(said[:1], "")
    spoken = words(said)
    trimmed = [word for word in spoken if word not in _FILLER]
    return (trimmed or spoken), kind


def _matches(term: list[str], names: tuple[str, ...], *, exact: bool,
             person: bool = False) -> bool:
    """Whether `term` names what goes by `names`: wholly, or (not `exact`) by some of its
    words. A person's first name alone is their whole name as it is said, so two people
    who share one are never chosen between."""
    if exact:
        return any(words(name) == term or (person and words(name)[:1] == term)
                   for name in names)
    return any(set(term) <= set(words(name)) for name in names)


def place_label(place: Place) -> str:
    if place.kind == "mpim":
        return f"the group message {place.name}"
    return f"#{place.name}"


async def find(reader: SlackReader, said: str, *, channels: bool = True,
               people: str = "direct") -> Place | Person:
    """What `said` names: one conversation or one person, or `Unclear`.

    `people` says which members may be meant: `direct` only those the owner has a direct
    conversation with (which is then what is returned, as a `Place`), `all` any member
    (returned as a `Person`), `none` nobody. An exact name wins over a part of one; more
    than one match at the same strength is never chosen between.
    """
    shown = clip(said, NAME_CHARS)
    if (ref := REF.fullmatch(said.strip())) and channels:
        return await reader.place(ref.group(1))
    term, kind = _term(said)
    if not term:
        raise Unclear(NO_SUCH.format(said=shown or "that"), status="no_match")
    if people == "all" and set(term) <= _ME:
        return Person(reader.owner, ("you",))
    places = await reader.places() if channels or people == "direct" else []
    named = [place for place in places if place.kind != "im" and place.name] if (
        channels and kind != "person") else []
    direct = {place.user: place for place in places if place.kind == "im" and place.user}
    members = await reader.people() if people != "none" and kind != "channel" else []
    members = [each for each in members if each.id != reader.owner]
    without: list[Person] = []
    if people == "direct":
        without = [each for each in members if each.id not in direct]
        members = [each for each in members if each.id in direct]
    for exact in (True, False):
        found: list[tuple[Candidate, Place | Person]] = [
            (Candidate(place_label(place), place.id), place)
            for place in named if _matches(term, (place.name,), exact=exact)
        ]
        for person in members:
            if not _matches(term, person.names, exact=exact, person=True):
                continue
            if people == "direct":
                place = direct[person.id]
                found.append((Candidate(f"{person.label}, a direct message", place.id), place))
            else:
                found.append((Candidate(person.label), person))
        if len(found) == 1:
            return found[0][1]
        if found:
            raise Unclear(SEVERAL.format(said=shown),
                          tuple(candidate for candidate, _ in found[:MAX_CANDIDATES]))
    if people == "direct" and any(
        _matches(term, person.names, exact=False) for person in without
    ):
        raise Unclear(NO_DIRECT.format(said=shown), status="no_match")
    raise Unclear(NO_SUCH.format(said=shown), status="no_match")


# --- the plan ------------------------------------------------------------------------------


async def answer(reader: SlackReader, filters: Filters) -> Context:
    """Read what `filters` ask for, within the reader's bounds. The one planner.

    Names are resolved first (the owner's conversations, then the member list, each asked
    at most once), then exactly one of the five reads is made, then what came back is
    attributed. Raises `Unclear` or `SlackReadError`.
    """
    place: Place | None = None
    person: Person | None = None
    if filters.conversation:
        found = await find(reader, filters.conversation)
        assert isinstance(found, Place)
        place = found
    if filters.thread:
        ref = REF.fullmatch(filters.thread)
        assert ref is not None  # `parse` held it to the pattern
        if place is not None and place.id != ref.group(1):
            raise Unclear("that thread is not in that conversation: name one or the other")
        place = place or await reader.place(ref.group(1))
    if filters.sender:
        found = await find(reader, filters.sender, channels=False, people="all")
        assert isinstance(found, Person)
        person = found

    kind = filters.kind
    if kind == THREAD:
        assert place is not None
        reads = await _thread(reader, filters, place, person)
    elif kind == PLACE:
        assert place is not None
        reads = await _latest(reader, filters, place, person)
    elif kind == WAITING:
        reads = await _waiting(reader, filters, place, person)
    else:
        base = MENTION_QUERY.format(owner=reader.owner) if kind == MENTIONS else ""
        reads = await _search(reader, filters, place, person, base=base)
        reader.more = reader.more or len(reads) > filters.limit
        reads = reads[: filters.limit]
    oldest = min((read.ts for read in reads), key=ts_key, default="")
    return await reader.context(reads, limit=filters.limit,
                                before="" if kind == WAITING else oldest)


def _in(read: Read, place: Place | None, person: Person | None) -> bool:
    """Whether `read` is in the place and from the person asked for, when either was."""
    return (place is None or read.channel == place.id) and (
        person is None or read.user == person.id)


def _has(read: Read, text: str) -> bool:
    """Whether `read` holds every word of `text`."""
    return set(words(text)) <= set(words(read.text[:RAW_CHARS]))


def _named(read: Read, place: Place) -> Read:
    """`read` knowing what its conversation is called."""
    kind = place.kind if place.kind in ("im", "mpim") else "channel"
    return replace(read, kind=kind, channel_name=place.name)


def _modifiers(place: Place | None, person: Person | None) -> list[str]:
    """What narrows a search to a place and a person, as Slack documents it. Built only
    from ids and a name Slack returned, each held to its shape; the same narrowing is
    applied again to what comes back."""
    narrowed = []
    if person is not None and _MEMBER.fullmatch(person.id):
        narrowed.append(f"from:<@{person.id}>")
    if place is not None:
        if place.kind == "im" and _MEMBER.fullmatch(place.user):
            narrowed.append(f"in:<@{place.user}>")
        elif place.name and _BARE.fullmatch(place.name):
            narrowed.append(f"in:{place.name}")
    return narrowed


async def _search(reader: SlackReader, filters: Filters, place: Place | None,
                  person: Person | None, *, base: str = "") -> list[Read]:
    """One search, newest first: the words, what narrows them, and `base` before both."""
    query = " ".join(part for part in (base, filters.text, *_modifiers(place, person)) if part)
    page = await reader.search(query, days=filters.days, before=filters.before)
    reader.more = reader.more or page.more
    reads = [read for read in page.reads if _in(read, place, person)]
    if base:  # what mentions the owner is what somebody else wrote
        reads = [read for read in reads if _theirs(read, reader.owner)]
    return reads


async def _latest(reader: SlackReader, filters: Filters, place: Place,
                  person: Person | None) -> list[Read]:
    """The latest messages of one conversation within the window, oldest first."""
    asked = PAGE_MESSAGES if person is not None else filters.limit
    page = await reader.history(place.id, limit=asked, latest=filters.before or None,
                                oldest=f"{reader.oldest(filters.days):.6f}")
    reads = [_named(read, place) for read in page.reads if _in(read, None, person)]
    reader.more = reader.more or page.more or len(reads) > filters.limit
    return list(reversed(reads[: filters.limit]))


async def _thread(reader: SlackReader, filters: Filters, place: Place,
                  person: Person | None) -> list[Read]:
    """One thread within the window, oldest first: the latest of it when it is long."""
    ref = REF.fullmatch(filters.thread)
    assert ref is not None
    page = await reader.replies(place.id, ref.group(2), latest=filters.before or None,
                                oldest=f"{reader.oldest(filters.days):.6f}")
    reads = [_named(read, place) for read in page.reads if _in(read, None, person)
             and (not filters.text or _has(read, filters.text))]
    reader.more = reader.more or page.more or len(reads) > filters.limit
    return reads[-filters.limit:]


def _theirs(read: Read, owner: str) -> bool:
    """Written by somebody else: not the owner, and not a bot or one of Slack's notices."""
    return bool(read.user) and read.user != owner and not read.automated


async def _all(calls: list[Awaitable[Any]]) -> list[Any]:
    """Every call's answer, a failure in its place: one conversation that cannot be read
    does not cost the rest."""
    answers = await asyncio.gather(*calls, return_exceptions=True)
    for each in answers:
        if isinstance(each, asyncio.CancelledError):
            raise each
        if isinstance(each, BaseException) and not isinstance(each, SlackReadError):
            log.warning("a Slack read failed unexpectedly (%s)", type(each).__name__)
    return answers


async def _waiting(reader: SlackReader, filters: Filters, place: Place | None,
                   person: Person | None) -> list[Read]:
    """What is waiting on the owner, as best it can be told (see the module's docstring)."""
    owner = reader.owner
    narrowed = " ".join(_modifiers(place, person))
    sent, mentioned = await _all([
        reader.search(f"{TO_ME_QUERY} {narrowed}".strip(), days=filters.days),
        reader.search(f"{MENTION_QUERY.format(owner=owner)} {narrowed}".strip(),
                      days=filters.days),
    ])
    if not isinstance(sent, Page) and not isinstance(mentioned, Page):
        raise sent if isinstance(sent, SlackReadError) else SlackReadError("unreachable")
    for half in (sent, mentioned):
        if isinstance(half, Page):
            reader.more = reader.more or half.more
        else:
            # Half of the question could not be asked: what is found is not all there is.
            reader.incomplete = True
    waiting: list[Read] = []

    # (a) Direct and group conversations whose last word is somebody else's.
    newest: dict[str, Read] = {}
    for read in sent.reads if isinstance(sent, Page) else []:
        if read.kind in ("im", "mpim") and _theirs(read, owner) and _in(read, place, None):
            newest.setdefault(read.channel, read)
    candidates = list(newest.values())[:MAX_CANDIDATES]
    reader.more = reader.more or len(newest) > MAX_CANDIDATES
    latest = await _all([reader.history(read.channel, limit=LATEST) for read in candidates])
    for read, page in zip(candidates, latest, strict=True):
        if not isinstance(page, Page):
            reader.incomplete = True  # could not be confirmed, so it is not claimed
            continue
        last = next((each for each in page.reads if not each.automated), None)
        if last is None or not _theirs(last, owner) or owner in last.reply_users:
            continue  # the owner had the last word, or answered it in its thread
        if last.repliers_unlisted:
            reader.more = True  # whether they answered in its thread cannot be told
            continue
        if _in(last, None, person):
            waiting.append(replace(last, kind=read.kind, channel_name=read.channel_name,
                                   link=read.link if last.ts == read.ts else ""))

    # (b) Mentions in a channel or a thread where the owner has not written since.
    mentions = [read for read in mentioned.reads if _theirs(read, owner)
                and read.kind == "channel" and _in(read, place, person)] if isinstance(
                    mentioned, Page) else []
    reader.more = reader.more or len(mentions) > MAX_CANDIDATES
    mentions = mentions[:MAX_CANDIDATES]
    after = await _all([_since(reader, read) for read in mentions])
    for read, later in zip(mentions, after, strict=True):
        if not isinstance(later, Page):
            reader.incomplete = True
        elif any(each.user == owner and ts_key(each.ts) > ts_key(read.ts)
                 for each in later.reads):
            continue
        elif later.more:
            reader.more = True  # more was written after it than was read: not claimed
        else:
            waiting.append(read)

    if filters.text:
        waiting = [read for read in waiting if _has(read, filters.text)]
    waiting.sort(key=lambda read: ts_key(read.ts), reverse=True)
    reader.more = reader.more or len(waiting) > filters.limit
    return waiting[: filters.limit]


async def _since(reader: SlackReader, read: Read) -> Page:
    """What was written after `read` where the owner would have answered it: its thread,
    and for a message that is not in one, the conversation as well."""
    # One page: when there is more than that after it, it is not claimed either way.
    thread = await reader.replies(read.channel, read.root, oldest=read.ts, pages=1)
    if read.thread_ts:
        return thread
    after = await reader.history(read.channel, oldest=read.ts, limit=SINCE)
    return Page(thread.reads + after.reads, thread.more or after.more)
