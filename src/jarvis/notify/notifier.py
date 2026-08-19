"""Getting a finished task to the user, by whatever route is open (spec §3.3).

Three stages, in order, on every `TaskCompleted` / `TaskFailed`:

1. **Announce** it into every live session, so somebody already talking to Jarvis simply
   hears the result.
2. **Text** it, unless a live *phone* session already announced it — the person on the
   call has heard it, and a text about what they were just told is noise. A local
   (wake-word) session does not count: they may well have walked away from the Mac.
3. **Call back**, if they asked to be called and are not on the phone already. The call
   carries a fresh single-use stream token whose `extra` tells the new session why it
   opened, so Jarvis leads with the result instead of "hello?".

Every stage is independently guarded: a Twilio outage during the text must not cost the
call-back, and nothing here may ever raise into the event bus.
"""

import hashlib
import hmac
import logging
from collections.abc import Callable

from jarvis.config import Settings
from jarvis.events import EventBus, TaskCompleted, TaskFailed
from jarvis.notify.twilio_out import TwilioOut, stream_twiml
from jarvis.session import SessionRegistry
from jarvis.stream_tokens import StreamTokenStore
from jarvis.tasks.models import Task
from jarvis.tasks.store import TaskStore

log = logging.getLogger("jarvis.notify.notifier")

SMS_BODY_LIMIT = 1200  # characters of summary; the report link is appended after it
CALLBACK_TOKEN_TTL_S = 120.0  # Twilio has to ring and be answered inside this
TOKEN_HEX_CHARS = 32  # half a sha256, plenty against guessing and short enough for a URL

DONE_TEXT = "Task {task_id} ({kind}) finished: {detail}"
FAILED_TEXT = "Task {task_id} ({kind}) failed: {detail}"
DONE_CONTEXT = (
    "You are calling the user back because task {task_id} ({kind}) finished. "
    "Result: {detail}. Greet them, tell them the result briefly, then ask if they need "
    "anything else."
)
FAILED_CONTEXT = (
    "You are calling the user back because task {task_id} ({kind}) failed. "
    "Error: {detail}. Greet them, tell them what went wrong briefly, then ask if they "
    "need anything else."
)


def report_token(task_id: int, secret: str) -> str:
    """The unguessable half of a report link: HMAC-SHA256 of the id under `secret`."""
    digest = hmac.new(secret.encode(), str(task_id).encode(), hashlib.sha256).hexdigest()
    return digest[:TOKEN_HEX_CHARS]


def verify_report_token(task_id: int, token: str, secret: str) -> bool:
    """True if `token` is the report token for `task_id` (constant-time compare)."""
    return hmac.compare_digest(report_token(task_id, secret), token)


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
    ) -> None:
        self._bus = bus
        self._store = store
        self._sessions = sessions
        self._twilio = twilio_out
        self._settings = settings
        self._stream_tokens = stream_tokens
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

        text = (FAILED_TEXT if failed else DONE_TEXT).format(
            task_id=task.id, kind=task.kind, detail=detail
        )
        announced_by_phone = await self._announce(task, text)
        await self._send_sms(task, text, announced_by_phone=announced_by_phone)
        await self._call_back(task, detail, failed=failed, announced_by_phone=announced_by_phone)

    # --- (1) live sessions -------------------------------------------------

    async def _announce(self, task: Task, text: str) -> bool:
        """Speak `text` into every live session; True if a *phone* session took it."""
        heard = False
        by_phone = False
        try:
            for session in self._sessions.live():
                spoken = await session.announce(text)
                heard = heard or spoken
                by_phone = by_phone or (spoken and session.channel == "phone")
            if heard:
                await self._store.update(task.id, announced=True)
        except Exception:
            log.exception("could not announce task %s into the live sessions", task.id)
        return by_phone

    # --- (2) the text ------------------------------------------------------

    async def _send_sms(self, task: Task, text: str, *, announced_by_phone: bool) -> None:
        """Text the summary and the report link, unless they just heard it on the phone."""
        if announced_by_phone or not self._twilio.configured:
            return
        try:
            to = self._sms_recipient(task)
            if not to:
                log.info("no number to text about task %s", task.id)
                return
            await self._twilio.send_sms(to, self._sms_body(task, text))
            await self._store.update(task.id, sms_sent=True)
        except Exception:
            log.exception("could not text the result of task %s", task.id)

    def _sms_recipient(self, task: Task) -> str | None:
        """Whoever asked for the task on the phone, else the owner's number."""
        if task.origin_channel == "phone" and task.origin_caller:
            return task.origin_caller
        return self._settings.owner_number

    def _sms_body(self, task: Task, text: str) -> str:
        body = text[:SMS_BODY_LIMIT]
        url = self.report_url(task.id)
        return f"{body}\n{url}" if url else body

    # --- (3) the call-back -------------------------------------------------

    async def _call_back(
        self, task: Task, detail: str, *, failed: bool, announced_by_phone: bool
    ) -> None:
        """Ring the user back with a session that already knows what happened."""
        host = self._settings.public_host
        if announced_by_phone or not task.callback_requested or not self._twilio.configured:
            return
        if not task.callback_number or not host:
            log.info("cannot call back about task %s: no number or no public host", task.id)
            return
        try:
            context = (FAILED_CONTEXT if failed else DONE_CONTEXT).format(
                task_id=task.id, kind=task.kind, detail=detail
            )
            token = self._stream_tokens.issue(
                caller=task.callback_number,
                extra={"task_id": task.id, "opening_context": context},
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
