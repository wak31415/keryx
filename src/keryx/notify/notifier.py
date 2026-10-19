"""Getting a finished task to the user, by whatever route is open.

Three stages, in order, on every `TaskCompleted` / `TaskFailed`:

1. **Announce** it into every live session, so somebody already talking to Keryx simply
   hears the result — except the session holding the line for this very task inside
   `dispatch_task`, which gets it as the tool result instead (see `InlineWaits`).
2. **Text** it, unless it was already delivered: a live *phone* session announced it, or
   somebody was holding the line for it. Otherwise a local (wake-word) session hearing it
   does not count: they may well have walked away from the Mac.
3. **Call back**, if they asked to be called and have not had it already. The call
   carries a fresh single-use stream token whose `extra` tells the new session why it
   opened, so Keryx leads with the result instead of "hello?".

There is a fourth route, for the one task that cannot be delivered by the first three: a
task that changed Keryx's own code. Loading it means restarting, restarting means dropping
whatever call would have carried the result, and confirming the restart means ringing them
anyway. So a task whose subagent asked for a restart hands its call-back over to the
restart's own confirmation, which then carries both — what the work came to, and whether it
is actually running (`keryx.restart`). The announcement and the text still go out first:
they cost nothing and they survive a restart that does not come back.

Every stage is independently guarded: a Twilio outage during the text must not cost the
call-back, and nothing here may ever raise into the event bus.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from keryx.config import Settings
from keryx.continuity.transcripts import read_tail
from keryx.events import EventBus, TaskCompleted, TaskFailed
from keryx.inline_waits import InlineWaits
from keryx.notify.callback import (
    CALLBACK_TOKEN_TTL_S,
    HISTORY_PREAMBLE,
    MAX_REQUEST_CHARS,
    no_trailing_stop,
)
from keryx.notify.deliver import announce_to_live_sessions, safe_send_sms
from keryx.notify.reports import report_token
from keryx.notify.twilio_out import TwilioOut, stream_twiml
from keryx.session import SessionRegistry
from keryx.stream_tokens import StreamTokenStore, outbound_extra
from keryx.tasks.models import Task
from keryx.tasks.store import TaskStore
from keryx.trust import TrustLevel

if TYPE_CHECKING:  # pragma: no cover - `restart` imports this module for its own reasons
    from keryx.restart.coordinator import RestartCoordinator

log = logging.getLogger("keryx.notify.notifier")

SMS_BODY_LIMIT = 1200  # characters of summary; the report link is appended after it

DONE_TEXT = "Task {task_id} finished: {detail}"
FAILED_TEXT = "Task {task_id} failed: {detail}"
#: The call-back opens a fresh session — a phone call cannot resume the one that asked for
#: it — so the context has to carry what they asked for as well as what came of it, or they
#: answer the phone to an answer with no question attached.
DONE_CONTEXT = (
    "You are calling the user back about task {task_id}, which they asked you for earlier "
    "on the phone and which has now finished. What they asked for: {request}. "
    "Result: {detail}.{history} In your first turn, greet them, say in a sentence or two what "
    "this is about and what came of it, then ask if they need anything else — do not stop "
    "after the greeting to wait for them: the result is why you rang. Say it once: no second "
    "greeting, and do not put the same result again in other words. This is a new call: they "
    "may have to give the PIN again before you can start more work."
)
FAILED_CONTEXT = (
    "You are calling the user back about task {task_id}, which they asked you for earlier "
    "on the phone and which has failed. What they asked for: {request}. "
    "Error: {detail}.{history} In your first turn, greet them, say in a sentence or two what "
    "this is about and what went wrong, then ask if they need anything else — do not stop "
    "after the greeting to wait for them: the failure is why you rang. Say it once: no second "
    "greeting, and do not put the same explanation again in other words. This is a new call: "
    "they may have to give the PIN again before you can start more work."
)
#: The note the earlier session left for this call, if it left one.
NOTE_PREAMBLE = " Where you left off: {note}."
#: Why the restart a finished task asks for is happening, read back on the confirmation.
RESTART_REASON = "to load what task {task_id} changed"
#: The `request()` outcomes that mean a restart really is coming, and that its confirmation
#: is therefore going to ring them. Anything else (`unsupported`, `failed`) is not a
#: call-back, so the ordinary one still has to go out.
RESTART_ARMED = frozenset({"restarting", "deferred", "already_pending"})


class Notifier:
    """Subscribes to task completion and turns it into announcements, texts and calls."""

    def __init__(
        self,
        bus: EventBus,
        store: TaskStore,
        sessions: SessionRegistry,
        twilio_out: TwilioOut,
        settings: Settings,
        stream_tokens: StreamTokenStore,
        inline_waits: InlineWaits,
        restart: "RestartCoordinator | None" = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._sessions = sessions
        self._twilio = twilio_out
        self._settings = settings
        self._stream_tokens = stream_tokens
        self._inline_waits = inline_waits
        self._restart = restart
        self._removals: list[Callable[[], None]] = []

    def start(self) -> None:
        """Subscribe to the completion events. Idempotent."""
        if self._removals:
            return
        self._removals = [
            self._bus.subscribe(TaskCompleted, self._on_task_event),
            self._bus.subscribe(TaskFailed, self._on_task_event),
        ]

    def stop(self) -> None:
        """Unsubscribe from the bus. Idempotent."""
        for remove in self._removals:
            remove()
        self._removals.clear()

    def report_url(self, task_id: int) -> str | None:
        """The tokenized report link, or None when there is no public host to serve it."""
        host = self._settings.public_host
        if not host:
            return None
        token = report_token(task_id, self._settings.report_secret_value())
        return f"https://{host}/reports/{task_id}?t={token}"

    # --- the handler -------------------------------------------------------

    async def _on_task_event(self, event: TaskCompleted | TaskFailed) -> None:
        """Announce, then text, then call back. Never raises."""
        failed = isinstance(event, TaskFailed)
        detail = event.error if failed else event.summary
        try:
            task = await self._store.get(event.task_id)
        except Exception:
            log.exception("could not load task %s to notify about it", event.task_id)
            return
        if task is None:
            log.warning("no task %s to notify about", event.task_id)
            return
        if task.internal:
            # Housekeeping Keryx asked for itself (the per-call memory update). They never
            # requested it, so announcing it into a live call, texting it, or ringing them
            # about it would all be Keryx interrupting them to talk about Keryx.
            log.debug("task %s is internal; nothing to notify about", task.id)
            return

        text = (FAILED_TEXT if failed else DONE_TEXT).format(
            task_id=task.id, detail=detail
        )
        delivered = await self._announce(task, text)
        await self._send_sms(task, text, delivered=delivered)
        if not failed and await self._arm_restart(task):
            # The restart's confirmation call is this task's call-back, and it is a better
            # one: it can say whether the change they asked for is actually running. Two
            # calls a minute apart about the same piece of work would be the alternative.
            return
        await self._call_back(task, detail, failed=failed, delivered=delivered)

    # --- (1) live sessions -------------------------------------------------

    async def _announce(self, task: Task, text: str) -> bool:
        """Speak `text` into every live session; True if the owner has it either way.

        A session holding the line for this task is skipped and counts as delivered: the
        `dispatch_task` tool result is about to say the same thing, and hearing it twice
        is worse than not hearing it here at all.
        """
        def holding_the_line(session) -> bool:
            if (session.session_id, task.id) not in self._inline_waits:
                return False
            log.info("session %s is holding the line for task %s", session.session_id, task.id)
            return True

        announced = await announce_to_live_sessions(
            # News: a result the owner is waiting on, which is not gated on the PIN
            # (`BRIEFING_BEFORE_PIN`). A call that has proved nothing may hear it and still
            # does not count as delivery, so the text and the call-back still go out.
            self._sessions,
            text,
            needs=TrustLevel.NONE,
            skip=holding_the_line,
            task_id=task.id,
        )
        if announced.heard:
            try:
                await self._store.update(task.id, announced=True)
            except Exception:
                log.exception("could not record that task %s was announced", task.id)
        return announced.delivered

    # --- (2) the text ------------------------------------------------------

    async def _send_sms(self, task: Task, text: str, *, delivered: bool) -> None:
        """Text the summary and the report link, unless they have just heard it.

        With texting off this stage simply does not happen, and the result reaches them by
        one of the routes that do: spoken into a live session, the call-back, or — if they
        were not there for either — the digest at the top of their next call, which is what
        `reported_at` exists to keep honest.
        """
        if delivered:
            return
        try:
            to = self._sms_recipient(task)
            if not await safe_send_sms(self._twilio, to, self._sms_body(task, text)):
                log.info("did not text about task %s", task.id)
                return
            await self._store.update(task.id, sms_sent=True)
        except Exception:
            log.exception("could not text the result of task %s", task.id)

    def _sms_recipient(self, task: Task) -> str | None:
        """Whoever asked for the task on the phone, else the owner's number."""
        if task.origin_channel == "phone" and task.origin_caller:
            return task.origin_caller
        return self._settings.owner_number

    def _sms_body(self, task: Task, text: str) -> str:
        """The summary, trimmed to something sendable, plus the report link if there is one.

        A task that died before its report was written (the exception path in the task
        manager) gets no link rather than one that would 404.
        """
        body = text[:SMS_BODY_LIMIT]
        url = self.report_url(task.id) if task.report_path else None
        return f"{body}\n{url}" if url else body

    # --- (4) hand the call-back to a restart --------------------------------

    async def _arm_restart(self, task: Task) -> bool:
        """Ask for the restart this task needs; True when it is on and owns the call-back.

        False for every ordinary task, and for one that asked on a machine where nothing
        supervises the service — there the restart is refused, and refusing to restart must
        not also swallow the result they were waiting for.
        """
        if not task.needs_restart or self._restart is None:
            return False
        try:
            answer = await self._restart.request(
                reason=RESTART_REASON.format(task_id=task.id),
                number=task.callback_number or self._sms_recipient(task),
                origin_channel=task.origin_channel,
                origin_session_id=task.origin_session_id,
                task_id=task.id,
            )
        except Exception:
            log.exception("could not arm the restart task %s asked for", task.id)
            return False
        status = answer.get("status")
        log.info("task %s asked for a restart: %s", task.id, status)
        if status not in RESTART_ARMED:
            return False
        # Only once: a follow-up on the same task must not restart a second time, and the
        # confirmation is the call-back, so nothing else should dial about this task.
        try:
            await self._store.update(task.id, needs_restart=False, callback_requested=False)
        except Exception:
            log.exception("could not clear the restart request on task %s", task.id)
        return True

    # --- (3) the call-back -------------------------------------------------

    def _previous_call(self, task: Task) -> str:
        """What the session that asked for this call-back had said, if anything survives."""
        parts = []
        if task.callback_note:
            parts.append(NOTE_PREAMBLE.format(note=task.callback_note))
        history = read_tail(
            self._settings.data_dir, task.origin_session_id or "", pin=self._settings.pin
        )
        if history:
            parts.append(HISTORY_PREAMBLE.format(history=history))
        return "".join(parts)


    async def _call_back(
        self, task: Task, detail: str, *, failed: bool, delivered: bool
    ) -> None:
        """Ring the user back with a session that already knows what happened."""
        host = self._settings.public_host
        if delivered or not task.callback_requested or not self._twilio.configured:
            return
        if not task.callback_number or not host:
            log.info("cannot call back about task %s: no number or no public host", task.id)
            return
        try:
            request = task.description
            if len(request) > MAX_REQUEST_CHARS:
                request = request[: MAX_REQUEST_CHARS - 1].rstrip() + "…"
            context = (FAILED_CONTEXT if failed else DONE_CONTEXT).format(
                task_id=task.id,
                request=request,
                detail=no_trailing_stop(detail),
                history=self._previous_call(task),
            )
            token = self._stream_tokens.issue(
                caller=task.callback_number,
                # Keryx is dialling: the token says so, and says what it dialled, so the
                # session that answers opens at `POSSESSION` when that is the owner's own
                # number (`keryx.trust`). A number they named out loud is not.
                extra=outbound_extra(
                    task.callback_number, task_id=task.id, opening_context=context
                ),
                ttl_s=CALLBACK_TOKEN_TTL_S,
            )
            twiml = stream_twiml(
                host,
                {"token": token, "caller": task.callback_number, "task_id": str(task.id)},
            )
            await self._twilio.place_call(
                task.callback_number,
                twiml=twiml,
                status_callback=f"https://{host}/twilio/status",
            )
            # Only once: a follow-up on the same task must not dial again.
            await self._store.update(task.id, callback_requested=False)
        except Exception:
            log.exception("could not call back about task %s", task.id)
