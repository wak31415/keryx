"""Tests for the voice session core.

Every test drives a `FakeTransport` and a `FakeProvider`: no socket, no mic, no model.
Waiting is always bounded — `eventually()` polls with a deadline and `running()` fails
the test rather than hanging if a session refuses to finish.
"""

import asyncio
import contextlib
import logging
import stat

import pytest
from fakes import TIMEOUT, DrainingFakeTransport, FakeProvider, FakeTransport, eventually

from keryx.config import Settings
from keryx.continuity.briefing import OTHERS_NUDGE, Briefing
from keryx.events import EventBus, SessionEnded, SessionStarted
from keryx.realtime.base import (
    AudioDelta,
    Disconnected,
    FunctionCall,
    ProviderError,
    ResponseDone,
    ResponseStarted,
    SpeechStarted,
    Transcript,
)
from keryx.session import (
    ANNOUNCE_INSTRUCTIONS,
    ANNOUNCE_REPORT_INSTRUCTIONS,
    OPENING_MESSAGE,
    RECONNECT_MESSAGE,
    SILENCE_MESSAGE,
    WRAP_UP_MESSAGE,
    SessionRegistry,
    VoiceSession,
)
from keryx.tools import ToolContext, ToolRegistry
from keryx.transports.base import AudioIn, Dtmf, Hangup

# audio/pcmu is 8 kHz 8-bit -> 8 bytes per millisecond.
PCMU_BYTES_PER_MS = 8


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "keryx",
        **overrides,
    )


@contextlib.asynccontextmanager
async def running(session: VoiceSession):
    """Run `session` in a task, and make sure it finishes before the test returns."""
    task = asyncio.create_task(session.run())
    try:
        await eventually(lambda: session.is_live or task.done())
        yield task
    finally:
        session.request_end("test over")
        try:
            await asyncio.wait_for(task, TIMEOUT)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise AssertionError("the session did not finish") from None


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def published(bus):
    records: list[object] = []
    bus.subscribe(SessionStarted, records.append)
    bus.subscribe(SessionEnded, records.append)
    return records


@pytest.fixture
def tools():
    return ToolRegistry()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def phone():
    return FakeTransport(channel="phone", caller="+15555555555", audio_format="audio/pcmu")


@pytest.fixture
def local():
    return FakeTransport(channel="local", caller=None, audio_format="audio/pcm")


@pytest.fixture
def make_session(settings, tools, bus):
    def build(transport, prov, **kwargs) -> VoiceSession:
        kwargs.setdefault("authorized", True)
        return VoiceSession(
            transport, prov, kwargs.pop("settings", settings), tools, bus, **kwargs
        )

    return build


def ended(published) -> list[SessionEnded]:
    return [event for event in published if isinstance(event, SessionEnded)]


# --- startup ---------------------------------------------------------------


async def test_session_config_follows_the_phone_transport(make_session, phone, provider, settings):
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.config is not None)

    config = provider.config
    assert config.audio_format == "audio/pcmu"
    assert config.interrupt_response is True
    assert config.voice == settings.voice == "marin"
    assert config.transcription_model == settings.openai_transcription_model
    assert config.transcription_language is None  # unset: the transcriber guesses
    assert "You are Lyra" in config.instructions
    assert "phone" in config.instructions


async def test_starting_a_session_never_logs_the_callers_number(
    make_session, phone, provider, caplog
):
    """The start line names the caller; `~/.jarvis/logs/keryx.log` is not the place for one."""
    with caplog.at_level(logging.INFO, logger="keryx.session"):
        session = make_session(phone, provider)
        async with running(session):
            await eventually(lambda: provider.config is not None)

    assert "started" in caplog.text
    assert phone.caller not in caplog.text
    assert phone.caller[-4:] in caplog.text


async def test_the_transcript_is_created_owner_only(make_session, phone, provider):
    """It ends up holding every word of the call; the default umask would publish it."""
    session = make_session(phone, provider)

    async with running(session):
        await eventually(session.transcript_path.exists)

    assert stat.S_IMODE(session.transcript_path.stat().st_mode) == 0o600


async def test_local_sessions_are_half_duplex(make_session, local, provider):
    session = make_session(local, provider)

    async with running(session):
        await eventually(lambda: provider.config is not None)

    assert provider.config.audio_format == "audio/pcm"
    assert provider.config.interrupt_response is False


