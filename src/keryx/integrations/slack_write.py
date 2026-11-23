"""Sending one message as the owner: a new message or a reply in a thread, nothing else.

The client behind the `slack_message` plugin (`plugins/slack_message.py`) and `keryx slack
message`. It works with the user token of a workspace at the `write` level, so what it
posts is the owner's own message, to other people. That is the most this service can do
to somebody who is not its owner, and it is held to the least:

**It can call two methods and no other.** `HttpSlackWriter.call` refuses anything that is
not in `WRITE_METHODS` before a request is made: `chat.postMessage`, and
`conversations.open` for the owner's direct conversation with a member they have not
written to before. No edit, no delete, no file, no reaction, no join, no schedule, no
broadcast of a reply to its channel. It is a separate client from the one that reads
(`integrations/slack_read.py`), which stays unable to post by construction.

**The text is posted as it was said.** `&`, `<` and `>` are escaped, so nothing in it can
become a mention, a link with other words on it, or a channel-wide notification; and a
message that tries one in words (`@channel`, `@here`, `@everyone`) is refused outright,
in one sentence, by `message_problem`.

**Who it goes to is resolved, never guessed** (`recipient`): through the same resolver as
reading (`slack_query.find`), against the owner's own conversations and the member list.
A name that could be several is `Unclear` with the candidates; one that matches nothing
says so. A conversation is only ever one the owner is in.

Confirming is the caller's job and is enforced there, in code: the voice tool sends only
on a second call that carries the value its first call returned, and the command sends
only with `--confirm`.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from keryx.integrations import slack_query
from keryx.integrations.slack import is_conversation, is_ts
from keryx.integrations.slack_context import NAME_CHARS, REF, clip
from keryx.integrations.slack_read import (
    API_URL,
    REQUEST_TIMEOUT_S,
    Person,
    Place,
    SlackReader,
    Unclear,
    classify,
)
from keryx.logging_util import quiet_http_loggers

log = logging.getLogger("keryx.slack.write")

#: Every Web API method this module may call: post one message, and open the direct
#: conversation it goes to. Nothing else is ever sent with the owner's token.
WRITE_METHODS = frozenset({"chat.postMessage", "conversations.open"})
#: The most one message may hold. It is one message: longer is not cut into several.
MESSAGE_CHARS = 3000

#: A notification of a whole channel, as Slack writes one or as somebody types one. The
#: word counts unless a letter or a digit touches it on either side: `_@channel_` and
#: `hi.@here` are refused, and an address like `me@here.com` is not one.
_BROADCAST = re.compile(
    r"<!(?:channel|here|everyone|subteam)"
    r"|(?<![A-Za-z0-9])@(?:channel|here|everyone)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

NO_MESSAGE = "There is nothing to send: the message is empty. Ask what it should say."
TOO_LONG = (
    f"That is longer than one Slack message may be here ({MESSAGE_CHARS} characters). Say "
    "so in one sentence; a long piece of writing is a task to hand over."
)
NO_BROADCAST = (
    "A message that notifies a whole channel (@channel, @here, @everyone) is never sent "
    "for them. Say so in one sentence, and offer it without that."
)
TO_THEMSELVES = (
    "That would be a message to themselves. What Keryx sends them is another tool: say "
    "so in a few words."
)
NOT_THEIRS = (
    "That conversation is not one of theirs that could be found, so nothing can be sent "
    "there. Say so in one sentence."
)
SENTENCES = {
    "rate_limited": "Slack is limiting how often it can be asked just now, and the message "
                    "was not sent. Say so in one sentence.",
    "unreachable": "Slack could not be reached, so it is not known whether the message "
                   "was sent. Say exactly that, and that they should look before sending "
                   "it again.",
    "signed_out": "Slack no longer takes the token it was given, and the message was not "
                  "sent. Say that it needs `keryx auth login slack --access write` at "
                  "their keyboard again.",
    "missing_scope": "The Slack app may not send as them yet, and the message was not "
                     "sent. Say that the app has to be reinstalled at the write level, "
                     "with `keryx auth login slack --access write` at their keyboard.",
    "not_found": "Slack does not know that conversation, or they are not in it, and the "
                 "message was not sent. Say so in one sentence.",
}


class SlackWriteError(Exception):
    """A message could not be sent: a status, and a sentence written to be spoken.

    Never a response body and never the token: `code` is one of `SENTENCES`, or Slack's
    own error code when it is one.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    @property
    def sentence(self) -> str:
        return SENTENCES.get(self.code) or (
            f"Slack refused the message ({self.code}), and it was not sent. Say so in one "
            "sentence."
        )

    def as_dict(self) -> dict[str, str]:
        return {"status": self.code, "message": self.sentence}


class SlackWriteApi(Protocol):
    """The two calls there are, as the owner (a fake in the tests)."""

    async def call(self, method: str, payload: Mapping[str, Any]) -> dict: ...

    async def aclose(self) -> None: ...


