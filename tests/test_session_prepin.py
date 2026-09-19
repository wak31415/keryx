"""What a phone caller can reach before the PIN (spec §3.3, §5; SECURITY.md).

Caller id is spoofable, so a caller on an allowed number has proved nothing. The ruling
these tests hold: on the phone, before the PIN, nothing private is read out, nothing is
announced into the session, and nothing the caller says or does outlives the call. The
local channel is authorized by construction and is unchanged.
"""

import logging

import pytest
from fakes import FakeProvider, FakeTransport, eventually
from test_session import make_settings, running

from jarvis.continuity.briefing import Briefing
from jarvis.continuity.transcripts import was_authorized
from jarvis.events import EventBus, SessionEnded
from jarvis.realtime.base import FunctionCall, Transcript
from jarvis.session import OPENING_MESSAGE, PIN_ACCEPTED_MESSAGE, VoiceSession
from jarvis.tools import ToolRegistry
from jarvis.transports.base import Dtmf
from jarvis.trust import TrustLevel

PIN = "123456"
NONE, POSSESSION = TrustLevel.NONE, TrustLevel.POSSESSION
CALLER = "+15550001111"


MEMORY = "They are waiting on the letter from the lawyer."
DIGEST = "- task 41 (finished) — they asked for: their bank balance\n  Result: 1,234 pounds"


class FakeBriefer:
    """A `BriefingSource` with something private in it, counting how often it is asked."""

    def __init__(self, briefing: Briefing | None = None) -> None:
        self.briefing = briefing or Briefing(memory=MEMORY, pending=DIGEST, pending_count=1)
        self.builds = 0

    async def build(self) -> Briefing:
        self.builds += 1
        return self.briefing


class OrderedProvider(FakeProvider):
    """A `FakeProvider` that also keeps one log of what it was sent, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.order: list[tuple[str, object]] = []

    async def update_instructions(self, instructions: str) -> None:
        await super().update_instructions(instructions)
        self.order.append(("instructions", instructions))

    async def inject_message(self, text, *, respond=True, response_instructions=None) -> None:
        await super().inject_message(
            text, respond=respond, response_instructions=response_instructions
        )
        self.order.append(("message", (text, respond)))

    async def submit_tool_result(self, call_id, output, *, respond=True) -> None:
        await super().submit_tool_result(call_id, output, respond=respond)
        self.order.append(("tool_result", respond))


def private(text: str) -> bool:
    return "lawyer" in text or "1,234 pounds" in text


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def ended(bus):
    events: list[SessionEnded] = []
    bus.subscribe(SessionEnded, events.append)
    return events


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def phone():
    return FakeTransport(channel="phone", caller=CALLER, audio_format="audio/pcmu")


@pytest.fixture
def local():
    return FakeTransport(channel="local", caller=None, audio_format="audio/pcm")


@pytest.fixture
def tools():
    """`submit_pin` exactly as `jarvis.tools.builtin` delegates it."""
    registry = ToolRegistry()

    async def submit_pin(ctx, arguments: dict) -> dict:
        return await ctx.session.submit_pin(arguments["pin"])

    registry.register(
        "submit_pin", "Check a PIN.", {"type": "object", "properties": {}}, submit_pin
    )
    return registry


@pytest.fixture
def make_session(tmp_path, bus, tools):
    def build(
        transport, prov, *, authorized=False, digest_before_pin=True, **kwargs
    ) -> VoiceSession:
        settings = make_settings(
            tmp_path,
            pin=PIN,
            projects={"orchard": str(tmp_path)},
            projects_root=tmp_path / "no-projects",
            skills_dir=tmp_path / "no-skills",
            digest_before_pin=digest_before_pin,
        )
        return VoiceSession(
            transport, prov, settings, tools, bus, authorized=authorized, **kwargs
        )

    return build


# --- who is trusted --------------------------------------------------------


def test_a_phone_call_is_trusted_only_once_authorized(make_session, phone, local, provider):
    assert make_session(phone, provider).trusted is False
    assert make_session(phone, provider, possession=True).trusted is False
    assert make_session(phone, provider, authorized=True).trusted is True
    assert make_session(local, provider, authorized=False).trusted is True


# --- the memory waits for the PIN; the digest does not ----------------------


async def test_an_unauthorized_call_opens_knowing_nothing_of_theirs(make_session, phone, provider):
    """The memory and the map of their world. The digest is the exception below it."""
    briefer = FakeBriefer(Briefing(memory=MEMORY))
    session = make_session(phone, provider, briefer=briefer)

    async with running(session):
        await eventually(lambda: provider.injected != [])

        assert MEMORY not in provider.config.instructions
        assert "orchard" not in provider.config.instructions  # nor what they are working on
        assert provider.injected[0][0] == OPENING_MESSAGE  # nothing unheard, so no nudge


async def test_an_unauthorized_call_hears_the_digest_before_the_pin(make_session, phone, provider):
    """The owner's ruling: unheard news is not gated by the PIN, spoofers and all."""
    session = make_session(phone, provider, briefer=FakeBriefer())

    async with running(session):
        await eventually(lambda: provider.injected != [])

        assert DIGEST in provider.config.instructions
        assert MEMORY not in provider.config.instructions
        assert provider.injected[0][0].startswith(OPENING_MESSAGE)
        assert "1 task finished" in provider.injected[0][0]