async def test_session_config_carries_the_tool_schemas(make_session, phone, provider, tools):
    async def handler(ctx: ToolContext, args: dict) -> dict:
        return {}

    tools.register("ping", "Ping.", {"type": "object", "properties": {}}, handler)
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.config is not None)

    assert provider.config.tools == tools.schemas()


async def test_each_call_offers_the_tools_on_disk_when_it_starts(
    make_session, phone, provider, tools
):
    async def handler(ctx: ToolContext, args: dict) -> dict:
        return {"pong": True}

    loads: list[str] = []

    def loader(call_tools: ToolRegistry) -> None:
        loads.append("load")
        call_tools.register("owners", "Theirs.", {"type": "object", "properties": {}}, handler)

    tools.set_loader(loader)
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.config is not None)
        provider.feed(FunctionCall(call_id="c1", name="owners", arguments={}))
        await eventually(lambda: provider.tool_results)

    assert [schema["name"] for schema in provider.config.tools] == ["owners"]
    assert provider.tool_results[0][1] == {"pong": True}
    assert loads == ["load"]
    assert "owners" not in tools


async def test_session_opens_with_a_greeting_request(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.injected)

    assert provider.injected[0] == (OPENING_MESSAGE, True, None)


async def test_opening_context_replaces_the_greeting(make_session, phone, provider):
    session = make_session(phone, provider, opening_context="[callback] Task 3 is done.")

    async with running(session):
        await eventually(lambda: provider.injected)

    assert provider.injected[0] == ("[callback] Task 3 is done.", True, None)
    assert "Task 3 is done." in provider.config.instructions


async def test_session_publishes_session_started_and_registers(
    make_session, phone, provider, published
):
    sessions = SessionRegistry()
    session = make_session(phone, provider, registry=sessions)

    async with running(session):
        await eventually(lambda: published != [])
        assert sessions.live() == [session]

    start = published[0]
    assert isinstance(start, SessionStarted)
    assert (start.session_id, start.channel, start.caller) == (
        session.session_id,
        "phone",
        "+15555555555",
    )
    assert sessions.live() == []


async def test_a_failed_connect_hangs_up_the_transport(make_session, phone, provider):
    provider.connect_error = RuntimeError("no socket")
    session = make_session(phone, provider)

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(session.run(), TIMEOUT)

    assert phone.hung_up is True


# --- audio pumps -----------------------------------------------------------


