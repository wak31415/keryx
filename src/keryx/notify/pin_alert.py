"""Telling the owner that somebody has been guessing their PIN.

`PinGuard` locks PIN entry on every call once wrong PINs pile up across calls, and the
session whose wrong PIN set the lock publishes `PinLockedOut` — once, not once per lock (see
`Lockout.alert`). This is who hears about it, over every path there is, because a lockout they
never hear about is a PIN being guessed that they cannot change:

- **spoken** into a live session that is already *authorized* — them, on the phone past the
  PIN or at their own microphone. Never into one that is not: that is where the guessing is.
- **Slack**, while the `send_to_slack` plugin is on, which is their written channel —
  asked for at alert time (`slack`), so turning it on needs no restart.
- **a text**, only through `safe_send_sms` and so only when `TwilioOut.can_text`.

Delivery runs beside the publisher rather than inside it: the publisher is the call being
locked out, which hangs up a few seconds later and would take a Slack post with it.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from keryx.config import Settings
from keryx.events import EventBus, PinLockedOut
from keryx.integrations.slack import SlackSender
from keryx.logging_util import mask_number
from keryx.notify.deliver import announce_to_live_sessions, safe_send_sms

log = logging.getLogger("keryx.notify.pin_alert")


def lockout_text(event: PinLockedOut, settings: Settings) -> str:
    """The alert, in one sentence that reads the same spoken, on Slack or as a text."""
    until = datetime.fromtimestamp(event.until).strftime("%H:%M")
    return (
        f"PIN entry on the phone is locked until {until} after {event.failures} wrong PINs in "
        f"the last {settings.pin_failure_window_hours:g} hours, the latest from a caller ID "
        f"ending {mask_number(event.caller)}; if that was not you, change KERYX_PIN."
    )


class PinLockoutAlerter:
    """Subscribes to `PinLockedOut` and tells the owner. `start()`/`stop()` are idempotent."""

    def __init__(
        self,
        bus: EventBus,
        sessions: Any,
        twilio: Any,
        settings: Settings,
        *,
        slack: Callable[[], SlackSender | None] | None = None,
    ) -> None:
        self._bus = bus
        self._sessions = sessions
        self._twilio = twilio
        self._settings = settings
        self._slack = slack
        self._unsubscribe = None
        self._in_flight: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self._bus.subscribe(PinLockedOut, self._on_lockout)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for task in self._in_flight:
            task.cancel()

    def _on_lockout(self, event: PinLockedOut) -> None:
        task = asyncio.get_running_loop().create_task(self.deliver(event), name="pin-alert")
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def deliver(self, event: PinLockedOut) -> None:
        """Every path there is, each on its own. Never raises."""
        text = lockout_text(event, self._settings)
        authorized = SimpleNamespace(
            live=lambda: [s for s in self._sessions.live() if getattr(s, "authorized", False)]
        )
        reached = bool(await announce_to_live_sessions(authorized, text))
        try:
            sender = self._slack() if self._slack is not None else None
            if sender is not None:
                reached = await sender.send(text) or reached
        except Exception:
            log.exception("could not post the PIN lockout to Slack")
        reached = await safe_send_sms(self._twilio, self._settings.owner_number, text) or reached
        if not reached:
            log.warning(
                "the PIN lockout alert reached nobody: no authorized call, no Slack, no text"
            )