async def test_digest_before_pin_off_restores_the_old_silence(make_session, phone, provider):
    briefer = FakeBriefer()
    session = make_session(phone, provider, briefer=briefer, digest_before_pin=False)

    async with running(session):
        await eventually(lambda: provider.injected != [])

        assert DIGEST not in provider.config.instructions
        assert provider.injected[0][0] == OPENING_MESSAGE
        assert briefer.builds == 0  # not even read


async def test_a_call_jarvis_placed_hears_the_digest_whatever_the_setting(
    make_session, phone, provider
):
    """Possession is proof the owner is holding the phone; the setting is about strangers."""
    session = make_session(
        phone, provider, possession=True, briefer=FakeBriefer(), digest_before_pin=False
    )

    async with running(session):
        await eventually(lambda: provider.injected != [])

        assert DIGEST in provider.config.instructions
        assert MEMORY not in provider.config.instructions


async def test_a_spoken_pin_delivers_the_briefing_before_the_turn_that_answers_it(
    make_session, phone
):
    """One turn is all a PIN may cost: the nudge rides in silently, ahead of the tool result
    whose response is the next thing they hear."""
    provider = OrderedProvider()
    session = make_session(phone, provider, briefer=FakeBriefer(), digest_before_pin=False)

    async with running(session):
        await eventually(lambda: provider.injected != [])
        provider.feed(FunctionCall(call_id="c1", name="submit_pin", arguments={"pin": PIN}))
        await eventually(lambda: provider.tool_results != [])

    kinds = [kind for kind, _ in provider.order]
    assert kinds[1:4] == ["instructions", "message", "tool_result"]
    instructions, (nudge, nudge_responds), responds = (value for _, value in provider.order[1:4])
    assert private(instructions)
    assert "orchard" in instructions
    assert "everything — the PIN is in" in instructions
    assert nudge.startswith("[system] 1 task finished") and nudge_responds is False
    assert responds is True


async def test_a_keyed_pin_delivers_it_ahead_of_the_accepted_note(make_session, phone):
    provider = OrderedProvider()
    session = make_session(phone, provider, briefer=FakeBriefer(), digest_before_pin=False)

    async with running(session):
        await eventually(lambda: provider.injected != [])
        for digit in PIN:
            phone.feed(Dtmf(digit))
        await eventually(lambda: (PIN_ACCEPTED_MESSAGE, True, None) in provider.injected)

    sent = [value for kind, value in provider.order if kind != "tool_result"][1:]
    assert private(sent[0])
    assert sent[1][0].startswith("[system] 1 task finished") and sent[1][1] is False
    assert sent[2] == (PIN_ACCEPTED_MESSAGE, True)