async def test_caller_audio_is_forwarded_to_the_provider(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        phone.feed(AudioIn(b"\x01\x02", timestamp_ms=20))
        phone.feed(AudioIn(b"\x03", timestamp_ms=40))
        await eventually(lambda: provider.sent_audio == [b"\x01\x02", b"\x03"])


async def test_assistant_audio_is_forwarded_to_the_transport(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        provider.feed(AudioDelta(item_id="item_1", audio=b"\xff" * 16))
        await eventually(lambda: phone.sent == [b"\xff" * 16])


async def test_the_first_audio_of_a_call_is_logged_once_with_its_delay(
    make_session, phone, provider, caplog
):
    """A call that opens in silence is one the owner hangs up on: how long it took is the
    first thing to know about it."""
    session = make_session(phone, provider)

    with caplog.at_level("INFO", logger="keryx.session"):
        async with running(session):
            provider.feed(AudioDelta(item_id="item_1", audio=b"\xff" * 16))
            provider.feed(AudioDelta(item_id="item_2", audio=b"\xff" * 16))
            await eventually(lambda: len(phone.sent) == 2)

    firsts = [r for r in caplog.records if "first audio" in r.getMessage()]
    assert len(firsts) == 1 and "after it opened" in firsts[0].getMessage()


# --- barge-in --------------------------------------------------------------


async def test_barge_in_clears_and_truncates_with_the_transport_clock(
    make_session, phone, provider
):
    session = make_session(phone, provider)
    audio = b"\x7f" * (100 * PCMU_BYTES_PER_MS)  # 100 ms of assistant audio

    async with running(session):
        phone.feed(AudioIn(b"\x01", timestamp_ms=1000))
        await eventually(lambda: provider.sent_audio == [b"\x01"])
        provider.feed(AudioDelta(item_id="item_1", audio=audio))
        await eventually(lambda: phone.sent == [audio])
        phone.feed(AudioIn(b"\x02", timestamp_ms=1040))
        await eventually(lambda: len(provider.sent_audio) == 2)

        provider.feed(SpeechStarted(item_id="item_2", audio_start_ms=1040))
        await eventually(lambda: provider.truncations != [])

    assert phone.cleared == 1
    assert provider.truncations == [("item_1", 40)]


async def test_barge_in_caps_played_ms_at_the_audio_actually_sent(make_session, phone, provider):
    session = make_session(phone, provider)
    audio = b"\x7f" * (100 * PCMU_BYTES_PER_MS)  # 100 ms of assistant audio

    async with running(session):
        phone.feed(AudioIn(b"\x01", timestamp_ms=1000))
        await eventually(lambda: provider.sent_audio == [b"\x01"])
        provider.feed(AudioDelta(item_id="item_1", audio=audio))
        await eventually(lambda: phone.sent == [audio])
        phone.feed(AudioIn(b"\x02", timestamp_ms=5000))  # 4 s later: way past the audio
        await eventually(lambda: len(provider.sent_audio) == 2)

        provider.feed(SpeechStarted(item_id="item_2", audio_start_ms=5000))
        await eventually(lambda: provider.truncations != [])

    assert provider.truncations == [("item_1", 100)]


async def test_barge_in_without_assistant_audio_does_nothing(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        provider.feed(SpeechStarted(item_id=None, audio_start_ms=0))
        provider.feed(AudioDelta(item_id="item_1", audio=b"\x00" * 8))
        await eventually(lambda: phone.sent != [])

    assert provider.truncations == []
    assert phone.cleared == 0


async def test_local_sessions_never_barge_in(make_session, local, provider):
    session = make_session(local, provider)

    async with running(session):
        provider.feed(AudioDelta(item_id="item_1", audio=b"\x00" * 480))
        await eventually(lambda: local.sent != [])
        provider.feed(SpeechStarted(item_id="item_2", audio_start_ms=100))
        provider.feed(AudioDelta(item_id="item_1", audio=b"\x00" * 480))
        await eventually(lambda: len(local.sent) == 2)

    assert provider.truncations == []
    assert local.cleared == 0


# --- tool calls ------------------------------------------------------------


async def test_a_function_call_runs_the_tool_and_submits_the_result(
    make_session, phone, provider, tools
):
    seen: list[ToolContext] = []

    async def handler(ctx: ToolContext, args: dict) -> dict:
        seen.append(ctx)
        return {"answer": args["q"]}

    tools.register("ask", "Ask.", {"type": "object", "properties": {}}, handler)
    session = make_session(phone, provider, authorized=False)

    async with running(session):
        provider.feed(FunctionCall(call_id="call_1", name="ask", arguments={"q": "when"}))
        await eventually(lambda: provider.tool_results != [])

    assert provider.tool_results == [("call_1", {"answer": "when"})]
    assert (seen[0].channel, seen[0].caller, seen[0].authorized) == (
        "phone",
        "+15555555555",
        False,
    )
    assert seen[0].session is session


async def test_an_ordinary_tool_result_asks_for_the_turn_that_speaks_about_it(
    make_session, phone, provider, tools
):
    async def handler(ctx: ToolContext, args: dict) -> dict:
        return {"answer": 42}

    tools.register("ask", "Ask.", {"type": "object", "properties": {}}, handler)
    session = make_session(phone, provider)

    async with running(session):
        provider.feed(FunctionCall(call_id="call_1", name="ask", arguments={}))
        await eventually(lambda: provider.tool_results != [])

    assert provider.tool_responses == [True]


async def test_a_silent_tool_result_does_not_buy_another_spoken_turn(
    make_session, phone, provider, tools
):
    """`mark_reported` is called *after* the result was spoken.

    Asking for a response over its answer is what made a call-back greet them, say the
    result, and then say the whole greeting over again.
    """

    async def handler(ctx: ToolContext, args: dict) -> dict:
        return {"reported": [7]}

    tools.register(
        "mark_reported",
        "Bookkeeping.",
        {"type": "object", "properties": {}},
        handler,
        silent=True,
    )
    session = make_session(phone, provider)

    async with running(session):
        provider.feed(FunctionCall(call_id="call_1", name="mark_reported", arguments={}))
        await eventually(lambda: provider.tool_results != [])

    # The output still reaches the conversation — a function call with no answer is worse
    # than a spare sentence — but nothing is generated over it.
    assert provider.tool_results == [("call_1", {"reported": [7]})]
    assert provider.tool_responses == [False]


async def test_a_tool_silent_per_call_asks_for_a_turn_only_when_the_call_says_so(
    make_session, phone, provider, tools
):
    """The call-back that greeted, stamped the task, and then went quiet until asked."""

    async def handler(ctx: ToolContext, args: dict) -> dict:
        return {"reported": [7]}

    tools.register(
        "mark_reported",
        "Bookkeeping.",
        {"type": "object", "properties": {}},
        handler,
        silent=lambda args: args.get("still_to_say") is False,
    )
    session = make_session(phone, provider)

    async with running(session):
        provider.feed(FunctionCall(call_id="early", name="mark_reported",
                                   arguments={"task_ids": [7], "still_to_say": True}))
        await eventually(lambda: len(provider.tool_results) == 1)
        provider.feed(FunctionCall(call_id="late", name="mark_reported",
                                   arguments={"task_ids": [7], "still_to_say": False}))
        await eventually(lambda: len(provider.tool_results) == 2)

    assert provider.tool_responses == [True, False]


async def test_an_unknown_tool_reports_an_error_to_the_model(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        provider.feed(FunctionCall(call_id="call_1", name="nope", arguments={}))
        await eventually(lambda: provider.tool_results != [])

    assert provider.tool_results == [("call_1", {"error": "unknown tool: nope"})]


async def test_a_tool_can_end_the_session_without_deadlocking(
    make_session, phone, provider, tools, published
):
    async def goodbye(ctx: ToolContext, args: dict) -> dict:
        ctx.session.request_end("user")
        return {"ok": True}

    tools.register("end_session", "End.", {"type": "object", "properties": {}}, goodbye)
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    provider.feed(FunctionCall(call_id="call_1", name="end_session", arguments={}))
    await asyncio.wait_for(task, TIMEOUT)

    assert provider.tool_results == [("call_1", {"ok": True})]
    assert [event.reason for event in ended(published)] == ["user"]


# --- transcript ------------------------------------------------------------


async def test_transcripts_are_appended_to_the_call_log(make_session, phone, provider, settings):
    session = make_session(phone, provider)

    async with running(session):
        provider.feed(Transcript(role="user", text="what time is it", item_id="item_1"))
        provider.feed(Transcript(role="assistant", text="Just past nine.", item_id="item_2"))
        await eventually(lambda: session.transcript_path.exists())
        await eventually(lambda: "Just past nine." in session.transcript_path.read_text())

    log_path = settings.data_dir / "calls" / f"{session.session_id}.log"
    assert log_path == session.transcript_path
    lines = log_path.read_text().splitlines()
    assert any(line.endswith("user: what time is it") for line in lines)
    assert any(line.endswith("assistant: Just past nine.") for line in lines)
    assert "phone" in lines[0]
    assert "test over" in lines[-1]


# --- ending ----------------------------------------------------------------


async def test_a_hangup_ends_the_session(make_session, phone, provider, published):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    phone.feed(Hangup("caller hung up"))
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended(published)] == ["hangup"]
    assert provider.closed is True
    assert session.is_live is False


async def test_teardown_hangs_up_closes_and_unregisters(make_session, phone, provider):
    sessions = SessionRegistry()
    session = make_session(phone, provider, registry=sessions)

    async with running(session):
        pass

    assert phone.hung_up is True
    assert provider.closed is True
    assert sessions.live() == []


async def test_the_transport_is_drained_before_hangup(make_session, provider):
    transport = DrainingFakeTransport(channel="local", caller=None, audio_format="audio/pcm")
    session = make_session(transport, provider)

    async with running(session):
        pass

    assert transport.calls == ["drain", "hangup"]
    assert transport.drains == [5.0]


async def test_a_phone_transport_is_drained_too(make_session, provider):
    """Twilio buffers outbound audio, so the goodbye needs draining before the hangup."""
    transport = DrainingFakeTransport(channel="phone", audio_format="audio/pcmu")
    session = make_session(transport, provider)

    async with running(session):
        pass

    assert transport.calls == ["drain", "hangup"]


async def test_request_end_waits_for_the_active_response(make_session, phone, provider, published):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    provider.feed(ResponseStarted(response_id="resp_1"))
    await eventually(lambda: session.response_active)

    session.request_end("user")
    await asyncio.sleep(0.05)
    assert not task.done(), "the session ended while a response was still speaking"

    provider.feed(ResponseDone(response_id="resp_1", status="completed"))
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended(published)] == ["user"]


async def test_request_end_gives_up_after_the_grace_period(
    make_session, phone, provider, monkeypatch
):
    monkeypatch.setattr("keryx.session.END_GRACE_SECONDS", 0.05)
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    provider.feed(ResponseStarted(response_id="resp_1"))
    await eventually(lambda: session.response_active)

    session.request_end("user")  # the response never finishes
    await asyncio.wait_for(task, TIMEOUT)


async def test_request_end_is_idempotent(make_session, phone, provider, published):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    session.request_end("first")
    session.request_end("second")
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended(published)] == ["first"]


# --- silence timeout -------------------------------------------------------


async def test_silence_ends_a_local_session_after_a_goodbye(
    make_session, local, provider, published, tmp_path
):
    settings = make_settings(tmp_path, local_silence_timeout=0.05)
    session = make_session(local, provider, settings=settings)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    provider.feed(ResponseDone(response_id="resp_1", status="completed"))
    await eventually(lambda: any(text == SILENCE_MESSAGE for text, _, _ in provider.injected))

    provider.feed(ResponseDone(response_id="resp_2", status="completed"))
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended(published)] == ["silence"]


