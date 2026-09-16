"""The transport-agnostic voice session (spec §3.2, §3.3).

`VoiceSession` is the heart of Jarvis: it owns one conversation, whether that
conversation arrives over a phone call or the Mac's microphone. It knows nothing about
Twilio or PortAudio — only the `Transport` protocol — and nothing about OpenAI beyond
the `RealtimeProvider` protocol.

Shape of a session:

- `run()` connects the provider, asks for a greeting and then runs two pumps
  concurrently: transport -> provider (caller audio, DTMF, hangup) and provider ->
  transport (assistant audio, barge-in, tool calls, transcripts, reconnects). Whichever
  pump finishes first ends the session, and teardown happens in exactly one place.
- **Barge-in** is a phone-only concern. The local device is half-duplex (the mic is gated
  while the speaker plays), so there the model is never interrupted mid-sentence. On the
  phone, `SpeechStarted` means the caller talked over the assistant: drop the queued
  playback and tell the model how much of its audio was actually heard, so the
  conversation history matches what happened.
- **Ending** is deliberately unhurried: `request_end()` only marks the session as ending;
  the run loop tears down once the response that is speaking has finished (or after
  `END_GRACE_SECONDS`), so a goodbye is never cut off mid-word. Every wait is bounded,
  because a local session that never ends leaves the wake-word runner deaf: the silence
  timer is armed from session start (not just from the first response), the goodbye it
  asks for is itself backstopped, and an opening that cannot even be sent ends the
  session on the spot.

Send failures (a socket that dropped between two awaits) are swallowed everywhere:
the provider reports the drop as a `Disconnected` event, which is where reconnects are
handled. Crashing a pump because one `send_audio` lost a race would end the call instead.
"""

import asyncio
import contextlib
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from jarvis.audio.util import ms_for_bytes
from jarvis.config import Settings, secure_dir, secure_file
from jarvis.continuity.briefing import Briefing, BriefingSource
from jarvis.events import EventBus, SessionEnded, SessionStarted
from jarvis.logging_util import mask_number
from jarvis.prompts import render_voice_prompt
from jarvis.realtime.base import (
    AudioDelta,
    Disconnected,
    FunctionCall,
    ProviderError,
    ProviderEvent,
    RealtimeProvider,
    ResponseDone,
    ResponseStarted,
    SessionConfig,
    SpeechStarted,
    Transcript,
)
from jarvis.tools.registry import ToolContext, ToolRegistry
from jarvis.transports.base import DRAIN_TIMEOUT_SECONDS, AudioIn, Dtmf, Hangup, Transport

log = logging.getLogger("jarvis.session")

OPENING_MESSAGE = "[session opened] Greet the user briefly."
RECONNECT_MESSAGE = "[system] The connection was reset; briefly apologize and continue."
SILENCE_MESSAGE = "[system] The user has been silent; say a brief goodbye."
WRAP_UP_MESSAGE = "[system] The call will end in 30 seconds; wrap up."
ANNOUNCE_INSTRUCTIONS = "Briefly tell the user about this in one or two sentences."

# What the model is told about a PIN typed on the keypad. Never the digits themselves.
PIN_LOCKOUT_MESSAGE = (
    "[system] Too many failed PIN attempts. Say a brief goodbye; the call will end."
)
PIN_ACCEPTED_MESSAGE = (
    "[system] The caller entered the correct PIN on the keypad and is now authorized for "
    "all tasks. Say nothing about the PIN — not that it worked, not that he is authorized "
    "— and carry straight on with what he asked for."
)
PIN_REJECTED_MESSAGE = (
    "[system] The caller entered an incorrect PIN on the keypad. Ask them to try again, in "
    "one sentence, without repeating any digits back."
)
#: What the model is told after a keypad entry, per `submit_pin` status. A lockout is
#: absent because `submit_pin` has already said its piece.
KEYPAD_PIN_MESSAGES = {"authorized": PIN_ACCEPTED_MESSAGE, "invalid": PIN_REJECTED_MESSAGE}


