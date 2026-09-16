"""The two ways a result reaches them, in one place each.

Three modules independently grew the same pair of loops — `Notifier`, the
`RestartCoordinator` and the standalone `restart.watchdog` — each iterating `sessions.live()`,
each swallowing its own exceptions, and each deciding for itself whether a text may be
sent. Three copies of a gate is three chances to get it wrong, and this is the gate where
being wrong is expensive: **`TwilioOut.can_text`, never `configured`.** `SMS_ENABLED` is
off by default (many accounts lack SMS permission for their region, and Slack is the
written channel), so a send gated on `configured` can be an HTTP 400 every time. Outbound
*calls* are a different capability and are unaffected — which matters, because the restart
watchdog's `<Say>` alert is the last thing working when Jarvis is down.

Nothing here raises. A delivery that did not happen comes back as `False`, and the caller
decides whether that is worth a log line, a fallback, or nothing at all.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("jarvis.notify.deliver")


class _Sessions(Protocol):
    """Just the part of `SessionRegistry` this needs — it is imported by `restart.watchdog`."""

    def live(self) -> list: ...


@dataclass(frozen=True)
class Announced:
    """What an announcement got to: any live session, and a phone one specifically.

    `on_phone` is the one that decides whether a text still needs to go out — hearing it
    down the line is the delivery a text would otherwise be duplicating.
    """

    heard: bool = False
    on_phone: bool = False

    def __bool__(self) -> bool:
        return self.heard


async def announce_to_live_sessions(
    sessions: _Sessions,
    text: str,
    *,
    skip: Any = None,
) -> Announced:
    """Speak `text` into every live session. Never raises.

    A session that answers False did not hear it — one already on its way out, or a phone
    call that has not given the PIN (`VoiceSession.announce` refuses those, because what is
    announced is private) — and it counts for nothing, so the caller's fallback still runs.

    `skip(session)` marks a session that must not be spoken to but counts as having
    heard — today that is a session holding the line for the very task being announced,
    whose tool result is about to say the same thing, and hearing it twice in one breath
    is worse than not hearing it here.

    An exception part-way through stops the loop rather than skipping to the next session,
    which is what the three copies of this did and is the safer of the two: whatever broke
    the first `announce` is likely to break the rest, and the caller's fallback is a text.
    """
    heard = False
    on_phone = False
    try:
        for session in sessions.live():
            if skip is not None and skip(session):
                heard = on_phone = True
                continue
            spoken = await session.announce(text)
            heard = heard or spoken
            on_phone = on_phone or (spoken and session.channel == "phone")
    except Exception:
        log.exception("could not announce into the live sessions")
    return Announced(heard, on_phone)


async def safe_send_sms(twilio: Any, to: str | None, body: str) -> bool:
    """Text `body` to `to`. `False`, never an exception, when it did not go.

    The `can_text` gate is asserted here and nowhere else. `to` is allowed to be `None` and
    `twilio` to be missing entirely, because both are ordinary states on a half-configured
    machine and neither is worth a traceback.
    """
    if not to or twilio is None or not twilio.can_text:
        return False
    try:
        await twilio.send_sms(to, body)
    except Exception:
        log.exception("could not send a text")
        return False
    return True