class HttpSlackWriter:
    """`SlackWriteApi` over HTTPS with a user token. It has `call`, held to
    `WRITE_METHODS`, and nothing else."""

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        quiet_http_loggers()
        self._client = httpx.AsyncClient(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_S,
            transport=transport,
        )

    async def call(self, method: str, payload: Mapping[str, Any]) -> dict:
        """One `POST` of a method in `WRITE_METHODS`; its answer, or a `SlackWriteError`."""
        if method not in WRITE_METHODS:
            raise SlackWriteError("not_a_write")
        try:
            response = await self._client.post(method, json=dict(payload))
        except httpx.HTTPError:
            raise SlackWriteError("unreachable") from None
        if response.status_code == 429:
            raise SlackWriteError("rate_limited")
        try:
            answer = response.json()
        except ValueError:
            raise SlackWriteError("unreachable") from None
        if not isinstance(answer, dict):
            raise SlackWriteError("unreachable")
        if answer.get("ok"):
            return answer
        raise SlackWriteError(classify(str(answer.get("error") or "")))

    async def aclose(self) -> None:
        await self._client.aclose()


def message_problem(text: str) -> str | None:
    """Why `text` is not sent, in a sentence to say; None when it may be."""
    if not text.strip():
        return NO_MESSAGE
    if len(text) > MESSAGE_CHARS:
        return TOO_LONG
    if _BROADCAST.search(text):
        return NO_BROADCAST
    return None


def escaped(text: str) -> str:
    """`text` as Slack is to show it: its own three characters made literal, so nothing
    in it is read as a mention, a link or a command."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True)
class Recipient:
    """Where one message goes, as Slack names it."""

    #: `person` (their direct conversation with the owner), `channel`, or `thread`.
    kind: str
    #: What Slack calls the person or the conversation.
    label: str
    #: The conversation's id; empty for a person whose direct conversation is opened
    #: only when the message is sent.
    channel: str = ""
    member: str = ""
    #: For a reply: the message its thread hangs from.
    thread_ts: str = ""

    @property
    def target(self) -> str:
        """What names this recipient exactly, to a later run that is not to look a name
        up again: a thread's ref, a conversation's id, or a member's."""
        if self.thread_ts:
            return f"{self.channel}:{self.thread_ts}"
        return self.channel or self.member

    @property
    def said(self) -> str:
        """The recipient in words, for the owner to hear before anything is sent."""
        name = clip(self.label, NAME_CHARS)
        if self.kind == "person":
            return f"a direct message to {name}"
        return f"a reply in a thread in {name}" if self.kind == "thread" else name


async def recipient(reader: SlackReader, to: str) -> Recipient:
    """Who `to` names: a person, a channel, or (a `ref` with a message in it) a thread.

    Resolved with the reading side's resolver and nothing guessed: raises `Unclear`, with
    candidates when a name could be several. A conversation has to be one the owner's own
    list holds, so its name is Slack's and never just an id.
    """
    to = " ".join(str(to or "").split())
    if not to or len(to) > NAME_CHARS:
        raise Unclear("say who it goes to: a person's name, a channel's name, or the ref "
                      "of a message to reply to")
    found = await slack_query.find(reader, to, people="all")
    if isinstance(found, Person):
        if found.id == reader.owner:
            raise Unclear(TO_THEMSELVES)
        return Recipient("person", found.label, member=found.id)
    listed = await reader.places()
    if found not in listed:
        raise Unclear(NOT_THEIRS, status="no_match")
    label = await _label(reader, found)
    ref = REF.fullmatch(to)
    if ref and ref.group(2):
        return Recipient("thread", label, channel=found.id, thread_ts=ref.group(2))
    kind = "person" if found.kind == "im" else "channel"
    return Recipient(kind, label, channel=found.id, member=found.user)


def exact(to: str) -> Recipient | None:
    """`to` as a recipient named exactly, by the id an earlier run printed; None when it
    is a name. Nothing is looked up: the id is what is sent to, and all that is said."""
    to = to.strip()
    if ref := slack_query.as_ref(to):
        channel, ts = ref.group(1), ref.group(2) or ""
        return Recipient("thread" if ts else "channel", channel, channel=channel, thread_ts=ts)
    if member := slack_query.member_id(to):
        return Recipient("person", member, member=member)
    return None


async def _label(reader: SlackReader, place: Place) -> str:
    """What `place` is called: a channel's name, or for a direct conversation the other
    member's."""
    if place.kind != "im":
        return slack_query.place_label(place)
    name = await reader.name(place.user)
    return place.id if name == "somebody" else name


@dataclass(frozen=True)
class Sent:
    """One message as Slack took it: where it landed."""

    channel: str
    ts: str


async def send(api: SlackWriteApi, to: Recipient, text: str) -> Sent:
    """Post `text` to `to` as the owner: one new message, or one reply in its thread.

    Raises `SlackWriteError`. The text is checked again here, whatever the caller did,
    and nothing is posted that `message_problem` refuses.
    """
    if message_problem(text):
        raise SlackWriteError("refused_message")
    channel = to.channel
    if not channel:
        opened = await api.call("conversations.open", {"users": to.member})
        found = opened.get("channel")
        channel = found.get("id") if isinstance(found, dict) else ""
    if not is_conversation(channel):
        raise SlackWriteError("unreachable")
    payload: dict[str, Any] = {
        "channel": channel,
        "text": escaped(text),
        # As it was said: no name in it is turned into a mention, and nothing unfurls.
        "link_names": False,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if to.thread_ts:
        if not is_ts(to.thread_ts):
            raise SlackWriteError("not_found")
        payload["thread_ts"] = to.thread_ts
    answer = await api.call("chat.postMessage", payload)
    ts = answer.get("ts")
    return Sent(channel=channel, ts=ts if is_ts(ts) else "")