async def test_a_local_session_ends_even_if_no_response_ever_arrives(
    make_session, local, provider, published, monkeypatch, tmp_path
):
    """The greeting may never be spoken (a rejected response.create): still hang up."""
    monkeypatch.setattr("keryx.session.END_GRACE_SECONDS", 0.05)
    settings = make_settings(tmp_path, local_silence_timeout=0.05)
    session = make_session(local, provider, settings=settings)

    task = asyncio.create_task(session.run())
    await asyncio.wait_for(task, TIMEOUT)  # not a single provider event ever arrives

    assert [text for text, _, _ in provider.injected] == [OPENING_MESSAGE, SILENCE_MESSAGE]
    assert [event.reason for event in ended(published)] == ["silence"]


async def test_a_failed_opening_injection_ends_the_session(
    make_session, local, provider, published
):
    provider.send_error = RuntimeError("socket gone")
    session = make_session(local, provider)

    task = asyncio.create_task(session.run())
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended(published)] == ["open_failed"]


async def test_user_speech_cancels_the_silence_timer(make_session, local, provider, tmp_path):
    settings = make_settings(tmp_path, local_silence_timeout=0.05)
    session = make_session(local, provider, settings=settings)

    async with running(session):
        provider.feed(ResponseDone(response_id="resp_1", status="completed"))
        await asyncio.sleep(0.01)
        provider.feed(SpeechStarted(item_id="item_1", audio_start_ms=0))
        await asyncio.sleep(0.1)

        assert [text for text, _, _ in provider.injected] == [OPENING_MESSAGE]


