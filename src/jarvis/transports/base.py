"""Transport interfaces shared by the phone and local channels (spec §3.2).

A transport is the audio pipe of one session: it yields inbound events (caller audio,
DTMF digits, hangup) and accepts outbound audio. The voice session is written against
this protocol only, so it never knows whether it is on a phone call or the Mac's mic.

`AudioFormat` is defined in `jarvis.audio.util` (which needs it for `ms_for_bytes`) and
re-exported here, the canonical place for transport-facing names.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from jarvis.audio.util import AudioFormat

__all__ = [
    "AudioFormat",
    "AudioIn",
    "Dtmf",
    "Hangup",
    "Transport",
    "TransportEvent",
]


@dataclass
class AudioIn:
    """Inbound caller audio in the transport's `audio_format`.

    `timestamp_ms` is the transport's own clock (Twilio's `media.timestamp`) when it has
    one, and None otherwise (the local device has no wire clock).
    """

    data: bytes
    timestamp_ms: int | None = None


@dataclass
class Dtmf:
    """A touch-tone digit pressed by the caller ("0"-"9", "*", "#")."""

    digit: str


@dataclass
class Hangup:
    """The session's audio pipe has ended; `events()` finishes after this."""

    reason: str


TransportEvent = AudioIn | Dtmf | Hangup


class Transport(Protocol):
    """The audio pipe of a single session."""

    channel: Literal["phone", "local"]
    caller: str | None  # E.164 for phone, None for local
    audio_format: AudioFormat  # same format inbound and outbound

    def events(self) -> AsyncIterator[TransportEvent]:
        """Inbound transport events; finishes after `Hangup`."""
        ...

    async def send_audio(self, data: bytes) -> None:
        """Enqueue assistant audio, in `audio_format`, for playback."""
        ...

    async def clear(self) -> None:
        """Drop queued playback audio (barge-in)."""
        ...

    async def hangup(self) -> None:
        """End the call/session; `events()` finishes."""
        ...
