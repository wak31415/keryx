"""Restarting the service, and phoning back once it is up again.

A restart is the one job Keryx cannot see through inside one process: the process that
runs `systemctl restart` is the process the service manager then kills. The flow is
therefore split across that death, joined by one small file (`state_dir/restart.json`):

1. **Before** — `RestartCoordinator.request()` records what was asked for, by whom, on
   which number, and which version was running; then it hands the restart to the service
   manager. If somebody is on the phone it waits for them to hang up first: a restart
   drops every live call, so an immediate one would cut off the very person who asked.
2. **After** — `resume()`, called once from `keryx serve`, finds that record, waits until
   the phone server is really listening (the call-back is answered by our own
   `<Connect><Stream>`, so ringing before then rings into nothing), and calls the user
   with a one-line status summary as the new session's opening context.

The second half is written for a machine that may be broken, so nothing here raises. The
record carries an attempt count, so a crash loop cannot dial once per crash; a call that
cannot be placed falls back to a text; and a record that could not be delivered at all is
kept — marked `failed`, with the error on it, for `keryx restart --status` to read back —
rather than retried forever.

Two things the flow deliberately does not do. It never dials into a live session: if
somebody is already talking to Keryx they are told there and then instead, and if the
line is busy but nothing can be said into it, the confirmation goes out as a text. And it
cannot notice that the service never came back at all — nothing of ours is left running to
notice. `Restart=always` (systemd) / `KeepAlive` (launchd) is what covers that, and
`keryx restart` says so as it hands over.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from keryx.config import Settings
from keryx.continuity.transcripts import read_tail
from keryx.logging_util import mask_number
from keryx.notify.callback import (
    CALLBACK_TOKEN_TTL_S,
    HISTORY_PREAMBLE,
    MAX_REQUEST_CHARS,
    no_trailing_stop,
)
from keryx.notify.deliver import announce_to_live_sessions, safe_send_sms
from keryx.notify.twilio_out import say_twiml, stream_twiml
from keryx.restart.logscan import errors_since, marks
from keryx.restart.service import (
    ServiceTarget,
    WatchPlan,
    resolve_target,
    spawn_watchdog,
    watch_command,
)
from keryx.restart.store import RECORD_NAME, RestartRecord, RestartStore, format_duration
from keryx.restart.version import (
    loaded_version,
    startup_log_marks,
)
from keryx.session import SessionRegistry
from keryx.stream_tokens import StreamTokenStore, outbound_extra
from keryx.tasks.models import Task, TaskStatus
from keryx.tasks.store import TaskStore
from keryx.trust import TrustLevel

log = logging.getLogger("keryx.restart")

#: How many times a pending record may be picked up before it is abandoned. A service
#: that crash-loops must ring once, not once per crash.
MAX_CALLBACK_ATTEMPTS = 2
#: How often the deferred restart and the call-back look for a clear line.
QUIET_POLL_S = 2.0
#: How long a restart waits for a call to end before giving up on itself. Longer than
#: `max_call_seconds`, so a normal call always gets its restart afterwards.
DEFER_TIMEOUT_S = 2400.0
#: How long the call-back waits for a session that is on its way out.
QUIET_TIMEOUT_S = 120.0
#: How long we wait to be killed after the service manager accepted the restart. Being
#: alive after this means the restart did not happen, whatever the command's exit code.
EXEC_CONFIRM_S = 20.0
#: How many rows the task counts in the summary look at.
TASK_SCAN_LIMIT = 50
#: What a live session hears instead of a call — nobody is rung mid-conversation.
RESTART_ANNOUNCEMENT = "The restart is done and you are back up: {status}."
#: The opening context of the call-back itself: a confirmation, not a report.
RESTART_CONTEXT = (
    "You are calling the user back because you — the service behind this call — have just "
    "restarted and are running again. They asked for the restart "
    "{when}{reason}{change}, and this call is the confirmation. Status: {status}. In your "
    "first turn, greet them and tell them in one or two sentences whether it worked — do "
    "not stop after the greeting to wait for them. If the status mentions errors in the "
    "log, or says the checkout did not change, that is the headline: say plainly that the "
    "update may not have taken, say what the error was, and offer to put Claude on it. "
    "Otherwise say it went through, mention anything else in the status they would want to "
    "know, and ask if they need anything else. Keep it short: they asked for a restart, not "
    "a report. This is a new call: they may have to give the PIN again before you can start "
    "more work."
)
#: The clause that names the work a restart was loading, for the context above.
LOADING_TASK = ", to load the work from task {task_id}"
#: The other opening context: a restart the *work itself* asked for, where the call is
#: both "here is what came of it" and "and here is whether it is running". Two calls a
#: minute apart about the same piece of work is what this exists to avoid.
RESTART_WITH_TASK_CONTEXT = (
    "You are calling the user back about task {task_id}, which they asked you for earlier and "
    "which has now finished — and about the restart it needed, because the work changed "
    "your own code and you have just restarted to load it. What they asked for: "
    "{request}. Result: {detail}. Restart: {status}.{history} In your first turn — do not stop "
    "after the greeting to wait for them — greet them, remind them in a few words what this "
    "is about, tell them what came of the work, and then say whether the change is actually "
    "running. If the restart line mentions errors in the log, or says the checkout did not "
    "change, that is the headline: say plainly that the update may not have taken, say what "
    "the error was, and offer to put Claude on it. Call mark_reported for task {task_id} once "
    "you have told them. Keep it short. This is a new call: they may have to give the PIN "
    "again before you can start more work."
)
#: The same confirmation as a text, when no call can be placed.
RESTART_SMS = "Keryx restarted and is back up: {status}"
FAILED_SMS = "Keryx tried to restart and it did not go through: {error}"
#: The same again as a plain spoken call, when nothing can be texted. Only the headline:
#: `keryx restart --status` has the rest.
FAILED_SPOKEN = (
    "This is a Keryx alert. The restart did not go through, and Keryx is still running "
    "the old version. Check the machine when you can."
)

#: What the model is told to say when a restart has to wait for the call to end.
DEFERRED_MESSAGE = (
    "Tell them you will restart as soon as this call ends and anything already running has "
    "finished — a restart drops the call and kills every task with it — and that you will "
    "ring them straight back when you are up again."
)
RESTARTING_MESSAGE = (
    "Tell them you are restarting now, that this call is about to drop, and that you will "
    "ring them back when you are up again."
)
NO_CALLBACK_MESSAGE = (
    "Tell them you are restarting now and that this call is about to drop. Warn them that "
    "you cannot ring them back afterwards, so they should call in to check."
)
UNSUPPORTED_MESSAGE = (
    "Tell them, in one sentence, that you cannot restart yourself because this copy of "
    "you was not started by a service manager, so nothing would start it again."
)
ALREADY_PENDING_MESSAGE = "Tell them a restart is already scheduled for when this call ends."

# --- the coordinator --------------------------------------------------------


class RestartCoordinator:
    """Both halves of a restart: asking for one, and confirming it afterwards.

    `twilio_out`, `sessions` and `stream_tokens` are the same objects the Notifier uses —
    a call-back is a call-back, whatever it is about. `spawn` and `sleep` are injectable
    so the tests never start a process or wait on a clock.
    """

    def __init__(
        self,
        settings: Settings,
        sessions: SessionRegistry,
        twilio_out: Any | None = None,
        stream_tokens: StreamTokenStore | None = None,
        task_store: TaskStore | None = None,
        *,
        store: RestartStore | None = None,
        spawn: Callable[[Sequence[str]], Awaitable[Any]] | None = None,
        spawn_watch: Callable[["WatchPlan", Settings], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._twilio = twilio_out
        self._stream_tokens = stream_tokens
        self._tasks = task_store
        self._store = store or RestartStore(settings.state_dir / RECORD_NAME)
        self._spawn = spawn or _spawn_detached
        self._spawn_watch = spawn_watch or spawn_watchdog
        self._sleep = sleep
        self._deferred: asyncio.Task | None = None

    @property
    def store(self) -> RestartStore:
        return self._store

    # --- (1) asking for the restart ----------------------------------------

    async def request(
        self,
        *,
        reason: str = "",
        number: str | None = None,
        origin_channel: str = "local",
        origin_session_id: str | None = None,
        task_id: int | None = None,
        force: bool = False,
    ) -> dict:
        """Schedule a restart of this service. Never raises; the status says what happened.

        `{"status": …}` is one of `restarting` (the service manager has it), `deferred` (a
        call is live; it goes ahead when the line clears), `already_pending`, `unsupported`
        (nothing supervises this process) or `failed`. `message` is what the voice model
        should say — the caller is usually about to be cut off by their own request.

        `task_id` is the work this restart exists to load, when it exists to load any. It
        turns the confirmation from "the process came back" into "the change you asked for
        is running", and it is what licenses the version check in `status_summary`.
        """
        target = resolve_target(self._settings)
        if target is None:
            log.warning("refusing a restart: this process is not running under a service")
            return {"status": "unsupported", "message": UNSUPPORTED_MESSAGE}
        if self._deferred is not None and not self._deferred.done():
            return {"status": "already_pending", "message": ALREADY_PENDING_MESSAGE}

        callback_number = number or self._settings.owner_number
        record = RestartRecord(
            requested_at=datetime.now(UTC).isoformat(),
            reason=reason.strip(),
            number=callback_number,
            origin_channel=origin_channel,
            origin_session_id=origin_session_id,
            target=target.describe(),
            version=await asyncio.to_thread(loaded_version, self._settings.state_dir),
            task_id=task_id,
            log_marks=await asyncio.to_thread(marks, self._settings.state_dir),
        )
        if not self._store.save(record):
            # Without the record the new process has no idea it should call anyone, and a
            # restart that goes quiet is worse than one that does not happen.
            return {"status": "failed", "message": "Tell them the restart could not be set up."}

        message = RESTARTING_MESSAGE if self._can_call_back(record) else NO_CALLBACK_MESSAGE
        if await self._blocker() is not None and not force:
            log.info("holding the restart until the line is clear")
            self._deferred = asyncio.create_task(self._restart_when_quiet(target), name="restart")
            return {"status": "deferred", "message": DEFERRED_MESSAGE, "target": target.describe()}

        self._deferred = asyncio.create_task(self._execute(target), name="restart")
        return {"status": "restarting", "message": message, "target": target.describe()}

    def _can_call_back(self, record: RestartRecord) -> bool:
        """True if the pieces of an outbound call are all present."""
        return bool(
            record.number
            and self._settings.public_host
            and self._twilio is not None
            and self._twilio.configured
        )

    async def _restart_when_quiet(self, target: ServiceTarget) -> None:
        """Wait for the machine to be idle, then restart. Give up rather than cut work off."""
        if not await self._wait_until_idle(DEFER_TIMEOUT_S):
            self._store.clear()
            return
        await self._execute(target)

    async def _wait_until_idle(self, timeout: float) -> bool:
        """Poll until no call is live *and* no task is running; False if `timeout` passes.

        Both, because a restart kills every subagent it finds and nothing resumes them: the
        task that is running is work they asked for minutes ago, and the restart would end it
        somewhere in the middle with no report. Waiting for only the line to clear made the
        moment a call ends — which is exactly when the memory update is dispatched — the
        most dangerous moment to restart in.
        """
        waited = 0.0
        while (blocker := await self._blocker()) is not None:
            if waited >= timeout:
                log.warning("abandoning the restart: %s for %ss", blocker, timeout)
                return False
            await self._sleep(QUIET_POLL_S)
            waited += QUIET_POLL_S
        return True

    async def _blocker(self) -> str | None:
        """What a restart would interrupt right now, or None when it would interrupt nothing."""
        if self._sessions.live():
            return "a session has been live"
        running = await self._running_tasks()
        if running:
            return f"{running} task(s) have been running"
        return None

    async def _running_tasks(self) -> int:
        """How many subagents a restart would kill. Nought when the store cannot be read —
        a query that is failing must not hold a restart off forever."""
        if self._tasks is None:
            return 0
        try:
            return len(await self._tasks.list(status=TaskStatus.RUNNING, limit=TASK_SCAN_LIMIT))
        except Exception:
            log.exception("could not count the tasks a restart would interrupt")
            return 0

    async def _wait_for_quiet(self, timeout: float) -> bool:
        """Poll until no session is live; False if `timeout` passes first.

        The *call-back*'s idea of quiet, which is only about the line: a confirmation that
        waited for the task queue to drain would be a confirmation they never got.
        """
        waited = 0.0
        while self._sessions.live():
            if waited >= timeout:
                return False
            await self._sleep(QUIET_POLL_S)
            waited += QUIET_POLL_S
        return True

    async def _execute(self, target: ServiceTarget) -> None:
        """Hand the restart to the service manager; we expect to be killed doing it."""
        await self._arm_watchdog(target)
        command = target.command()
        log.info("restarting through %s: %s", target.manager, " ".join(command))
        try:
            code = await self._spawn(command)
        except Exception as exc:  # a missing binary, a refused fork
            await self._failed(f"{type(exc).__name__}: {exc}")
            return
        if code and code > 0:
            await self._failed(f"{command[0]} exited {code}")
            return
        # A negative code is the client killed by a signal, and the signal is ours: the
        # restart it queued tore down the cgroup the client was still sitting in. That is
        # the restart working, not failing — reported as a failure it left `restart.json`
        # saying `systemctl exited -15` on a service that had in fact come back fine, and
        # nobody was told anything, because the process that would have said so was dead.
        if code:
            log.info("%s was killed handing the restart over, which is the restart", command[0])
        # systemd kills us as part of the restart, so anything past here is the command
        # having quietly done nothing — the one failure that would otherwise go unnoticed.
        await self._sleep(EXEC_CONFIRM_S)
        await self._failed(f"{target.describe()} accepted the restart but nothing happened")

    async def _arm_watchdog(self, target: ServiceTarget) -> None:
        """Start the process that notices a restart which never comes back. Never raises.

        Every other part of this file runs on one side of the death or the other. This is
        the only thing that runs *through* it, and so the only thing that can tell them the
        service is gone rather than merely late — `resume()` cannot report a process that
        never got far enough to run it.

        Armed here rather than in `request()` because a deferred restart waits for the line
        to clear, and a watchdog counting down from half an hour ago would give up before
        the restart it is watching had even happened.
        """
        record = self._store.load()
        if record is None:
            return
        record.watchdog = await asyncio.to_thread(self._arm, target)
        self._store.save(record)

    def _arm(self, target: ServiceTarget) -> str:
        """Start the watchdog; the string is what `keryx restart --status` reads back."""
        plan = watch_command(self._settings, target)
        if plan is None:
            log.warning("no systemd-run: a restart that does not come back will go unnoticed")
            return "not started: systemd-run is not on PATH, so nothing outlives the restart"
        try:
            pid = self._spawn_watch(plan, self._settings)
        except Exception as exc:
            log.exception("could not arm the restart watchdog")
            return f"not started: {type(exc).__name__}: {exc}"
        log.info("armed the restart watchdog as %s (pid %s)", plan.label, pid)
        return f"{plan.label} (pid {pid})"

    async def _failed(self, error: str) -> None:
        """Record a restart that did not go through, and tell them: said, texted or rung.

        The ring is a plain `<Say>`, like the watchdog's: the one thing it has to carry is
        that the restart failed, and `keryx restart --status` has the rest. It never rings
        into a live call — whoever is on the line could not be told, so it is not the owner
        on the phone Keryx would be ringing.
        """
        log.error("the restart failed: %s", error)
        record = self._store.load() or RestartRecord()
        record.state = "failed"
        record.error = error
        self._store.save(record)
        spoken = f"The restart did not go through: {error}."
        if await self._announce(spoken):
            return
        if await self._send_sms(record.number, FAILED_SMS.format(error=error)):
            return
        if self._sessions.live():
            log.error("a call is live, so the failed restart is only in the log")
            return
        await self._ring(record.number, FAILED_SPOKEN)

    # --- (2) confirming it afterwards --------------------------------------

    async def resume(
        self,
        *,
        wait_ready: Callable[[], Awaitable[bool]] | None = None,
        wakeword: bool = False,
    ) -> None:
        """Deliver the confirmation for a pending restart, if there is one. Never raises.

        Called once per `keryx serve`. `wait_ready` returns True when the phone server is
        listening — without it there is nothing for the call-back's media stream to connect
        to, so the confirmation goes out as a text instead.
        """
        try:
            await self._resume(wait_ready=wait_ready, wakeword=wakeword)
        except Exception:
            log.exception("the restart call-back failed")

    async def _resume(
        self,
        *,
        wait_ready: Callable[[], Awaitable[bool]] | None,
        wakeword: bool,
    ) -> None:
        record = self._store.load()
        if record is None or record.state != "pending":
            return
        if record.attempts >= MAX_CALLBACK_ATTEMPTS:
            log.error(
                "giving up on the restart call-back after %s attempts: the service is not "
                "staying up long enough to place it",
                record.attempts,
            )
            record.state = "failed"
            record.error = "the service restarted repeatedly before it could call back"
            self._store.save(record)
            await self._send_sms(record.number, FAILED_SMS.format(error=record.error))
            return

        # Counted before the call, not after: a process that dies mid-dial must not come
        # back to a record that looks untouched and ring again, and again.
        record.attempts += 1
        self._store.save(record)

        phone_up = bool(wait_ready and await wait_ready())
        status = await self.status_summary(record, phone_up=phone_up, wakeword=wakeword)
        log.info("restart confirmed: %s", status)
        if record.quiet:
            self._store.clear()
            return
        await self._deliver(record, status, phone_up=phone_up)

    async def status_summary(
        self,
        record: RestartRecord,
        *,
        phone_up: bool,
        wakeword: bool = False,
        now: datetime | None = None,
    ) -> str:
        """The one line the call-back leads with: did it work, and what is it running.

        Ordered for somebody who is about to hear it read out: how long it was down, then
        anything that went wrong, then what it is running and what it is listening on.
        Errors come second because they are the answer to the only question worth asking
        about a restart that was loading a change — "did the change work" — and burying
        them behind three clauses of housekeeping is how they get skipped.
        """
        parts = [f"back up after {format_duration(record.age_seconds(now))} down"]

        # This process's own mark in preference to the record's: see `mark_startup_logs`
        # for why the difference matters. The record's is the fallback for a service too
        # old to have stamped one, which is no worse than what there was before.
        since = startup_log_marks(self._settings.state_dir) or record.log_marks
        errors = await asyncio.to_thread(errors_since, self._settings.state_dir, since)
        if errors:
            parts.append(f"but {errors.spoken()}")

        version = await asyncio.to_thread(loaded_version, self._settings.state_dir)
        if version and record.version and version != record.version:
            parts.append(f"now on {version}, was {record.version}")
        elif version:
            parts.append(f"still on {version}")
        if record.task_id is not None and version and version == record.version:
            # A restart asked for in order to load a change, running the same checkout it
            # was running before, has loaded nothing. Worth saying: the alternative is a
            # confident "all done" over work that never reached the disk.
            parts.append(
                f"the checkout did not change, so the work from task {record.task_id} may "
                "not have landed"
            )

        channels = [name for name, up in (("phone", phone_up), ("wake word", wakeword)) if up]
        parts.append(f"{' and '.join(channels)} listening" if channels else "no channel listening")

        interrupted, queued = await self._task_counts()
        if interrupted:
            parts.append(f"{interrupted} task(s) were interrupted and will not resume")
        if queued:
            # Queued means never started, so these are picked back up rather than lost —
            # `TaskManager.resume_queued`, which `keryx serve` runs after this call.
            parts.append(f"{queued} task(s) never started and are being picked back up")
        if not interrupted and not queued:
            parts.append("no tasks were lost")
        return "; ".join(parts)

    async def _task_counts(self) -> tuple[int, int]:
        """(interrupted, queued): rows the old process left behind. Nothing resumes them."""
        if self._tasks is None:
            return 0, 0
        try:
            running = await self._tasks.list(status=TaskStatus.RUNNING, limit=TASK_SCAN_LIMIT)
            queued = await self._tasks.list(status=TaskStatus.QUEUED, limit=TASK_SCAN_LIMIT)
        except Exception:
            log.exception("could not count the tasks the restart interrupted")
            return 0, 0
        return len(running), len(queued)

    async def _deliver(self, record: RestartRecord, status: str, *, phone_up: bool) -> None:
        """Say it into a live session, else call, else text. Never rings into a live call."""
        if await self._announce(RESTART_ANNOUNCEMENT.format(status=status)):
            self._store.clear()
            return
        if self._sessions.live() and not await self._wait_for_quiet(QUIET_TIMEOUT_S):
            log.info("a session is live: texting the restart confirmation instead of calling")
            await self._finish(record, RESTART_SMS.format(status=status), "the line was busy")
            return

        blocked = self._call_blocker(record, phone_up=phone_up)
        if blocked is not None:
            await self._finish(record, RESTART_SMS.format(status=status), blocked)
            return
        try:
            await self._place_call(record, status)
        except Exception as exc:
            log.exception("could not call back about the restart")
            body = RESTART_SMS.format(status=status)
            await self._finish(record, body, f"{type(exc).__name__}: {exc}")
            return
        self._store.clear()

    def _call_blocker(self, record: RestartRecord, *, phone_up: bool) -> str | None:
        """Why no call can be placed, or None when one can."""
        if not record.number:
            return "there was no number to call back on"
        if not phone_up:
            return "the phone server is not listening, so a call-back would have nowhere to land"
        if self._twilio is None or not self._twilio.configured:
            return "Twilio is not configured"
        if not self._settings.public_host:
            return "there is no public host for the media stream"
        if self._stream_tokens is None:
            return "there is no stream-token store"
        return None

    async def _place_call(self, record: RestartRecord, status: str) -> None:
        """Ring the user with a session that already knows the restart worked."""
        host = self._settings.public_host
        # `is not None` on the token store: an empty one is falsy (it counts its tokens).
        assert host and record.number and self._stream_tokens is not None  # _call_blocker
        context = await self._opening_context(record, status)
        token = self._stream_tokens.issue(
            caller=record.number,
            # `task_id` lets the call stamp that task reported before the PIN: it opens by
            # saying the result. `outbound_extra` says Keryx dialled it and what it
            # dialled, which opens the session at `POSSESSION` on the owner's own number.
            extra=outbound_extra(
                record.number, opening_context=context, restart=True, task_id=record.task_id
            ),
            ttl_s=CALLBACK_TOKEN_TTL_S,
        )
        twiml = stream_twiml(host, {"token": token, "caller": record.number})
        await self._twilio.place_call(
            record.number, twiml=twiml, status_callback=f"https://{host}/twilio/status"
        )
        log.info("called %s back to confirm the restart", mask_number(record.number))

    async def _opening_context(self, record: RestartRecord, status: str) -> str:
        """What the call-back opens knowing: the restart, and the work that asked for it."""
        task = await self._task(record.task_id)
        if task is None:
            reason = f", because {record.reason}" if record.reason else ""
            return RESTART_CONTEXT.format(
                when=f"{format_duration(record.age_seconds())} ago",
                reason=reason,
                change=LOADING_TASK.format(task_id=record.task_id) if record.task_id else "",
                status=status,
            )
        request = task.description
        if len(request) > MAX_REQUEST_CHARS:
            request = request[: MAX_REQUEST_CHARS - 1].rstrip() + "…"
        history = read_tail(
            self._settings.data_dir, record.origin_session_id or "", pin=self._settings.pin
        )
        return RESTART_WITH_TASK_CONTEXT.format(
            task_id=task.id,
            request=request,
            detail=no_trailing_stop(task.summary or "it finished without a summary"),
            status=status,
            history=HISTORY_PREAMBLE.format(history=history) if history else "",
        )

    async def _task(self, task_id: int | None) -> Task | None:
        """The task this restart is loading, if it is loading one and it can be read."""
        if task_id is None or self._tasks is None:
            return None
        try:
            return await self._tasks.get(task_id)
        except Exception:
            log.exception("could not read task %s to lead the call-back with it", task_id)
            return None

    async def _finish(self, record: RestartRecord, body: str, why: str) -> None:
        """The fallback: text the confirmation, and keep the record if even that failed."""
        log.warning("not calling back about the restart: %s", why)
        if await self._send_sms(record.number, f"{body} (no call: {why})"):
            self._store.clear()
            return
        record.state = "failed"
        record.error = f"could not confirm the restart: {why}"
        self._store.save(record)

    # --- outbound plumbing --------------------------------------------------

    async def _announce(self, text: str) -> bool:
        """Speak `text` into every live session that could be the owner; True if one took it.

        `POSSESSION`, not `FULL`: the confirmation names the checkout and what broke, which
        is not for a stranger, but a call Keryx placed to the owner's own number is not one
        — and the alternative to saying it there is ringing a phone they are already on.
        """
        return (
            await announce_to_live_sessions(self._sessions, text, needs=TrustLevel.POSSESSION)
        ).heard

    async def _send_sms(self, number: str | None, body: str) -> bool:
        """Text `body`, if there is anything to text it with. Never raises."""
        to = number or self._settings.owner_number
        if await safe_send_sms(self._twilio, to, body):
            return True
        log.warning("could not text about the restart (%s)", body)
        return False

    async def _ring(self, number: str | None, spoken: str) -> None:
        """Ring them with `spoken` and nothing behind it. Never raises."""
        to = number or self._settings.owner_number
        if not to or self._twilio is None or not self._twilio.configured:
            log.error("no call can be placed either; the failed restart is only in the log")
            return
        try:
            await self._twilio.place_call(to, twiml=say_twiml(spoken))
        except Exception:
            log.exception("could not call about the failed restart; it is only in the log")
            return
        log.info("called %s about the failed restart", mask_number(to))

    async def shutdown(self) -> None:
        """Drop a deferred restart that never got its quiet moment. Idempotent."""
        if self._deferred is None:
            return
        self._deferred.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._deferred
        self._deferred = None


async def _spawn_detached(command: Sequence[str]) -> int | None:
    """Run the restart command in its own session and wait for its exit code.

    `start_new_session` keeps the command off our controlling terminal; it does *not* take
    it out of the service's cgroup, and it does not need to. `systemctl restart` only has
    to reach the manager — the job it queues there outlives the client that queued it, and
    us with it.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    return await process.wait()