async def test_speaking_after_the_silence_goodbye_keeps_the_session(
    make_session, local, provider, tmp_path
):
    """The goodbye was asked for, but the user came back: the session carries on."""
    settings = make_settings(tmp_path, local_silence_timeout=0.05)
    session = make_session(local, provider, settings=settings)

    async with running(session) as task:
        provider.feed(ResponseDone(response_id="resp_1", status="completed"))
        await eventually(lambda: any(text == SILENCE_MESSAGE for text, _, _ in provider.injected))
        provider.feed(SpeechStarted(item_id="item_1", audio_start_ms=0))
        await asyncio.sleep(0.01)
        provider.feed(ResponseDone(response_id="resp_2", status="completed"))
        await asyncio.sleep(0.05)

        assert task.done() is False
        assert session.is_live is True


async def test_phone_sessions_have_no_silence_timeout(make_session, phone, provider, tmp_path):
    settings = make_settings(tmp_path, local_silence_timeout=0.05)
    session = make_session(phone, provider, settings=settings)

    async with running(session):
        provider.feed(ResponseDone(response_id="resp_1", status="completed"))
        await asyncio.sleep(0.1)

        assert [text for text, _, _ in provider.injected] == [OPENING_MESSAGE]


async def test_a_zero_silence_timeout_disables_the_timer(make_session, local, provider, tmp_path):
    settings = make_settings(tmp_path, local_silence_timeout=0)
    session = make_session(local, provider, settings=settings)

    async with running(session):
        provider.feed(ResponseDone(response_id="resp_1", status="completed"))
        await asyncio.sleep(0.05)

        assert [text for text, _, _ in provider.injected] == [OPENING_MESSAGE]


