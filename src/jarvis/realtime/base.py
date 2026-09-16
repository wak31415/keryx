"""Provider-agnostic realtime interfaces: session config, typed events, protocol (spec §3.2).

These are the names the voice session is written against; a provider implementation
(currently `jarvis.realtime.openai`) translates its wire protocol into these events.
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from jarvis.audio.util import AudioFormat


@dataclass
class SessionConfig:
    """Everything the provider needs to open a session.

    `audio_format` is used for both the input and the output stream: the phone path is
    `audio/pcmu` end to end (no transcoding), the local path `audio/pcm`.
    `interrupt_response` is False on the half-duplex local path, where the mic is gated
    while the assistant speaks and barge-in is therefore impossible.
    `vad_mode` decides what counts as "he has stopped talking" — see the field below.
    """

    instructions: str
    tools: list[dict]  # OpenAI function-tool schemas
    voice: str
    audio_format: AudioFormat
    #: "semantic" lets the model judge whether the sentence is finished — it waits through
    #: a pause for thought, where a timer cuts in. "server" is the fixed silence timer,
    #: `vad_silence_ms` long, which the semantic mode ignores (and the API rejects there).
    vad_mode: Literal["server", "semantic"] = "semantic"
    vad_eagerness: Literal["low", "medium", "high", "auto"] = "medium"
    vad_threshold: float = 0.5
    vad_silence_ms: int = 500
    vad_prefix_ms: int = 300
    #: Server-side background-noise suppression on the *input* stream. `near_field` is a
    #: phone held to the head, `far_field` a microphone across the room; None leaves it
    #: off, which is the API's own default. It is worth setting mostly because noise the
    #: model hears as speech is noise that barges in on it mid-sentence.
    noise_reduction: Literal["near_field", "far_field"] | None = None
    interrupt_response: bool = True
    transcription_model: str | None = "gpt-4o-mini-transcribe"


@dataclass
class AudioDelta:
    """A chunk of assistant audio, decoded, in the session's `audio_format`."""

    item_id: str
    audio: bytes


@dataclass
class SpeechStarted:
    """Server VAD detected the user starting to speak (barge-in trigger)."""

    item_id: str | None
    audio_start_ms: int


@dataclass
class SpeechStopped:
    """Server VAD detected the end of the user's turn."""


@dataclass
class ResponseStarted:
    response_id: str


@dataclass
class ResponseDone:
    response_id: str
    status: str


@dataclass
class FunctionCall:
    call_id: str
    name: str
    arguments: dict


@dataclass
class Transcript:
    role: Literal["user", "assistant"]
    text: str
    item_id: str | None


@dataclass
class ProviderError:
    """A provider-side error. `fatal` means the session cannot continue on this socket."""

    code: str | None
    message: str
    fatal: bool


@dataclass
class Disconnected:
    """The provider socket dropped. The session may try `reconnect()` once."""

    reason: str


ProviderEvent = (
    AudioDelta
    | SpeechStarted
    | SpeechStopped
    | ResponseStarted
    | ResponseDone
    | FunctionCall
    | Transcript
    | ProviderError
    | Disconnected
)


class RealtimeProvider(Protocol):
    """The realtime model connection the voice session drives.

    Only one response may be active at a time: `submit_tool_result` and
    `inject_message(respond=True)` go through an internal response queue, so a
    `response.create` issued while a response is in flight is held back until the
    active one finishes. Conversation items are always sent immediately, and
    `submit_tool_result(respond=False)` sends one without asking for a turn at all.
    """

    async def connect(self, config: SessionConfig) -> None: ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[ProviderEvent]:
        """Typed provider events; ends after `Disconnected` unless `reconnect()` succeeds."""
        ...

    async def send_audio(self, data: bytes) -> None:
        """Append caller audio, in `config.audio_format`, to the input buffer."""
        ...

    async def submit_tool_result(
        self, call_id: str, output: dict | str, *, respond: bool = True
    ) -> None:
        """Create the `function_call_output` item and, by default, request a response.

        `respond=False` submits the output and stops there, for a tool whose result has
        nothing to say out loud (`ToolRegistry.is_silent`).
        """
        ...

    async def inject_message(
        self,
        text: str,
        *,
        respond: bool = True,
        response_instructions: str | None = None,
    ) -> None:
        """Insert a system message (e.g. a task announcement) and optionally speak it."""
        ...

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """Trim an assistant item to the audio the caller actually heard (barge-in)."""
        ...

    async def cancel_response(self) -> None: ...

    async def reconnect(self) -> bool:
        """One attempt to re-open the socket and re-send the session config."""
        ...

    async def update_instructions(self, instructions: str) -> None:
        """Replace the system prompt of the open session; the rest of the config stands.

        Remembered, so a `reconnect()` re-sends the new prompt. How a phone call gets its
        briefing once the PIN is accepted, having opened without it.
        """
        ...


#: Makes one fresh (unconnected) provider per session.
ProviderFactory = Callable[[], RealtimeProvider]
