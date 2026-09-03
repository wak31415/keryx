"""The part of a restart that runs *through* the death of the process (spec §3.3).

`RestartCoordinator` has two halves, and both of them are inside Jarvis: one asks for the
restart, the other confirms it on the far side. That covers every failure except the one
that matters most when Jarvis has just changed its own code — the service that never comes
back. A new process is what runs `resume()`, so a change that will not import means there
is no new process, no `resume()`, no call, no text: total silence, which reads exactly like
a restart that went fine.

This is the third half. It is started by `_execute()` immediately before the restart
command, in a transient unit of its own so the cgroup kill cannot take it with the service
(see `restart.watch_command`), and it does one thing: watch `restart.json` until the
restart resolves itself, and speak up when it does not.

* **The record disappears** — Jarvis came back and told him. Nothing to do.
* **The record says `failed`** — Jarvis came back far enough to know it went wrong, and
  has already said so on its own. Also nothing to do; a second text says nothing new.
* **The record is still `pending` when the deadline passes** — nobody is coming. This is
  the case that exists for: text him the detail, then ring him with a short spoken alert.

The call is plain `<Say>` TwiML on purpose. Every other call Jarvis places is answered by
its own media stream, and the media stream is served by the process that is not running.

Nothing here raises. It is the last thing standing on a machine where something has
already gone wrong, and its own failure must land in a log rather than in a traceback
nobody is left to catch.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from jarvis.config import Settings
from jarvis.logging_util import mask_number
from jarvis.logscan import LogErrors, errors_since
from jarvis.notify.twilio_out import TwilioOut, say_twiml
from jarvis.restart import (
    RECORD_NAME,
    RestartRecord,
    RestartStore,
    format_duration,
    health_probe,
)

log = logging.getLogger("jarvis.restart_watch")

#: How long the service is given to come back and confirm itself. Generous on purpose:
#: systemd's `RestartSec`, a slow start, the wait for the phone server to listen and a
#: deferred call-back all happen inside it, and a false alarm is a phone call he did not
#: need about a service that is fine.
DEADLINE_S = 300.0
#: How often the record is looked at. It is one small file on local disk.
POLL_S = 3.0
#: A text is one message, not three.
MAX_SMS_CHARS = 900

#: What happened, in one word — returned by `watch()` and printed by `jarvis restart-watch`.
NOTHING = "nothing"  # there was no pending restart to watch
CONFIRMED = "confirmed"  # Jarvis came back and delivered the confirmation itself
REPORTED = "reported"  # Jarvis came back, knows it failed, and has already said so
ALERTED = "alerted"  # nobody came back; we told him
MUTE = "mute"  # nobody came back, and we had no way to tell him

DOWN_SMS = (
    "Jarvis did not come back after the restart{reason}. It has been {age} and nothing is "
    "answering on the machine.{task}{errors} It was running {version}. "
    "Check with: jarvis restart --status"
)
STUCK_SMS = (
    "Jarvis restarted{reason} and is answering again, but never confirmed it — no call and "
    "no text went out.{task}{errors} Check with: jarvis restart --status"
)
#: The spoken alert. Short, and it carries no traceback: a text message holds the detail,
#: and a phone call exists to make him look at it.
DOWN_SPOKEN = "Jarvis did not come back after the restart, and nothing is answering."
STUCK_SPOKEN = "Jarvis restarted but never confirmed it."
SPOKEN_ALERT = "This is a Jarvis alert. {headline} I have sent you the details by text."
SPOKEN_ALERT_ONLY = "This is a Jarvis alert. {headline} Check the machine when you can."


async def watch(
    settings: Settings,
    *,
    store: RestartStore | None = None,
    twilio: Any | None = None,
    deadline_s: float = DEADLINE_S,
    poll_s: float = POLL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    probe: Callable[[Settings], int | None] = health_probe,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Watch the pending restart to its end; returns one of the outcome words above.

    Never raises: this is called from a process whose entire job is to be the thing that
    still works.
    """
    try:
        return await _watch(
            settings,
            store=store or RestartStore(settings.data_dir / RECORD_NAME),
            twilio=twilio if twilio is not None else TwilioOut(settings),
            deadline_s=deadline_s,
            poll_s=poll_s,
            sleep=sleep,
            probe=probe,
            clock=clock,
        )
    except Exception:
        log.exception("the restart watchdog failed")
        return MUTE