async def test_with_nothing_unheard_the_pin_only_updates_the_instructions(
    make_session, phone, provider
):
    session = make_session(phone, provider, briefer=FakeBriefer(Briefing(memory=MEMORY)))

    async with running(session):
        await eventually(lambda: provider.injected != [])
        await session.submit_pin(PIN)

        assert [private(update) for update in provider.instruction_updates] == [True]
        assert [text for text, *_ in provider.injected] == [OPENING_MESSAGE]


async def test_the_pin_does_not_re_announce_a_digest_the_call_already_had(make_session, phone):
    """It was spoken at the greeting. The PIN buys the memory, not the news a second time."""
    provider = OrderedProvider()
    session = make_session(phone, provider, briefer=FakeBriefer())

    async with running(session):
        await eventually(lambda: provider.injected != [])
        await session.submit_pin(PIN)

    nudges = [value[0] for kind, value in provider.order if kind == "message"]
    assert not any("has not heard yet" in text for text in nudges[1:])
    assert private(provider.instruction_updates[-1])  # but the memory did arrive


async def test_a_wrong_pin_delivers_nothing(make_session, phone, provider):
    briefer = FakeBriefer()
    session = make_session(phone, provider, briefer=briefer)

    async with running(session):
        await eventually(lambda: provider.injected != [])
        read_for_the_digest = briefer.builds
        await session.submit_pin("999999")

        assert provider.instruction_updates == []
        assert briefer.builds == read_for_the_digest  # nothing re-read, nothing re-sent
        assert MEMORY not in provider.config.instructions


async def test_a_spoken_pin_takes_a_call_jarvis_placed_to_full(make_session, phone, provider):
    """The escape hatch that was always there, and does not depend on the keypad at all.

    `submit_pin` is one of the five tools ungated at every level, so a call-back whose
    keypad is busy with an approval menu can still reach FULL by saying the digits.
    """
    session = make_session(phone, provider, possession=True, briefer=FakeBriefer())

    async with running(session):
        await eventually(lambda: provider.injected != [])
        assert session.trust is POSSESSION
        provider.feed(FunctionCall(call_id="c1", name="submit_pin", arguments={"pin": PIN}))
        await eventually(lambda: session.authorized)

        assert session.trust is TrustLevel.FULL
        assert private(provider.instruction_updates[-1])


async def test_a_pin_accepted_before_the_call_is_connected_is_briefed_at_the_opening(
    make_session, phone, provider
):
    briefer = FakeBriefer()
    session = make_session(phone, provider, briefer=briefer)
    await session.submit_pin(PIN)

    async with running(session):
        await eventually(lambda: provider.injected != [])

    assert private(provider.config.instructions)
    assert provider.instruction_updates == []
    assert briefer.builds == 1


async def test_a_local_session_is_briefed_from_the_start(make_session, local, provider):
    session = make_session(local, provider, authorized=True, briefer=FakeBriefer())

    async with running(session):
        await eventually(lambda: provider.injected != [])

    assert private(provider.config.instructions)
    assert "1 task finished" in provider.injected[0][0]


async def test_a_briefing_that_cannot_be_sent_does_not_cost_the_pin(make_session, phone, provider):
    session = make_session(phone, provider, briefer=FakeBriefer())

    async with running(session):
        await eventually(lambda: provider.injected != [])
        provider.send_error = ConnectionError("the socket dropped")

        assert await session.submit_pin(PIN) == {"status": "authorized"}
        assert session.trusted is True
        provider.send_error = None


# --- the transcript says whether they ever gave the PIN ----------------------


async def test_the_transcript_marks_a_call_unauthorized_until_the_pin(
    make_session, phone, provider
):
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: session.transcript_path.exists())
        assert was_authorized(session.transcript_path.read_text()) is False
        await session.submit_pin(PIN)

    written = session.transcript_path.read_text()
    assert "authorized=no" in written.splitlines()[0]
    assert was_authorized(written) is True


async def test_a_local_transcript_is_authorized_from_the_start(make_session, local, provider):
    session = make_session(local, provider, authorized=True)

    async with running(session):
        await eventually(lambda: session.transcript_path.exists())

    assert "authorized=yes" in session.transcript_path.read_text().splitlines()[0]