# --- call duration guardrail ----------------------------------------------


async def test_max_call_seconds_warns_then_ends_the_call(
    make_session, phone, provider, published, monkeypatch, tmp_path
):
    monkeypatch.setattr("keryx.session.MAX_CALL_WARNING_SECONDS", 0.05)
    settings = make_settings(tmp_path, max_call_seconds=0.1)
    session = make_session(phone, provider, settings=settings)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    await eventually(lambda: any(text == WRAP_UP_MESSAGE for text, _, _ in provider.injected))
    assert (WRAP_UP_MESSAGE, False, None) in provider.injected

    await asyncio.wait_for(task, TIMEOUT)
    assert [event.reason for event in ended(published)] == ["max_duration"]


async def test_local_sessions_ignore_max_call_seconds(make_session, local, provider, tmp_path):
    settings = make_settings(tmp_path, max_call_seconds=0.05, local_silence_timeout=0)
    session = make_session(local, provider, settings=settings)

    async with running(session):
        await asyncio.sleep(0.1)

        assert [text for text, _, _ in provider.injected] == [OPENING_MESSAGE]


# --- announcements ---------------------------------------------------------


async def test_announce_injects_a_system_message(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        assert await session.announce("Task 3 finished.") is True

    text, respond, instructions = provider.injected[-1]
    assert text == f"[system] Task 3 finished.\n{ANNOUNCE_INSTRUCTIONS}"
    assert respond is True
    assert "mark_reported" not in text  # not a task's result: nothing to stamp


async def test_announce_keeps_the_voice_prompt_for_its_turn(make_session, phone, provider):
    """#82: a response's own `instructions` replace the session's in the Realtime API, so
    an announcement made with them answered without the voice prompt — the one place that
    says to call mark_reported on a result from a "[system]" note."""
    session = make_session(phone, provider)

    async with running(session):
        assert await session.announce("Task 3 finished.", task_id=3) is True

    _, _, instructions = provider.injected[-1]
    assert instructions is None


async def test_a_task_announced_into_a_call_asks_for_its_stamp(make_session, phone, provider):
    """#82: told only in the system prompt, the model stamped no mid-call result at all,
    and the owner heard each one again at the top of the next call."""
    session = make_session(phone, provider)

    async with running(session):
        assert await session.announce("Task 3 finished: done.", task_id=3) is True

    text, _, _ = provider.injected[-1]
    assert text.startswith("[system] Task 3 finished: done.\n")
    assert ANNOUNCE_REPORT_INSTRUCTIONS.format(task_id=3) in text
    assert "task_ids [3]" in text


async def test_announce_is_false_before_and_after_the_session(make_session, phone, provider):
    session = make_session(phone, provider)

    assert await session.announce("too early") is False

    async with running(session):
        pass

    assert await session.announce("too late") is False


async def test_announce_is_false_when_the_provider_send_fails(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        provider.send_error = RuntimeError("socket gone")
        assert await session.announce("Task 3 finished.") is False
        provider.send_error = None


# --- reconnects and errors -------------------------------------------------


async def test_a_disconnect_reconnects_and_apologizes(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        provider.feed(Disconnected("socket closed"))
        await eventually(lambda: provider.reconnects == 1)
        await eventually(
            lambda: any(text == RECONNECT_MESSAGE for text, _, _ in provider.injected)
        )
        provider.feed(AudioDelta(item_id="item_1", audio=b"\x00" * 8))
        await eventually(lambda: phone.sent != [])


async def test_a_failed_reconnect_ends_the_session(make_session, phone, provider, published):
    provider.reconnect_result = False
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    provider.feed(Disconnected("socket closed"))
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended(published)] == ["disconnected"]


async def test_a_fatal_provider_error_ends_the_session(make_session, phone, provider, published):
    session = make_session(phone, provider)

    task = asyncio.create_task(session.run())
    await eventually(lambda: session.is_live)
    provider.feed(ProviderError(code="invalid_api_key", message="nope", fatal=True))
    await asyncio.wait_for(task, TIMEOUT)

    assert [event.reason for event in ended(published)] == ["provider_error"]


async def test_a_non_fatal_provider_error_is_logged_and_survived(
    make_session, phone, provider, caplog
):
    session = make_session(phone, provider)

    async with running(session):
        with caplog.at_level(logging.WARNING, logger="keryx.session"):
            provider.feed(ProviderError(code="whatever", message="odd", fatal=False))
            provider.feed(AudioDelta(item_id="item_1", audio=b"\x00" * 8))
            await eventually(lambda: phone.sent != [])

    assert "odd" in caplog.text


async def test_provider_send_failures_do_not_crash_the_session(make_session, phone, provider):
    session = make_session(phone, provider)

    async with running(session):
        provider.send_error = RuntimeError("socket gone")
        phone.feed(AudioIn(b"\x01", timestamp_ms=10))
        await asyncio.sleep(0.02)
        provider.send_error = None
        phone.feed(AudioIn(b"\x02", timestamp_ms=30))
        await eventually(lambda: provider.sent_audio == [b"\x02"])


# --- misc ------------------------------------------------------------------


async def test_dtmf_digits_reach_the_hook_and_never_the_model(make_session, phone, provider):
    session = make_session(phone, provider)
    seen: list[str] = []
    session._on_dtmf = seen.append  # Task 10 replaces the hook with the PIN collector

    async with running(session):
        phone.feed(Dtmf("7"))
        await eventually(lambda: seen == ["7"])

    assert [text for text, _, _ in provider.injected] == [OPENING_MESSAGE]
    assert provider.sent_audio == []


async def test_authorize_flips_the_flag(make_session, phone, provider):
    session = make_session(phone, provider, authorized=False)

    assert session.authorized is False
    session.authorize()
    assert session.authorized is True


async def test_session_ids_are_short_and_unique(make_session, phone, provider):
    first = make_session(phone, provider)
    second = make_session(phone, provider)

    assert first.session_id != second.session_id
    assert len(first.session_id) <= 12
    assert make_session(phone, provider, session_id="fixed").session_id == "fixed"


def test_session_registry_lists_only_live_sessions(make_session, phone, provider):
    sessions = SessionRegistry()
    session = make_session(phone, provider)

    sessions.add(session)
    assert sessions.live() == []  # registered, but not live until run() starts it

    sessions.remove(session)
    sessions.remove(session)  # removing twice is fine


async def test_the_session_takes_its_turn_detection_from_settings(
    make_session, phone, provider, settings
):
    """How long Keryx waits before answering is a setting, not a constant."""
    tuned = settings.model_copy(
        update={
            "vad_mode": "server",
            "vad_silence_ms": 3000,
            "vad_threshold": 0.6,
            "vad_prefix_ms": 250,
        }
    )
    session = make_session(phone, provider, settings=tuned)

    async with running(session):
        await eventually(lambda: provider.config is not None)

    config = provider.config
    assert config.vad_mode == "server"
    assert config.vad_silence_ms == 3000
    assert config.vad_threshold == 0.6
    assert config.vad_prefix_ms == 250


async def test_the_default_session_waits_for_a_finished_sentence(
    make_session, phone, provider
):
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.config is not None)

    assert provider.config.vad_mode == "semantic"
    assert provider.config.vad_eagerness == "medium"
    # A phone is held to the head, so the near-field profile is the right one for it.
    assert provider.config.noise_reduction == "near_field"


# --- the briefing ----------------------------------------------------------


class FakeBriefer:
    """A `BriefingSource` that hands back a fixed briefing, or refuses to."""

    def __init__(self, briefing: Briefing | None = None, error: Exception | None = None) -> None:
        self.briefing = briefing or Briefing()
        self.error = error
        self.builds = 0

    async def build(self) -> Briefing:
        self.builds += 1
        if self.error is not None:
            raise self.error
        return self.briefing


async def test_a_session_with_no_briefer_opens_exactly_as_it_always_did(
    make_session, phone, provider
):
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.injected != [])

    assert provider.injected[0][0] == OPENING_MESSAGE


