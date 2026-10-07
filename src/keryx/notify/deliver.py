"""The two ways a result reaches them, in one place each.

Three modules independently grew the same pair of loops — `Notifier`, the
`RestartCoordinator` and the standalone `restart.watchdog` — each iterating `sessions.live()`,
each swallowing its own exceptions, and each deciding for itself whether a text may be
sent. Three copies of a gate is three chances to get it wrong, and this is the gate where
being wrong is expensive: **`TwilioOut.can_text`, never `configured`.** `SMS_ENABLED` is
off by default (many accounts lack SMS permission for their region, and Slack is the
written channel), so a send gated on `configured` can be an HTTP 400 every time. Outbound
*calls* are a different capability and are unaffected — which matters, because the restart
watchdog's `<Say>` alert is the last thing working when Keryx is down.

Nothing here raises. A delivery that did not happen comes back as `False`, and the caller
decides whether that is worth a log line, a fallback, or nothing at all.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from keryx.trust import TrustLevel

log = logging.getLogger("keryx.notify.deliver")


class _Sessions(Protocol):
    """Just the part of `SessionRegistry` this needs — it is imported by `restart.watchdog`."""

    def live(self) -> list: ...


@dataclass(frozen=True)
class Announced:
    """What an announcement got to: any live session, and one that counts as delivery.

    `delivered` is the one that decides whether a text still needs to go out — hearing it
    down the line is the delivery a text would otherwise be duplicating. It takes a phone
    session at `POSSESSION` or better (`keryx.trust`) and nothing less: a call that has
    proved nothing may still *hear* the news, because caller id is spoofable and the
    digest is not gated on it, but whoever heard it may not be the owner, so the text and
    the call-back still have to go.
    """

    heard: bool = False
    delivered: bool = False

    def __bool__(self) -> bool:
        return self.heard


async def announce_to_live_sessions(
    sessions: _Sessions,
    text: str,
    *,
    needs: TrustLevel = TrustLevel.FULL,
    skip: Any = None,
    on_heard: Callable[[], Awaitable[object]] | None = None,
) -> Announced:
    """Speak `text` into every live session that is trusted enough for it. Never raises.

    `needs` is what *this* announcement requires, because they are not alike: a finished
    task is news and needs nothing (`TrustLevel.NONE`, subject to `BRIEFING_BEFORE_PIN`); a
    prompt waiting on the owner's screen needs a call that could answer it. The default is
    `FULL`, so an announcement that has not thought about it gets the old behaviour.

    A session that answers False did not hear it — one already on its way out, or one that
    has not proved enough for this (`VoiceSession.announce` decides) — and it counts for
    nothing, so the caller's fallback still runs.

    `skip(session)` marks a session that must not be spoken to but counts as having
    heard — today that is a session holding the line for the very task being announced,
    whose tool result is about to say the same thing, and hearing it twice in one breath
    is worse than not hearing it here.

    An exception part-way through stops the loop rather than skipping to the next session,
    which is what the three copies of this did and is the safer of the two: whatever broke
    the first `announce` is likely to break the rest, and the caller's fallback is a text.

    `on_heard` goes to each session that speaks it, which runs it once the owner has
    started to hear it (`VoiceSession.announce`); it may run more than once.
    """
    heard = False
    delivered = False
    try:
        for session in sessions.live():
            if skip is not None and skip(session):
                heard = delivered = True
                continue
            extra = {} if on_heard is None else {"on_heard": on_heard}
            spoken = await session.announce(text, needs=needs, **extra)
            heard = heard or spoken
            delivered = delivered or (spoken and _counts_as_delivery(session))
    except Exception:
        log.exception("could not announce into the live sessions")
    return Announced(heard, delivered)


def _counts_as_delivery(session: Any) -> bool:
    """True when hearing it down this line means the owner has been told.

    A session with no `trust` at all is one of the small stand-ins other modules pass in
    (`pin_alert` hands over its own filtered view), and those have already decided.
    """
    if session.channel != "phone":
        return False  # they may have walked away from the microphone
    return getattr(session, "trust", TrustLevel.FULL) >= TrustLevel.POSSESSION


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
