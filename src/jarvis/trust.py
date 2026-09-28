"""How much one call has proved about who is on it.

There used to be one bit — `VoiceSession.authorized`, earned only by the PIN — and it was
both too coarse and wrong about direction. Too coarse, because "may hear a result that is
already waiting" and "may spend the owner's machine" are not the same permission. Wrong
about direction, because it treated every phone call the same way when an *outbound* call
is different in kind: Jarvis dialled a number the owner configured, so whoever answered is
holding that phone. Caller id on the way in proves nothing; a number Jarvis chose on the
way out is not something a caller can forge.

Three levels, ordered, so everything downstream asks for "at least this much" rather than
listing the levels it will take:

- `NONE` — an inbound phone call before the PIN. It is talking to a stranger until proved
  otherwise, and what such a call may *hear* is the ruling in `briefing.py`: the standing
  briefing, because the PIN is the line between reading and acting. What it may *do* is
  nothing that outlives the call.
- `POSSESSION` — a call Jarvis placed to one of `Settings.owner_numbers`, proved by the
  single-use stream token Jarvis minted for it
  (`jarvis.stream_tokens.confers_possession`), and by nothing else: never Twilio's
  `From`/`To`, which is the inbound claim the PIN exists to doubt.
- `FULL` — the PIN was given on this call, or the channel is the machine's own microphone.

`VoiceSession.trusted` is the old spelling of `FULL` and still means exactly that.
"""

from enum import IntEnum


class TrustLevel(IntEnum):
    """What a call has proved. Ordered: compare with `>=`, never with a set of members."""

    #: An inbound phone call before the PIN. Caller id is spoofable, so this is a stranger.
    NONE = 0
    #: A call Jarvis placed to a phone of the owner's: whoever answered is holding it.
    POSSESSION = 1
    #: The PIN was given on this call, or it is the local microphone.
    FULL = 2
