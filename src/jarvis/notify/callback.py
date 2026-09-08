"""What both call-backs agree on: how long the token lives, and how the context reads.

Two things in Jarvis ring him about work that has finished. `notify/notifier.py` does it
for an ordinary task, and `restart/coordinator.py` does it for the one task the notifier
cannot deliver — work that changed Jarvis's own code, whose result has to wait for the
restart that loads it and then arrive alongside "and it is running". They are different
flows on purpose, but they open the same kind of call, so four things have to match:

- **`CALLBACK_TOKEN_TTL_S`** — the single-use stream token has to outlive Twilio ringing
  and being answered, and no longer.
- **`HISTORY_PREAMBLE`** — the tail of the earlier call, framed as memory rather than as a
  script, or the model reads it back to him.
- **`MAX_REQUEST_CHARS`** — how much of what he asked for the context carries.
- **`no_trailing_stop`** — the join between a spoken summary and the sentence around it.

They lived in the notifier, which meant the coordinator imported it at module scope: the
half of the `restart ⇄ notifier` cycle that actually ran at import time, for four pieces
of shared copy. Not in `deliver.py` either — that module's charter is the two ways a result
reaches him, and prompt wording is a different thing.
"""


#: How long the call-back's single-use stream token lives. Twilio has to ring and be
#: answered inside this.
CALLBACK_TOKEN_TTL_S = 120.0
#: What the model is told the transcript is, so it treats it as memory rather than script.
HISTORY_PREAMBLE = (
    " You have no memory of that call, so here is how it ended — do not read it back to "
    "him, just know it:\n{history}\n"
)
#: How much of the original request the call-back context carries.
MAX_REQUEST_CHARS = 200


def no_trailing_stop(text: str) -> str:
    """`text` without a full stop on the end, for a template that supplies its own.

    A spoken summary usually ends in one and the sentence around it always does, so
    without this the context reads "the tests pass.." — which a text-to-speech voice
    does not swallow as gracefully as a reader would.
    """
    return text.rstrip().rstrip(".").rstrip()