async def _watch(
    settings: Settings,
    *,
    store: RestartStore,
    twilio: Any,
    deadline_s: float,
    poll_s: float,
    sleep: Callable[[float], Awaitable[None]],
    probe: Callable[[Settings], int | None],
    clock: Callable[[], float],
) -> str:
    record = store.load()
    if record is None:
        log.info("no restart is pending; nothing to watch")
        return NOTHING

    deadline = clock() + deadline_s
    while True:
        if record is None:
            log.info("the restart confirmed itself")
            return CONFIRMED
        if record.state != "pending":
            # Jarvis got far enough to know it went wrong, which means it got far enough
            # to say so. Repeating it from here would tell him nothing he does not have.
            log.info("the restart is already recorded as %s; leaving it be", record.state)
            return REPORTED
        if clock() >= deadline:
            break
        await sleep(poll_s)
        record = store.load()

    log.error("nothing confirmed the restart inside %ss", deadline_s)
    return await _alert(settings, store, record, twilio, probe)


async def _alert(
    settings: Settings,
    store: RestartStore,
    record: RestartRecord,
    twilio: Any,
    probe: Callable[[Settings], int | None],
) -> str:
    """Tell him the restart never landed, by text and then by phone."""
    if store.load() is None:
        # It confirmed itself in the moment between the last poll and this one.
        return CONFIRMED

    live = await asyncio.to_thread(probe, settings)
    up = live is not None
    errors = await asyncio.to_thread(errors_since, settings.data_dir, record.log_marks)
    body = _body(record, errors, up=up)
    log.error("%s", body)

    # Recorded before anything is sent, and before the service can come up late: a record
    # left `pending` would have a Jarvis that starts an hour from now ring him about a
    # restart he has already been told died.
    record.state = "failed"
    record.error = "restarted but never confirmed it" if up else "never came back"
    store.save(record)

    texted = await _text(twilio, settings, record, body)
    headline = STUCK_SPOKEN if up else DOWN_SPOKEN
    spoken = (SPOKEN_ALERT if texted else SPOKEN_ALERT_ONLY).format(headline=headline)
    called = await _call(twilio, settings, record, spoken)
    return ALERTED if texted or called else MUTE


def _body(record: RestartRecord, errors: LogErrors, *, up: bool) -> str:
    """The text message: everything worth knowing, in one message."""
    template = STUCK_SMS if up else DOWN_SMS
    # A log line ends however it ends; the sentence around it has to end in a full stop.
    last = errors.lines[-1].rstrip(".") if errors else ""
    return template.format(
        reason=f" ({record.reason})" if record.reason else "",
        age=format_duration(record.age_seconds()),
        task=f" It was loading the work from task {record.task_id}." if record.task_id else "",
        errors=f" Last error: {last}." if last else "",
        version=record.version or "an unknown version",
    )[:MAX_SMS_CHARS]


def _recipient(record: RestartRecord, settings: Settings) -> str | None:
    return record.number or settings.owner_number


async def _text(twilio: Any, settings: Settings, record: RestartRecord, body: str) -> bool:
    """Text him the detail. False when there was nothing to text it with."""
    to = _recipient(record, settings)
    if not to or twilio is None or not twilio.can_text:
        log.info("not texting about the restart; the call below is the whole alert")
        return False
    try:
        await twilio.send_sms(to, body)
    except Exception:
        log.exception("could not text about the restart that never came back")
        return False
    log.info("texted %s that the restart never landed", mask_number(to))
    return True


async def _call(twilio: Any, settings: Settings, record: RestartRecord, spoken: str) -> bool:
    """Ring him with the spoken alert. False when no call could be placed."""
    to = _recipient(record, settings)
    if not to or twilio is None or not twilio.configured:
        return False
    try:
        await twilio.place_call(to, twiml=say_twiml(spoken))
    except Exception:
        log.exception("could not call about the restart that never came back")
        return False
    log.info("called %s about the restart that never landed", mask_number(to))
    return True
