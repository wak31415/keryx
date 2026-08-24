"""Getting a finished task to the user, by whatever route is open (spec §3.3).

Three stages, in order, on every `TaskCompleted` / `TaskFailed`:

1. **Announce** it into every live session, so somebody already talking to Jarvis simply
   hears the result — except the session holding the line for this very task inside
   `dispatch_task`, which gets it as the tool result instead (see `InlineWaits`).
2. **Text** it, unless it was already delivered: a live *phone* session announced it, or
   somebody was holding the line for it. Otherwise a local (wake-word) session hearing it
   does not count: they may well have walked away from the Mac.
3. **Call back**, if they asked to be called and have not had it already. The call
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
from jarvis.inline_waits import InlineWaits
from jarvis.notify.twilio_out import TwilioOut, stream_twiml
from jarvis.session import SessionRegistry
from jarvis.stream_tokens import StreamTokenStore
from jarvis.tasks.models import Task
from jarvis.tasks.store import TaskStore

log = logging.getLogger("jarvis.notify.notifier")

SMS_BODY_LIMIT = 1200  # characters of summary; the report link is appended after it
CALLBACK_TOKEN_TTL_S = 120.0  # Twilio has to ring and be answered inside this
TOKEN_HEX_CHARS = 32  # half a sha256, plenty against guessing and short enough for a URL

DONE_TEXT = "Task {task_id} finished: {detail}"
FAILED_TEXT = "Task {task_id} failed: {detail}"
DONE_CONTEXT = (
    "You are calling the user back because task {task_id} finished. "
    "Result: {detail}. Greet them, tell them the result briefly, then ask if they need "
    "anything else."
)
FAILED_CONTEXT = (
    "You are calling the user back because task {task_id} failed. "
    "Error: {detail}. Greet them, tell them what went wrong briefly, then ask if they "
    "need anything else."
)


def report_token(task_id: int, secret: str) -> str:
    """The unguessable half of a report link: HMAC-SHA256 of the id under `secret`."""
    digest = hmac.new(secret.encode(), str(task_id).encode(), hashlib.sha256).hexdigest()
    return digest[:TOKEN_HEX_CHARS]


def verify_report_token(task_id: int, token: str, secret: str) -> bool:
    """True if `token` is the report token for `task_id` (constant-time compare).

    Compared as bytes: `hmac.compare_digest` refuses `str` operands with non-ASCII
    characters, and this one comes straight off a public query string.
    """
    expected = report_token(task_id, secret).encode()
    return hmac.compare_digest(expected, token.encode("utf-8", "surrogatepass"))


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
    ) -> None:
        self._bus = bus
        self._store = store
        self._sessions = sessions
        self._twilio = twilio_out
        self._settings = settings
        self._stream_tokens = stream_tokens
        self._inline_waits = inline_waits
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
            task_id=task.id, detail=detail
        )
        delivered = await self._announce(task, text)
        await self._send_sms(task, text, delivered=delivered)
        await self._call_back(task, detail, failed=failed, delivered=delivered)

    # --- (1) live sessions -------------------------------------------------

    async def _announce(self, task: Task, text: str) -> bool:
        """Speak `text` into every live session; True if the user has it either way.

        A session holding the line for this task is skipped and counts as delivered: the
        `dispatch_task` tool result is about to say the same thing, and hearing it twice
        is worse than not hearing it here at all.
        """
        heard = False
        delivered = False
        try:
            for session in self._sessions.live():
                if (session.session_id, task.id) in self._inline_waits:
                    log.info(
                        "session %s is holding the line for task %s",
                        session.session_id,
                        task.id,
                    )
                    heard = delivered = True
                    continue
                spoken = await session.announce(text)
                heard = heard or spoken
                delivered = delivered or (spoken and session.channel == "phone")
            if heard:
                await self._store.update(task.id, announced=True)
        except Exception:
            log.exception("could not announce task %s into the live sessions", task.id)
        return delivered

    # --- (2) the text ------------------------------------------------------

    async def _send_sms(self, task: Task, text: str, *, delivered: bool) -> None:
        """Text the summary and the report link, unless they have just heard it."""
        if delivered or not self._twilio.configured:
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
        """The summary, trimmed to something sendable, plus the report link if there is one.

        A task that died before its report was written (the exception path in the task
        manager) gets no link rather than one that would 404.
        """
        body = text[:SMS_BODY_LIMIT]
        url = self.report_url(task.id) if task.report_path else None
        return f"{body}\n{url}" if url else body

    # --- (3) the call-back -------------------------------------------------

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
            context = (FAILED_CONTEXT if failed else DONE_CONTEXT).format(
                task_id=task.id, detail=detail
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