class Keypad(Protocol):
    """Whatever wants the keypad digits the PIN did not take (`jarvis.approvals`).

    The PIN comes first and always: digits only reach here once the session is authorized,
    so "he keyed something in" can never be mistaken for "he keyed the PIN in". `digit`
    returns the `[system]` note to put to the model, or None when the key meant nothing to
    it — an unclaimed digit is silently dropped, exactly as it is today.
    """

    def digit(self, session_id: str, key: str) -> str | None: ...  # pragma: no cover


# How many PINs a caller may get wrong before the call ends (spec §3.3).
PIN_MAX_ATTEMPTS = 3
# How long a half-typed PIN survives between two keypresses.
DTMF_RESET_SECONDS = 5.0
# Keys that are not part of a PIN: `#` submits what has been typed, `*` is ignored.
DTMF_NON_DIGITS = ("#", "*")

# How long a requested end waits for the speaking response to finish before hanging up.
END_GRACE_SECONDS = 10.0
# How far before `max_call_seconds` the model is told to wrap up (see WRAP_UP_MESSAGE).
MAX_CALL_WARNING_SECONDS = 30.0


class SessionState(StrEnum):
    NEW = "new"
    RUNNING = "running"
    ENDING = "ending"  # an end was requested; the last response may still be speaking
    ENDED = "ended"