async def test_the_briefing_reaches_the_system_prompt(make_session, phone, provider):
    briefer = FakeBriefer(
        Briefing(
            memory="They are mid-way through the orchard sync.",
            pending="- task 41 (finished) — they asked for: the ingest script",
            pending_count=1,
        )
    )
    session = make_session(phone, provider, briefer=briefer)

    async with running(session):
        await eventually(lambda: provider.config is not None)

    instructions = provider.config.instructions
    assert "orchard sync" in instructions
    assert "task 41" in instructions
    assert briefer.builds == 1  # once per session, before the provider is connected


async def test_an_optional_tool_is_described_by_its_own_schema_not_the_prompt(
    make_session, phone, provider, tools
):
    """A plugin says everything about itself in its description; the prompt names none, so
    it never tells the model about a tool it was not given."""

    async def handler(ctx, arguments):
        return {}

    tools.register("cluster_stats", "What the cluster is doing.",
                   {"type": "object", "properties": {}}, handler)
    session = make_session(phone, provider)

    async with running(session):
        await eventually(lambda: provider.config is not None)

    assert "cluster_stats" in {schema["name"] for schema in provider.config.tools}
    assert "cluster_stats" not in provider.config.instructions


async def test_unreported_work_is_pushed_at_the_opening_message_too(
    make_session, phone, provider
):
    """A realtime model leads with what it was just handed far more reliably."""
    briefer = FakeBriefer(Briefing(pending="- task 41 (finished)", pending_count=1))
    session = make_session(phone, provider, briefer=briefer)

    async with running(session):
        await eventually(lambda: provider.injected != [])

    opening = provider.injected[0][0]
    assert opening.startswith(OPENING_MESSAGE)
    assert "1 task finished" in opening