# --- a spoken PIN is never written down ------------------------------------


async def test_a_spoken_pin_reaches_neither_the_transcript_nor_the_log(
    make_session, phone, provider, caplog
):
    session = make_session(phone, provider)
    caplog.set_level(logging.DEBUG)

    async with running(session):
        provider.feed(Transcript(role="assistant", text="What's your PIN?", item_id="a"))
        provider.feed(Transcript(role="user", text="It's one two three 4 5 6.", item_id="b"))
        provider.feed(FunctionCall(call_id="c1", name="submit_pin", arguments={"pin": PIN}))
        await eventually(lambda: provider.tool_results != [])
        await eventually(lambda: "[PIN]" in session.transcript_path.read_text())

    written = session.transcript_path.read_text()
    assert "user: It's [PIN]." in written
    assert "4 5 6" not in written and "three" not in written
    assert PIN not in caplog.text and "4 5 6" not in caplog.text


# --- nothing is announced into it ------------------------------------------


async def test_news_is_announced_into_a_call_before_the_pin(make_session, phone, provider):
    """A result that lands mid-call reaches it, for the same reason the digest does."""
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.injected != [])

        assert await session.announce("Task 41 finished: 1,234 pounds", needs=NONE) is True
        assert "Task 41 finished" in provider.injected[-1][0]


async def test_with_the_digest_off_nothing_is_announced_before_the_pin(
    make_session, phone, provider
):
    """And False, so the notifier and the broker do not count it as having told them."""
    session = make_session(phone, provider, digest_before_pin=False)

    async with running(session):
        await eventually(lambda: provider.injected != [])

        assert await session.announce("Task 41 finished: 1,234 pounds", needs=NONE) is False
        assert [text for text, *_ in provider.injected] == [OPENING_MESSAGE]

        await session.submit_pin(PIN)
        assert await session.announce("Task 41 finished: 1,234 pounds", needs=NONE) is True


async def test_an_approval_is_never_announced_into_a_call_that_could_not_answer_it(
    make_session, phone, provider
):
    """What is waiting on their screen is not news; only a call that can press a key hears it."""
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.injected != [])

        assert await session.announce("Claude wants to run: git push", needs=POSSESSION) is False
        assert [text for text, *_ in provider.injected] == [OPENING_MESSAGE]


async def test_a_call_jarvis_placed_hears_an_approval(make_session, phone, provider):
    session = make_session(phone, provider, possession=True)

    async with running(session):
        await eventually(lambda: provider.injected != [])

        assert await session.announce("Claude wants to run: git push", needs=POSSESSION) is True


async def test_a_local_session_is_announced_to_as_before(make_session, local, provider):
    session = make_session(local, provider, authorized=True)

    async with running(session):
        assert await session.announce("Task 41 finished.") is True


# --- a call Jarvis placed itself --------------------------------------------


def test_a_session_knows_which_task_its_opening_context_is_about(make_session, phone, provider):
    assert make_session(phone, provider, opening_task_id=41).opening_task_id == 41
    assert make_session(FakeTransport(), FakeProvider()).opening_task_id is None


# --- what outlives the call ------------------------------------------------


async def test_a_call_that_never_gave_the_pin_ends_unauthorized(
    make_session, phone, provider, ended
):
    """The memory writer reads this flag: without it, nothing the caller said is kept."""
    session = make_session(phone, provider)

    async with running(session):
        pass

    assert [event.authorized for event in ended] == [False]


async def test_a_call_that_gave_the_pin_ends_authorized(make_session, phone, provider, ended):
    session = make_session(phone, provider)

    async with running(session):
        assert (await session.submit_pin(PIN))["status"] == "authorized"

    assert [event.authorized for event in ended] == [True]


async def test_a_local_session_ends_authorized(make_session, local, provider, ended):
    session = make_session(local, provider, authorized=True)

    async with running(session):
        pass

    assert [event.authorized for event in ended] == [True]
