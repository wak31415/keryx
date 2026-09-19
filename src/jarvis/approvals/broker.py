"""The half of the approval bridge that lives inside Jarvis.

A Claude Code hook runs on the owner's machine whenever the CLI is about to put a prompt on
their screen. It connects to the Unix socket this module listens on and *blocks*. Nothing
else happens: the prompt is drawn as it always was, and if they answer at the keyboard the
hook's answer is thrown away (measured — the keyboard always wins). Only when the prompt
has sat unanswered for `APPROVAL_ESCALATE_SECONDS` does the broker do anything at all, and
what it does is ring them.

Four rules hold the whole design up:

- **A Unix socket, never an HTTP route.** `cloudflared` forwards the whole of port 8080 to
  the public internet, so a `/approvals` endpoint would be reachable by anyone. A socket at
  `data_dir/approvals.sock`, mode 0600, is unreachable through the tunnel by construction,
  and filesystem permissions are the right authorization for a thing whose only legitimate
  client is a process already running as them.
- **Failure is always "do nothing".** Every path that is not an explicit, PIN-gated,
  keypad-confirmed answer ends with the waiting hook being told nothing — which leaves the
  ordinary on-screen prompt exactly as it is today. Broker down, socket missing, Twilio
  broken, call unanswered, wrong digit, timeout: all the same outcome.
- **The keypad decides, never the transcription.** `arm()` only offers the menu; `digit()`
  is the one thing in this file that can approve anything. A television in the background
  cannot press a key, and neither can a mis-heard "yeah, sure".
- **Pending is a fact to be re-checked, never assumed.** The hook is *not* killed when they
  answer at the keyboard, so without the `resolve` path the broker would ring them about
  prompts they dealt with five minutes ago. Pending is re-checked before dialling and again
  before any verdict is applied.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from jarvis.approvals.models import ApprovalRequest, Kind, Outcome, Verdict, input_digest
from jarvis.approvals.policy import classify
from jarvis.config import Settings
from jarvis.notify.deliver import announce_to_live_sessions
from jarvis.notify.twilio_out import TwilioOut, stream_twiml
from jarvis.session import SessionRegistry
from jarvis.stream_tokens import StreamTokenStore, outbound_extra
from jarvis.trust import TrustLevel

log = logging.getLogger("jarvis.approvals.broker")

SOCKET_NAME = "approvals.sock"
STATE_DIR_NAME = "approvals"
AUDIT_NAME = "audit.jsonl"
#: Touched while anything is pending. The `PostToolUse` hook runs on *every* tool call, so
#: its first act is to stat this file and exit — one syscall in the overwhelmingly common
#: case where nothing is waiting.
MARKER_NAME = "PENDING"
#: Its presence turns the whole bridge off without touching settings or restarting Jarvis.
KILL_SWITCH_NAME = "DISABLED"

#: The `sun_path` limit for an `AF_UNIX` socket — 108 bytes on Linux, 104 on macOS, and
#: the *whole* path counts. `~/.jarvis/approvals.sock` is nowhere near it; a `DATA_DIR`
#: nested somewhere deep is, and the bare `OSError: AF_UNIX path too long` it produces
#: says nothing anyone can act on. Checked here so the log line names the fix instead.
MAX_SOCKET_PATH_BYTES = 100

PROTOCOL = 1
#: How long the broker waits for a client to say what it wants before dropping it.
CLIENT_READ_TIMEOUT_S = 10.0
#: Twilio has to ring and be answered inside this, same as the Notifier's call-back.
CALL_TOKEN_TTL_S = 120.0
#: How long an armed keypad confirmation stays armed. Long enough to read a menu out and
#: have them find the key; short enough that a digit pressed later cannot land on it.
ARM_TTL_S = 90.0

#: Hook events that mean "that prompt is not waiting any more".
RESOLVING_EVENTS = frozenset({"PostToolUse", "PermissionDenied", "Stop", "SessionEnd"})
#: The ones precise enough to name a single tool call rather than a whole session.
PRECISE_EVENTS = frozenset({"PostToolUse", "PermissionDenied"})

APPROVED_MESSAGE = (
    "Approved by the user by phone: Jarvis called them, they gave the PIN and confirmed on the "
    "keypad. This approval covers this one tool call and nothing else."
)
REJECTED_MESSAGE = (
    "Rejected by the user by phone (Jarvis, PIN-verified keypad confirmation). Do not retry "
    "it; ask them for another approach."
)
ANSWERED_MESSAGE = (
    "Answered by the user over the phone (Jarvis, PIN-verified keypad confirmation): {answer}."
)

#: Read to them at the top of the escalation call. It has to carry the whole protocol: the
#: session it opens is brand new and knows nothing about why it was opened.
CALL_CONTEXT = (
    "You are calling them because Claude Code has been waiting about {waited} for an answer on "
    "their screen and has not had one. That is the only reason for this call.\n\n"
    "{requests}\n\n"
    "Greet them in a few words, say Claude is waiting on them, and read the request back once, "
    "as it is written above — do not paraphrase it and do not embellish it. They have to give the "
    "PIN before you can answer anything for them. Then call answer_approval with the request "
    "number: it hands you back a keypad menu, which you read out. They decide with the keypad "
    "and only with the keypad — if they say yes out loud, thank them and still ask them to press "
    "the digit. Never guess which option they mean, never press one on their behalf, and if they "
    "would rather leave it, say so and end the call: it stays on their screen either way."
)
REQUEST_LINE = "Request {id}: {summary}. The options are: {menu}."
ANNOUNCE_TEXT = (
    "Claude Code has been waiting about {waited} for an answer on their screen. {line} Tell them "
    "that, read the request back once as written, and if they want to deal with it now call "
    "answer_approval with the request number and read out the keypad menu it gives you."
)

DIGIT_LEFT = (
    "[system] They pressed zero: request {id} is being left alone. It is still on their screen. "
    "Say so in a few words and move on."
)
DIGIT_APPLIED = (
    "[system] They pressed a key: request {id} is {outcome} and Claude has been told. Say so in "
    "a few words. Do not read the option back as if they had said it."
)
DIGIT_UNKNOWN = (
    "[system] That was not one of the options for request {id}. Read the menu out again: "
    "{menu}. Do not decide for them."
)
DIGIT_GONE = (
    "[system] Request {id} is not waiting any more — it was dealt with at the keyboard or the "
    "session ended. Tell them there is nothing to answer and move on."
)


class ApprovalBroker:
    """Pending prompts, the timers that escalate them, and the one keypad that answers them.

    `sessions`, `twilio_out` and `stream_tokens` are the same objects the Notifier uses, so
    an approval call is built exactly like a task call-back and needs no new public surface.
    `now` is injectable purely so tests do not have to wait out a rate-limit window.
    """

    def __init__(
        self,
        settings: Settings,
        sessions: SessionRegistry,
        twilio_out: TwilioOut,
        stream_tokens: StreamTokenStore,
        *,
        now=time.monotonic,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._twilio = twilio_out
        self._stream_tokens = stream_tokens
        self._now = now

        self._pending: dict[int, ApprovalRequest] = {}
        self._waiters: dict[int, asyncio.Future] = {}
        #: session_id -> (request_id, expires_at). One armed confirmation per call.
        self._armed: dict[str, tuple[int, float]] = {}
        self._dials: deque[float] = deque()
        self._dialled_at: float | None = None
        self._next_id = 1
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    # --- lifecycle ---------------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self._settings.data_dir / STATE_DIR_NAME

    @property
    def socket_path(self) -> Path:
        return self._settings.data_dir / SOCKET_NAME

    @property
    def disabled(self) -> bool:
        """True when the kill switch file is there, checked afresh every single time."""
        return (self.state_dir / KILL_SWITCH_NAME).exists()

    async def start(self) -> bool:
        """Bind the socket and start listening. False when the bridge stays off.

        A socket file left behind by a process that died is unlinked and replaced; one that
        something is *still listening on* is left alone and this broker simply does not run,
        because two brokers answering the same hook would both try to ring the owner.
        """
        if not self._settings.approvals_enabled:
            log.info("the approval bridge is off (APPROVALS_ENABLED)")
            return False
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.state_dir, 0o700)
        self._clear_marker()

        path = self.socket_path
        if len(str(path).encode()) > MAX_SOCKET_PATH_BYTES:
            log.error(
                "%s is %d bytes, too long for a unix socket (the limit is about %d); the "
                "approval bridge is off — put DATA_DIR somewhere shorter",
                path,
                len(str(path).encode()),
                MAX_SOCKET_PATH_BYTES,
            )
            return False
        if path.exists():
            if await self._someone_listening(path):
                log.error("another Jarvis is already serving %s; the approval bridge is off", path)
                return False
            with contextlib.suppress(OSError):
                path.unlink()
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(path))
            os.chmod(path, 0o600)
        except OSError:
            log.exception("could not listen on %s; the approval bridge is off", path)
            self._server = None
            return False
        log.info("approval bridge listening on %s", path)
        self._audit("started", None, escalate_after=self._settings.approval_escalate_seconds)
        return True

    async def stop(self) -> None:
        """Stop listening, release every waiting hook, and take the socket away. Idempotent."""
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        for request in list(self._pending.values()):
            self._settle(request, Outcome.EXPIRED, None, via="shutdown")
        with contextlib.suppress(OSError):
            self.socket_path.unlink()
        self._clear_marker()

    @staticmethod
    async def _someone_listening(path: Path) -> bool:
        try:
            _, writer = await asyncio.open_unix_connection(str(path))
        except OSError:
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    # --- the socket --------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One hook invocation: read what it wants, answer it, close."""
        try:
            line = await asyncio.wait_for(reader.readline(), CLIENT_READ_TIMEOUT_S)
            message = json.loads(line or b"{}")
            if not isinstance(message, dict):
                raise ValueError("not an object")
        except (TimeoutError, ValueError, json.JSONDecodeError, OSError):
            log.warning("dropped a malformed approval client")
            await self._reply(writer, {"decision": "none", "reason": "malformed"})
            return

        event = message.get("event") if isinstance(message.get("event"), dict) else {}
        if message.get("op") == "resolve":
            self._resolve_from_event(event)
            await self._reply(writer, {"ok": True})
            return
        if message.get("op") != "raise" or message.get("protocol") != PROTOCOL:
            await self._reply(writer, {"decision": "none", "reason": "unsupported"})
            return
        await self._raise(event, reader, writer)

    async def _reply(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        with contextlib.suppress(Exception):
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()

    async def _raise(self, event: dict, reader, writer) -> None:
        """Register a pending prompt and hold the hook until somebody answers or time runs out."""
        if self.disabled:
            await self._reply(writer, {"decision": "none", "reason": "disabled"})
            return
        described = classify(event, self._settings)
        if described is None:
            await self._reply(writer, {"decision": "none", "reason": "ineligible"})
            return

        request = ApprovalRequest(
            id=self._next_id,
            session_id=str(event.get("session_id") or ""),
            cwd=str(event.get("cwd") or ""),
            tool_name=str(event.get("tool_name") or ""),
            raised_at=self._now(),
            **described,
        )
        self._next_id += 1
        self._pending[request.id] = request
        self._touch_marker()
        self._audit("raised", request)

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._waiters[request.id] = waiter
        self._spawn(self._escalate_later(request), name=f"approval-{request.id}")

        # Whichever comes first: an answer, the hook's process going away (EOF), or the end
        # of the window. Two of those three mean the prompt is left exactly as it was.
        eof = asyncio.ensure_future(reader.read(1))
        lifetime = (
            self._settings.approval_escalate_seconds + self._settings.approval_call_window_seconds
        )
        try:
            await asyncio.wait({waiter, eof}, timeout=lifetime, return_when=asyncio.FIRST_COMPLETED)
        finally:
            eof.cancel()
            self._waiters.pop(request.id, None)

        if waiter.done() and not waiter.cancelled():
            verdict = waiter.result()
        else:
            # A completed read means end-of-file — the hook's process is gone, so there is
            # no prompt left to answer. Anything else here is the window simply running out.
            done = eof.done() and not eof.cancelled() and not eof.exception()
            gone = done and eof.result() == b""
            self._settle(request, Outcome.ABANDONED if gone else Outcome.EXPIRED, None)
            verdict = None

        payload = {"decision": "none"} if verdict is None else {"decision": verdict.payload()}
        await self._reply(writer, payload)

    # --- resolution from the other hooks -----------------------------------

    def _resolve_from_event(self, event: dict) -> None:
        """Mark pending prompts resolved because Claude reported them dealt with.

        `PostToolUse` and `PermissionDenied` name a single tool call, so they resolve only
        the request whose tool and input hash match. `Stop` and `SessionEnd` name no tool at
        all, so they clear everything that session had waiting — the safe direction, because
        the cost of clearing one too many is a prompt they are not rung about.
        """
        name = str(event.get("hook_event_name") or "")
        session_id = str(event.get("session_id") or "")
        if name not in RESOLVING_EVENTS or not session_id:
            return
        tool = event.get("tool_name")
        tool_input = event.get("tool_input")
        sha = input_digest(tool_input) if isinstance(tool_input, dict) else None
        precise = name in PRECISE_EVENTS and bool(tool) and sha is not None

        for request in list(self._pending.values()):
            if request.session_id != session_id:
                continue
            if precise and (request.tool_name != tool or request.input_sha != sha):
                continue
            self._settle(request, Outcome.RESOLVED_ELSEWHERE, None, via=name)

    # --- escalation --------------------------------------------------------

    async def _escalate_later(self, request: ApprovalRequest) -> None:
        """Wait out the grace period, then tell them — if it is still worth telling them."""
        try:
            await asyncio.sleep(self._settings.approval_escalate_seconds)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            return
        if not request.pending:
            return
        reason = self._blocked()
        if reason is not None:
            self._audit("escalation_skipped", request, reason=reason)
            return
        try:
            await self._escalate(request)
        except Exception:
            log.exception("could not escalate approval request %s", request.id)
            self._audit("escalation_failed", request)

    def _blocked(self) -> str | None:
        """Why they must not be told about this right now, or None."""
        if self.disabled:
            return "the kill switch is on"
        if self._quiet_now():
            return "quiet hours"
        window = self._now() - 3600
        while self._dials and self._dials[0] < window:
            self._dials.popleft()
        if len(self._dials) >= self._settings.approval_max_per_hour:
            return f"already rang {len(self._dials)} times this hour"
        return None

    async def _escalate(self, request: ApprovalRequest) -> None:
        """Announce it into a call already in progress, or place one."""
        waited = self._waited(request)
        line = REQUEST_LINE.format(id=request.id, summary=request.summary, menu=request.menu())
        announced = await announce_to_live_sessions(
            self._sessions,
            ANNOUNCE_TEXT.format(waited=waited, line=line),
            # What is waiting on their screen names their projects and their commands, and
            # only a call that could answer it has any business hearing it.
            needs=TrustLevel.POSSESSION,
        )
        if announced.heard:
            # They are already on the phone. A second call about the same thing is the exact
            # duplicate this feature has to avoid, so it goes into the call they are on. Only a
            # call that took it counts: one that has proved nothing refuses it.
            request.escalated_at = self._now()
            request.escalated_via = "announce"
            self._audit("escalated", request, via="announce")
            return

        if self._dialled_at is not None and self._now() - self._dialled_at < CALL_TOKEN_TTL_S:
            # A call about another request is already on its way; it will carry this one too.
            request.escalated_at = self._now()
            request.escalated_via = "riding"
            self._audit("escalated", request, via="riding")
            return

        await self._dial(request, waited)

    async def _dial(self, request: ApprovalRequest, waited: str) -> None:
        host = self._settings.public_host
        number = self._settings.owner_number
        if not (host and number and self._twilio.configured):
            self._audit("escalation_skipped", request, reason="no number, host or Twilio")
            return

        lines = "\n".join(
            REQUEST_LINE.format(id=item.id, summary=item.summary, menu=item.menu())
            for item in self._pending.values()
            if item.pending
        )
        token = self._stream_tokens.issue(
            caller=number,
            # Jarvis is dialling `OWNER_NUMBER` itself, so the session that answers opens
            # at `POSSESSION` — which is the level that may answer an approval on the
            # keypad, and this call exists to have one answered (`jarvis.trust`).
            extra=outbound_extra(
                number, opening_context=CALL_CONTEXT.format(waited=waited, requests=lines)
            ),
            ttl_s=CALL_TOKEN_TTL_S,
        )
        sid = await self._twilio.place_call(
            number,
            twiml=stream_twiml(host, {"token": token, "caller": number}),
            status_callback=f"https://{host}/twilio/status",
        )
        self._dialled_at = self._now()
        self._dials.append(self._now())
        request.escalated_at = self._now()
        request.escalated_via = "call"
        self._audit("escalated", request, via="call", call_sid=sid)

    def _waited(self, request: ApprovalRequest) -> str:
        minutes = max(1, round((self._now() - request.raised_at) / 60))
        return "a minute" if minutes == 1 else f"{minutes} minutes"

    def _quiet_now(self) -> bool:
        """True inside `APPROVAL_QUIET_HOURS` (`23:00-07:00`); blank or unparseable is never."""
        window = (self._settings.approval_quiet_hours or "").strip()
        if not window:
            return False
        try:
            start_text, end_text = window.split("-", 1)
            start = _minutes(start_text)
            end = _minutes(end_text)
        except (ValueError, IndexError):
            log.warning("APPROVAL_QUIET_HOURS is not `HH:MM-HH:MM`: %r", window)
            return False
        # Naive local time, deliberately: `HH:MM` in a window called "quiet hours" means
        # the clock on the wall next to whoever set it, and this is a single-owner service
        # running on their own machine. The assumption is therefore that the *host's*
        # timezone is theirs — which is true of a laptop and of a server at home, and not
        # true of a VPS in another region. There is no timezone setting because there is
        # no second user to have a different one; if that ever changes, this is the line.
        current = datetime.now().hour * 60 + datetime.now().minute
        if start <= end:
            return start <= current < end
        return current >= start or current < end  # a window that crosses midnight

    # --- what the voice model calls into -----------------------------------

    def pending_requests(self) -> list[dict]:
        """Every prompt still waiting, in the words the model reads out."""
        return [
            {"request_id": request.id, "summary": request.summary, "options": request.menu()}
            for request in self._pending.values()
            if request.pending
        ]

    def arm(self, request_id: object, session_id: str) -> dict:
        """Offer the keypad menu for one request. This does **not** answer anything.

        The split is the whole safety story: the model may only ever get as far as reading
        a menu out, and the digit that follows is the one thing that can approve.
        """
        request = self._pending.get(request_id) if isinstance(request_id, int) else None
        if request is None or not request.pending:
            return {"status": "gone", "message": "That request is not waiting any more."}
        self._armed[session_id] = (request.id, self._now() + ARM_TTL_S)
        self._audit("armed", request, session=session_id)
        return {
            "status": "awaiting_keypad",
            "request_id": request.id,
            "summary": request.summary,
            "options": request.menu(),
        }

    def digit(self, session_id: str, key: str) -> str | None:
        """Apply a keypad digit to whatever this call armed. None means "not for us".

        Anything that is not a digit on the menu leaves the request exactly where it was and
        asks them again — an unrecognised key must never be read as agreement.
        """
        armed = self._armed.get(session_id)
        if armed is None:
            return None
        request_id, expires_at = armed
        if self._now() >= expires_at:
            self._armed.pop(session_id, None)
            return None
        request = self._pending.get(request_id)
        if request is None or not request.pending:
            self._armed.pop(session_id, None)
            return DIGIT_GONE.format(id=request_id)

        if key == "0":
            self._armed.pop(session_id, None)
            self._settle(request, Outcome.LEFT, None, via="keypad", answer="left")
            return DIGIT_LEFT.format(id=request.id)
        if not (key.isdigit() and 1 <= int(key) <= len(request.options)):
            self._audit("keypad_unrecognised", request, session=session_id)
            return DIGIT_UNKNOWN.format(id=request.id, menu=request.menu())

        choice = request.options[int(key) - 1]
        self._armed.pop(session_id, None)
        verdict, outcome_word = _verdict_for(request, int(key) - 1, choice)
        self._settle(request, Outcome.ANSWERED, verdict, via="keypad", answer=choice)
        return DIGIT_APPLIED.format(id=request.id, outcome=outcome_word)

    # --- bookkeeping -------------------------------------------------------

    def _settle(
        self,
        request: ApprovalRequest,
        outcome: Outcome,
        verdict: Verdict | None,
        *,
        via: str | None = None,
        answer: str | None = None,
    ) -> None:
        """End a request exactly once, and release the hook that is waiting on it."""
        if not request.pending:
            self._audit("verdict_discarded", request, via=via, answer=answer)
            return
        request.outcome = outcome
        request.answered_at = self._now()
        request.answer = answer
        self._pending.pop(request.id, None)
        for session_id, (armed_id, _) in list(self._armed.items()):
            if armed_id == request.id:
                self._armed.pop(session_id, None)
        waiter = self._waiters.get(request.id)
        if waiter is not None and not waiter.done():
            waiter.set_result(verdict)
        self._touch_marker()
        self._audit("settled", request, via=via, answer=answer, applied=verdict is not None)

    def _touch_marker(self) -> None:
        """Keep `PENDING` in step with the table, so the `PostToolUse` hook can stat and exit."""
        marker = self.state_dir / MARKER_NAME
        try:
            if self._pending:
                marker.touch()
            else:
                marker.unlink(missing_ok=True)
        except OSError:
            log.warning("could not update the approvals marker at %s", marker)

    def _clear_marker(self) -> None:
        with contextlib.suppress(OSError):
            (self.state_dir / MARKER_NAME).unlink(missing_ok=True)

    def _audit(self, event: str, request: ApprovalRequest | None, **extra: object) -> None:
        """One line per thing that happened, in a 0600 file nothing here ever rewrites.

        The full tool input is never written: what lands is the bounded spoken summary and
        the SHA-256 of the input, which is enough to recognise the same call again and not
        enough to leak the contents of a file.
        """
        line = {"ts": datetime.now(UTC).isoformat(), "event": event}
        if request is not None:
            line.update(request.audit())
        line.update({key: value for key, value in extra.items() if value is not None})
        path = self.state_dir / AUDIT_NAME
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, default=str) + "\n")
            if not existed:
                os.chmod(path, 0o600)
        except OSError:
            log.warning("could not write the approvals audit line %s", event)

    def _spawn(self, coro, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _verdict_for(request: ApprovalRequest, index: int, choice: str) -> tuple[Verdict, str]:
    """The verdict a chosen option comes to, and the word for it in the spoken note.

    A question is answered by a *denial* carrying the answer: a plain `allow` on a tool that
    needs interaction falls straight through to the on-screen picker, so the answer has to
    ride back as feedback (measured; see the report).
    """
    if request.kind is Kind.QUESTION:
        return Verdict("deny", ANSWERED_MESSAGE.format(answer=choice)), f"answered {choice}"
    if index == 0:
        return Verdict("allow", APPROVED_MESSAGE), "approved"
    return Verdict("deny", REJECTED_MESSAGE), "rejected"


def _minutes(text: str) -> int:
    hour, minute = text.strip().split(":", 1)
    return int(hour) * 60 + int(minute)