async def test_nothing_unreported_leaves_the_opening_message_alone(
    make_session, phone, provider
):
    session = make_session(phone, provider, briefer=FakeBriefer(Briefing(memory="something")))

    async with running(session):
        await eventually(lambda: provider.injected != [])

    assert provider.injected[0][0] == OPENING_MESSAGE


async def test_a_briefer_that_raises_does_not_cost_the_call(make_session, phone, provider):
    briefer = FakeBriefer(error=RuntimeError("the database is gone"))
    session = make_session(phone, provider, briefer=briefer)

    async with running(session):
        await eventually(lambda: provider.config is not None)

    assert "You are Lyra" in provider.config.instructions
    assert provider.injected[0][0] == OPENING_MESSAGE


async def test_a_call_back_keeps_its_own_opening_context_and_gains_the_nudge(
    make_session, phone, provider
):
    briefer = FakeBriefer(Briefing(pending="- task 41 (finished)", pending_count=2))
    session = make_session(
        phone, provider, briefer=briefer, opening_context="You are calling them back about task 41."
    )

    async with running(session):
        await eventually(lambda: provider.injected != [])

    opening = provider.injected[0][0]
    assert opening.startswith("You are calling them back about task 41.")
    assert "2 tasks finished" in opening


async def test_a_call_back_about_the_only_news_opens_with_its_context_alone(
    make_session, phone, provider
):
    """The task it rang about is still unreported, so it is in the digest too — and a nudge
    about it, "after your greeting", made the model greet, tease and wait (#63)."""
    context = "You are calling them back about task 41."
    briefer = FakeBriefer(Briefing(pending="- task 41", pending_count=1, task_ids=(41,)))
    session = make_session(
        phone, provider, briefer=briefer, opening_context=context, opening_task_id=41
    )

    async with running(session):
        await eventually(lambda: provider.injected != [])

    assert provider.injected[0][0] == context


async def test_a_call_back_hears_of_the_other_news_after_its_own(make_session, phone, provider):
    context = "You are calling them back about task 41."
    briefing = Briefing(pending="- task 40\n- task 41", pending_count=2, task_ids=(40, 41))
    session = make_session(
        phone,
        provider,
        briefer=FakeBriefer(briefing),
        opening_context=context,
        opening_task_id=41,
    )

    async with running(session):
        await eventually(lambda: provider.injected != [])

    assert provider.injected[0][0] == context + OTHERS_NUDGE


def test_the_prompt_is_told_the_agents_the_dispatch_tool_offers():
    """Read off the tool's own schema, so the prompt can never offer a different set."""
    from keryx.session import _dispatch_agents

    agent = {"enum": ["claude", "codex"]}
    schemas = [
        {"name": "web_search", "parameters": {"properties": {}}},
        {"name": "dispatch_task", "parameters": {"properties": {"agent": agent}}},
    ]

    assert _dispatch_agents(schemas) == ["claude", "codex"]
    assert _dispatch_agents(schemas[:1]) == []
    assert _dispatch_agents([{"name": "dispatch_task", "parameters": {"properties": {}}}]) == []