class VoiceSession:
    """One voice conversation: a transport on one side, a realtime model on the other."""

    def __init__(
        self,
        transport: Transport,
        provider: RealtimeProvider,
        settings: Settings,
        tools: ToolRegistry,
        bus: EventBus,
        *,
        authorized: bool,
        opening_context: str | None = None,
        session_id: str | None = None,
        registry: "SessionRegistry | None" = None,
        briefer: BriefingSource | None = None,
        keypad: Keypad | None = None,
        opening_task_id: int | None = None,
    ) -> None:
        self._transport = transport
        self._provider = provider
        self._settings = settings
        self._tools = tools
        self._bus = bus
        self._registry = registry
        self._opening_context = opening_context
        self._briefer = briefer
        self._keypad = keypad
        #: Filled in by `run()` before the prompt is built; an empty one until then, so a
        #: session that never ran still renders a prompt.
        self._briefing = Briefing()

        self.session_id = session_id or secrets.token_hex(4)
        self.channel: str = transport.channel
        self.caller: str | None = transport.caller
        self.authorized = authorized
        #: The task whose result `opening_context` carries, on a call Jarvis placed about it
        #: (a call-back, a restart's confirmation). Set only from a token Jarvis minted, and
        #: the one task `mark_reported` may stamp before the PIN: the call opened by saying it.
        self.opening_task_id = opening_task_id

        self._state = SessionState.NEW
        self._finish_now = asyncio.Event()
        self._end_reason: str | None = None
        self._end_after_response: str | None = None  # reason to end on the next response
        self._grace_expired = False

        self._response_active = False
        self._current_item_id: str | None = None
        self._item_first_ts_ms: float | None = None
        self._item_bytes_sent = 0
        self._last_audio_ts_ms: int | None = None

        self._pin_attempts = 0
        self._dtmf_buffer = ""
        self._dtmf_last = 0.0

        self._tool_tasks: set[asyncio.Task] = set()
        self._silence_task: asyncio.Task | None = None
        self._max_call_task: asyncio.Task | None = None
        self._grace_task: asyncio.Task | None = None

    def __repr__(self) -> str:
        return f"<VoiceSession {self.session_id} {self.channel} {self._state}>"

    # --- public surface ----------------------------------------------------

    @property
    def is_live(self) -> bool:
        """True while the session can still speak (not ending, not ended)."""
        return self._state is SessionState.RUNNING

    @property
    def trusted(self) -> bool:
        """True when what is private may reach this session: always locally, after the PIN
        on the phone.

        Caller id is spoofable, so an allowed number proves nothing. Until this is true a
        call is not briefed (memory, unheard results) and is announced nothing.
        """
        return self.channel != "phone" or self.authorized

    @property
    def response_active(self) -> bool:
        """True while the model is producing a response."""
        return self._response_active

    @property
    def transcript_path(self) -> Path:
        return self._settings.data_dir / "calls" / f"{self.session_id}.log"

    async def run(self) -> None:
        """Run the session to completion. Returns once the call has been torn down."""
        self._briefing = await self._load_briefing()
        config = self._build_config()
        try:
            await self._provider.connect(config)
        except Exception:
            log.exception("session %s could not connect to the provider", self.session_id)
            await self._safe_call(self._transport.hangup)
            raise

        if self._state is SessionState.NEW:
            self._state = SessionState.RUNNING
        if self._registry is not None:
            self._registry.add(self)
        self._append_transcript(
            f"--- session {self.session_id} channel={self.channel} caller={self.caller or 'none'}"
        )
        await self._bus.publish(SessionStarted(self.session_id, self.channel, self.caller))
        log.info(
            "session %s started (%s, caller %s)",
            self.session_id,
            self.channel,
            mask_number(self.caller),
        )

        opened = await self._safe_call(
            self._provider.inject_message, self._opening_message(), respond=True
        )
        if not opened:
            # Nothing will ever be spoken here, and no `ResponseDone` will arrive to arm
            # the silence backstop: end now rather than leave the local runner blocked.
            self.request_end("open_failed")
        self._arm_max_call_timer()
        # Armed from the start, not just from the first `ResponseDone`: if the greeting
        # response never happens (a rejected `response.create` surfaces only as a
        # non-fatal error), this is the one thing that still ends a local session.
        self._arm_silence_timer()

        pumps = [
            asyncio.create_task(self._pump_transport(), name=f"transport-{self.session_id}"),
            asyncio.create_task(self._pump_provider(), name=f"provider-{self.session_id}"),
        ]
        finish = asyncio.create_task(self._finish_now.wait(), name=f"finish-{self.session_id}")
        try:
            await asyncio.wait([*pumps, finish], return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Runs on the normal path and on cancellation alike; the awaits below only
            # get interrupted if someone cancels this task a second time.
            await self._stop_tasks([*pumps, finish], report=True)
            await self._teardown()

    async def announce(self, text: str) -> bool:
        """Speak an out-of-band message (a finished task, say). False if not live."""
        if not self.is_live:
            return False
        log.info("session %s announcing: %s", self.session_id, text)
        return await self._safe_call(
            self._provider.inject_message,
            f"[system] {text}",
            respond=True,
            response_instructions=ANNOUNCE_INSTRUCTIONS,
        )

    def request_end(self, reason: str = "user") -> None:
        """Ask for the session to end once the response that is speaking has finished."""
        if self._state in (SessionState.ENDING, SessionState.ENDED):
            return
        self._state = SessionState.ENDING
        self._end_reason = reason
        self._cancel_silence_timer()
        log.info("session %s ending (%s)", self.session_id, reason)
        self._maybe_finish()

    def authorize(self) -> None:
        """Mark the caller as authorized for destructive work (PIN accepted)."""
        self.authorized = True
        log.info("session %s authorized", self.session_id)

    async def submit_pin(self, pin: str) -> dict:
        """Check a PIN and authorize the session if it matches (spec §3.3, §5).

        The one place a PIN is ever compared, whether it was spoken or typed. Returns
        `not_configured` / `authorized` / `invalid` (with the attempts left) / `locked`;
        the digits are never logged and never handed back. After `PIN_MAX_ATTEMPTS`
        wrong ones the model is asked for a goodbye and the call ends — a locked session
        stays locked even if the right PIN turns up afterwards.

        A blank configured PIN is *no* PIN, and a blank candidate answers nothing: both
        are refused rather than compared, so `submit_pin("")` can never authorize.
        """
        expected = self._settings.pin
        if not expected:
            return {"status": "not_configured"}
        if self.authorized:
            return {"status": "authorized"}
        if self._pin_attempts >= PIN_MAX_ATTEMPTS:
            return {"status": "locked"}

        candidate = (pin or "").strip()
        if candidate and hmac.compare_digest(candidate.encode(), expected.encode()):
            self.authorize()
            await self._brief_after_pin()
            return {"status": "authorized"}

        self._pin_attempts += 1
        log.warning(
            "session %s: PIN attempt %d of %d failed",
            self.session_id,
            self._pin_attempts,
            PIN_MAX_ATTEMPTS,
        )
        if self._pin_attempts >= PIN_MAX_ATTEMPTS:
            await self._lock_out()
            return {"status": "locked"}
        return {"status": "invalid", "attempts_left": PIN_MAX_ATTEMPTS - self._pin_attempts}

    async def _lock_out(self) -> None:
        """Ask for a goodbye, then end the call once it has been spoken (spec §3.3).

        `request_end()` on the spot would hang up mid-word: the injected `response.create`
        has not round-tripped yet, so nothing is "speaking" and teardown would run
        immediately. Same handshake as the silence timer instead — end on the next
        `ResponseDone` — with a backstop for a goodbye that never comes. If a response is
        already speaking (a keypad entry typed over a sentence) the flag fires on *that*
        response's done, which cuts the goodbye short but never cuts it off mid-word.
        """
        self._end_after_response = "pin_lockout"
        if not await self._safe_call(
            self._provider.inject_message, PIN_LOCKOUT_MESSAGE, respond=True
        ):
            self.request_end("pin_lockout")  # no goodbye is coming; end now
            return
        self._spawn_task(self._lockout_backstop(), name="lockout")

    async def _lockout_backstop(self) -> None:
        """End the call anyway if the lockout goodbye is never spoken."""
        await asyncio.sleep(END_GRACE_SECONDS)
        log.info("session %s: the lockout goodbye was never spoken; ending", self.session_id)
        self.request_end("pin_lockout")

    # --- startup -----------------------------------------------------------

    async def _load_briefing(self) -> Briefing:
        """What this session opens knowing: the unreported tasks and the memory.

        Nothing, for a phone call that has not given the PIN — `_brief_after_pin` fetches it
        then. A briefing that cannot be built is not a reason to drop a call — `Briefer`
        already swallows its own failures, and this catches anything a substitute raises.
        """
        if self._briefer is None or not self.trusted:
            return Briefing()
        try:
            return await self._briefer.build()
        except Exception:
            log.exception("session %s could not build its briefing", self.session_id)
            return Briefing()

    async def _brief_after_pin(self) -> None:
        """Hand a call what the PIN was holding back: the full prompt, and a nudge if due.

        The nudge goes in without a response of its own. Whatever answers the PIN — the tool
        result for a spoken one, the accepted note for a keyed one — is the turn that comes
        next and it now sees both, so the PIN still costs one turn. Before `run()` has
        connected there is nothing to update: `run()` builds the briefing itself, and the
        session is trusted by then. A send that fails is swallowed like any other; the PIN
        still counts.
        """
        if self._state is not SessionState.RUNNING:
            return
        self._briefing = await self._load_briefing()
        await self._safe_call(
            self._provider.update_instructions, self._build_config().instructions
        )
        nudge = self._briefing.after_pin_nudge()
        if nudge:
            await self._safe_call(self._provider.inject_message, nudge, respond=False)

    def _opening_message(self) -> str:
        """The message that opens the session, plus the nudge about anything unreported.

        The nudge rides on the opening message rather than on the system prompt alone
        because a realtime model leads with what it was just handed far more reliably
        than with a section it has to go looking for.
        """
        return (self._opening_context or OPENING_MESSAGE) + self._briefing.opening_nudge()

    def _build_config(self) -> SessionConfig:
        """The provider session: transport's audio format, our prompt, our tools."""
        return SessionConfig(
            instructions=render_voice_prompt(
                self._settings,
                channel=self.channel,
                caller=self.caller,
                authorized=self.authorized,
                opening_context=self._opening_context,
                pending=self._briefing.pending,
                memory=self._briefing.memory,
            ),
            tools=self._tools.schemas(),
            voice=self._settings.openai_voice,
            audio_format=self._transport.audio_format,
            vad_mode=self._settings.vad_mode,
            vad_eagerness=self._settings.vad_eagerness,
            vad_threshold=self._settings.vad_threshold,
            vad_silence_ms=self._settings.vad_silence_ms,
            vad_prefix_ms=self._settings.vad_prefix_ms,
            noise_reduction=self._settings.noise_reduction_for(self.channel),
            # The local device is half-duplex, so the model must not try to interrupt
            # itself: the mic is gated while it speaks and there is nothing to hear.
            interrupt_response=self.channel == "phone",
            transcription_model=self._settings.openai_transcription_model,
        )

    # --- transport -> provider --------------------------------------------

    async def _pump_transport(self) -> None:
        async for event in self._transport.events():
            if isinstance(event, AudioIn):
                if event.timestamp_ms is not None:
                    self._last_audio_ts_ms = event.timestamp_ms
                await self._safe_call(self._provider.send_audio, event.data)
            elif isinstance(event, Dtmf):
                self._on_dtmf(event.digit)
            elif isinstance(event, Hangup):
                log.info("session %s: transport hung up (%s)", self.session_id, event.reason)
                self._end_reason = self._end_reason or "hangup"
                return
        self._end_reason = self._end_reason or "transport closed"

    def _on_dtmf(self, digit: str) -> None:
        """Collect keypad digits into the PIN buffer (spec §3.3).

        The digit is deliberately neither logged nor forwarded to the model: DTMF is how
        the PIN is entered, and it must never reach the transcript. A buffer older than
        `DTMF_RESET_SECONDS` is a different attempt and is thrown away, so a mis-hit does
        not poison the next entry. `#` submits what has been typed; a buffer as long as
        the PIN submits itself, which is what makes `#` optional.
        """
        log.debug("session %s received a keypad digit", self.session_id)
        expected = self._settings.pin
        if self.authorized and self._keypad is not None:
            # The PIN is behind us, so this digit is somebody else's: an approval waiting
            # on a confirmation, today. Still never logged and never sent to the model.
            self._spawn_task(self._offer_digit(digit), name="keypad")
            return
        if not expected or self.authorized or self._pin_attempts >= PIN_MAX_ATTEMPTS:
            return

        now = time.monotonic()
        if now - self._dtmf_last > DTMF_RESET_SECONDS:
            self._dtmf_buffer = ""
        self._dtmf_last = now
        if digit not in DTMF_NON_DIGITS:
            self._dtmf_buffer += digit
        if digit != "#" and len(self._dtmf_buffer) < len(expected):
            return

        entered, self._dtmf_buffer = self._dtmf_buffer, ""
        if not entered:
            return  # a bare `#`, or one trailing a finished entry: nothing to check
        # `_on_dtmf` is called from the transport pump, which cannot await: the check and
        # the note to the model are scheduled, and tracked so teardown cleans them up.
        self._spawn_task(self._check_keypad_pin(entered), name="pin")

    async def _check_keypad_pin(self, pin: str) -> None:
        """Check a keyed-in PIN and tell the model how it went — never what was typed."""
        result = await self.submit_pin(pin)
        message = KEYPAD_PIN_MESSAGES.get(result["status"])
        if message is not None:
            await self._safe_call(self._provider.inject_message, message, respond=True)

    async def _offer_digit(self, digit: str) -> None:
        """Hand a post-PIN digit to whoever is listening, and relay what it decided.

        The keypad is the only thing that may answer an approval, so a broken listener must
        not be able to turn a key press into anything at all: it is caught here and the
        digit is simply dropped, which leaves the prompt where it was.
        """
        try:
            message = self._keypad.digit(self.session_id, digit)
        except Exception:
            log.exception("session %s: the keypad listener failed", self.session_id)
            return
        if message:
            await self._safe_call(self._provider.inject_message, message, respond=True)

    # --- provider -> transport --------------------------------------------

    async def _pump_provider(self) -> None:
        async for event in self._provider.events():
            await self._handle_provider_event(event)
        self._end_reason = self._end_reason or "provider closed"

    async def _handle_provider_event(self, event: ProviderEvent) -> None:
        if isinstance(event, AudioDelta):
            await self._on_audio_delta(event)
        elif isinstance(event, SpeechStarted):
            await self._on_speech_started()
        elif isinstance(event, ResponseStarted):
            self._response_active = True
        elif isinstance(event, ResponseDone):
            self._on_response_done()
        elif isinstance(event, FunctionCall):
            # Beside the pumps, so audio keeps flowing while the tool works.
            self._spawn_task(self._run_tool(event), name=f"tool-{event.name}")
        elif isinstance(event, Transcript):
            self._append_transcript(f"{event.role}: {event.text}")
        elif isinstance(event, ProviderError):
            self._on_provider_error(event)
        elif isinstance(event, Disconnected):
            await self._on_disconnected(event)

    async def _on_audio_delta(self, event: AudioDelta) -> None:
        """Play assistant audio and keep the bookkeeping barge-in needs."""
        if event.item_id != self._current_item_id:
            self._current_item_id = event.item_id
            self._item_first_ts_ms = self._now_ms()
            self._item_bytes_sent = 0
        self._item_bytes_sent += len(event.audio)
        await self._safe_call(self._transport.send_audio, event.audio)

    async def _on_speech_started(self) -> None:
        """The caller started talking: on the phone that is a barge-in."""
        self._cancel_silence_timer()
        if self._end_after_response == "silence":
            # They came back before the goodbye finished: there is nothing to end. A
            # `pin_lockout` goodbye is not called off by talking over it.
            log.info("session %s: the user spoke after the goodbye; staying", self.session_id)
            self._end_after_response = None
        if self.channel != "phone":
            return
        if self._current_item_id is None or self._item_bytes_sent == 0:
            return

        played_ms = self._played_ms()
        log.debug("session %s barge-in after %.0f ms", self.session_id, played_ms)
        await self._safe_call(self._transport.clear)
        await self._safe_call(self._provider.truncate, self._current_item_id, int(played_ms))
        self._reset_item_tracking()

    def _on_response_done(self) -> None:
        self._response_active = False
        if self._end_after_response is not None:
            reason, self._end_after_response = self._end_after_response, None
            self.request_end(reason)
            return
        self._maybe_finish()
        self._arm_silence_timer()

    def _on_provider_error(self, event: ProviderError) -> None:
        log.warning(
            "session %s provider error (%s): %s", self.session_id, event.code, event.message
        )
        if event.fatal:
            self.request_end("provider_error")

    async def _on_disconnected(self, event: Disconnected) -> None:
        """The socket dropped: one reconnect attempt, then apologize or end."""
        if self._state in (SessionState.ENDING, SessionState.ENDED):
            return  # our own close(); the iterator is about to finish
        log.warning("session %s disconnected: %s", self.session_id, event.reason)
        try:
            reconnected = await self._provider.reconnect()
        except Exception:
            log.warning("session %s reconnect raised", self.session_id, exc_info=True)
            reconnected = False

        if not reconnected:
            self.request_end("disconnected")
            return
        self._reset_item_tracking()
        self._response_active = False
        await self._safe_call(self._provider.inject_message, RECONNECT_MESSAGE, respond=True)

    # --- tool calls --------------------------------------------------------

    def _spawn_task(self, coro: Coroutine, *, name: str) -> None:
        """Run `coro` beside the pumps, tracked so teardown cancels what is still going."""
        task = asyncio.create_task(coro, name=f"{name}-{self.session_id}")
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _run_tool(self, call: FunctionCall) -> None:
        """Run one tool and hand its result back, with or without a turn to speak about it.

        A silent tool (`mark_reported`, `end_session`) is called *after* the thing worth
        saying has been said; asking for a response over its result is how the model came
        to repeat a greeting it had already given. Everything else answers a question the
        caller is waiting on, and still gets its turn.
        """
        ctx = ToolContext(session=self, channel=self.channel, caller=self.caller)
        result = await self._tools.call(call.name, call.arguments, ctx)
        await self._safe_call(
            self._provider.submit_tool_result,
            call.call_id,
            result,
            respond=not self._tools.is_silent(call.name),
        )

    # --- timers ------------------------------------------------------------

    def _arm_silence_timer(self) -> None:
        """Local sessions hang up on their own after a stretch of silence."""
        self._cancel_silence_timer()
        timeout = self._settings.local_silence_timeout
        if self.channel != "local" or timeout <= 0 or not self.is_live:
            return
        self._silence_task = asyncio.create_task(
            self._silence_timer(timeout), name=f"silence-{self.session_id}"
        )

    def _cancel_silence_timer(self) -> None:
        if self._silence_task is not None:
            self._silence_task.cancel()
            self._silence_task = None

    async def _silence_timer(self, timeout: float) -> None:
        """Wait out the silence, ask for a goodbye, and end even if none is spoken."""
        await asyncio.sleep(timeout)
        log.info("session %s silent for %.1fs; saying goodbye", self.session_id, timeout)
        self._end_after_response = "silence"
        if not await self._safe_call(self._provider.inject_message, SILENCE_MESSAGE, respond=True):
            self.request_end("silence")  # no goodbye is coming; end now
            return

        # The goodbye normally ends the session on its `ResponseDone`, which cancels this
        # task. If that response never comes, this is the backstop that hangs up anyway.
        await asyncio.sleep(END_GRACE_SECONDS)
        log.info("session %s: the goodbye was never spoken; ending anyway", self.session_id)
        self.request_end("silence")

    def _arm_max_call_timer(self) -> None:
        """Phone calls have a hard length limit, with a warning shortly before it."""
        total = self._settings.max_call_seconds
        if self.channel != "phone" or total <= 0:
            return
        self._max_call_task = asyncio.create_task(
            self._max_call_timer(total), name=f"max-call-{self.session_id}"
        )

    async def _max_call_timer(self, total: float) -> None:
        warn_at = total - MAX_CALL_WARNING_SECONDS
        if warn_at > 0:
            await asyncio.sleep(warn_at)
            await self._safe_call(self._provider.inject_message, WRAP_UP_MESSAGE, respond=False)
            await asyncio.sleep(total - warn_at)
        else:
            await asyncio.sleep(total)
        log.info("session %s hit max_call_seconds (%.0fs)", self.session_id, total)
        self.request_end("max_duration")

    # --- ending ------------------------------------------------------------

    def _maybe_finish(self) -> None:
        """Tear down once an end is requested and nothing is speaking any more."""
        if self._state is not SessionState.ENDING:
            return
        if self._response_active and not self._grace_expired:
            if self._grace_task is None:
                self._grace_task = asyncio.create_task(
                    self._grace_timer(), name=f"grace-{self.session_id}"
                )
            return
        self._finish_now.set()

    async def _grace_timer(self) -> None:
        await asyncio.sleep(END_GRACE_SECONDS)
        log.info("session %s: response did not finish in time; ending anyway", self.session_id)
        self._grace_expired = True
        self._maybe_finish()

    async def _teardown(self) -> None:
        """Stop everything, hang up, close the provider, announce the end. Idempotent."""
        if self._state is SessionState.ENDED:
            return
        self._state = SessionState.ENDED
        reason = self._end_reason or "ended"

        await self._stop_tasks(
            [self._silence_task, self._max_call_task, self._grace_task, *self._tool_tasks]
        )
        self._silence_task = self._max_call_task = self._grace_task = None

        drain = getattr(self._transport, "drain", None)
        if drain is not None:
            # Both transports queue playback — the speaker locally, Twilio's own buffer on
            # the phone — and the model produces audio faster than either plays it, so
            # hanging up here would cut off the goodbye.
            try:
                await asyncio.wait_for(
                    drain(DRAIN_TIMEOUT_SECONDS), DRAIN_TIMEOUT_SECONDS + 1.0
                )
            except Exception:
                log.debug("session %s: transport drain failed", self.session_id, exc_info=True)

        await self._safe_call(self._transport.hangup)
        await self._safe_call(self._provider.close)
        if self._registry is not None:
            self._registry.remove(self)
        await self._bus.publish(
            SessionEnded(
                self.session_id, self.channel, self.caller, reason, authorized=self.authorized
            )
        )
        self._append_transcript(f"--- session ended ({reason})")
        log.info("session %s ended (%s)", self.session_id, reason)

    async def _stop_tasks(
        self, tasks: Iterable[asyncio.Task | None], *, report: bool = False
    ) -> None:
        """Cancel `tasks` and wait for them; optionally log the ones that crashed."""
        pending = [task for task in tasks if task is not None]
        for task in pending:
            task.cancel()
        if not pending:
            return
        for result in await asyncio.gather(*pending, return_exceptions=True):
            if report and isinstance(result, Exception):
                log.error("session %s pump failed", self.session_id, exc_info=result)
                self._end_reason = self._end_reason or "error"

    # --- helpers -----------------------------------------------------------

    async def _safe_call(self, fn: Callable[..., Awaitable], *args, **kwargs) -> bool:
        """Await `fn(...)`; a failed send is logged and swallowed, never raised."""
        try:
            await fn(*args, **kwargs)
            return True
        except Exception:
            log.debug(
                "session %s: %s failed",
                self.session_id,
                getattr(fn, "__name__", fn),
                exc_info=True,
            )
            return False

    def _now_ms(self) -> float:
        """The transport's clock if it has one (Twilio's), else the wall clock."""
        if self._last_audio_ts_ms is not None:
            return float(self._last_audio_ts_ms)
        return time.monotonic() * 1000.0

    def _played_ms(self) -> float:
        """How much of the current assistant item the caller actually heard."""
        first_ts = self._item_first_ts_ms
        elapsed = 0.0 if first_ts is None else self._now_ms() - first_ts
        sent_ms = ms_for_bytes(self._item_bytes_sent, self._transport.audio_format)
        return min(max(elapsed, 0.0), sent_ms)

    def _reset_item_tracking(self) -> None:
        self._current_item_id = None
        self._item_first_ts_ms = None
        self._item_bytes_sent = 0

    def _append_transcript(self, text: str) -> None:
        """Append one line to `data_dir/calls/<session_id>.log`; never fatal.

        The stamp is a full local ISO timestamp rather than a wall clock: `jarvis.continuity.recall`
        reads these back weeks later and "14:02:11" cannot say which day that was. Older
        transcripts stamped with the time alone still parse — recall dates those from the
        file's modification time instead.
        """
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {text}\n"
        try:
            secure_dir(self.transcript_path.parent)
            existed = self.transcript_path.exists()
            with self.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            if not existed:
                # On creation only: this file ends up holding every word of the call.
                secure_file(self.transcript_path)
        except OSError:
            log.warning("session %s: could not write the transcript", self.session_id)


class SessionRegistry:
    """The sessions that are currently live, for announcements (spec §3.3)."""

    def __init__(self) -> None:
        self._sessions: list[VoiceSession] = []

    def add(self, session: VoiceSession) -> None:
        if session not in self._sessions:
            self._sessions.append(session)

    def remove(self, session: VoiceSession) -> None:
        with contextlib.suppress(ValueError):
            self._sessions.remove(session)

    def live(self) -> list[VoiceSession]:
        """Registered sessions that can still speak, oldest first."""
        return [session for session in self._sessions if session.is_live]
