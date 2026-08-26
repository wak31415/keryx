"""Restarting the service, and phoning back once it is up again (spec §3.3).

A restart is the one job Jarvis cannot see through inside one process: the process that
runs `systemctl restart` is the process the service manager then kills. The flow is
therefore split across that death, joined by one small file (`data_dir/restart.json`):

1. **Before** — `RestartCoordinator.request()` records what was asked for, by whom, on
   which number, and which version was running; then it hands the restart to the service
   manager. If somebody is on the phone it waits for them to hang up first: a restart
   drops every live call, so an immediate one would cut off the very person who asked.
2. **After** — `resume()`, called once from `jarvis serve`, finds that record, waits until
   the phone server is really listening (the call-back is answered by our own
   `<Connect><Stream>`, so ringing before then rings into nothing), and calls the user
   with a one-line status summary as the new session's opening context.

The second half is written for a machine that may be broken, so nothing here raises. The
record carries an attempt count, so a crash loop cannot dial once per crash; a call that
cannot be placed falls back to a text; and a record that could not be delivered at all is
kept — marked `failed`, with the error on it, for `jarvis restart --status` to read back —
rather than retried forever.

Two things the flow deliberately does not do. It never dials into a live session: if
somebody is already talking to Jarvis they are told there and then instead, and if the
line is busy but nothing can be said into it, the confirmation goes out as a text. And it
cannot notice that the service never came back at all — nothing of ours is left running to
notice. `Restart=always` (systemd) / `KeepAlive` (launchd) is what covers that, and
`jarvis restart` says so as it hands over.
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvis.config import Settings
from jarvis.logscan import errors_since, marks
from jarvis.notify.notifier import CALLBACK_TOKEN_TTL_S
from jarvis.notify.twilio_out import stream_twiml
from jarvis.session import SessionRegistry
from jarvis.stream_tokens import StreamTokenStore
from jarvis.tasks.models import TaskStatus
from jarvis.tasks.store import TaskStore

log = logging.getLogger("jarvis.restart")

#: Where the two halves of a restart meet, under `data_dir`.
RECORD_NAME = "restart.json"

#: Default unit/label names, matching `ops/systemd/jarvis.service` and
#: `ops/launchd/com.william.jarvis.plist`.
SYSTEMD_UNIT = "jarvis.service"
LAUNCHD_LABEL = "com.william.jarvis"

#: How many times a pending record may be picked up before it is abandoned. A service
#: that crash-loops must ring once, not once per crash.
MAX_CALLBACK_ATTEMPTS = 2
#: How long `resume()` waits for the phone server to start listening.
READY_TIMEOUT_S = 60.0
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
#: Bound on `git describe`, which is only ever decoration on the summary.
GIT_TIMEOUT_S = 2.0
#: How many rows the task counts in the summary look at.
TASK_SCAN_LIMIT = 50

#: What a live session hears instead of a call — nobody is rung mid-conversation.
RESTART_ANNOUNCEMENT = "The restart is done and Jarvis is back up: {status}."
#: The opening context of the call-back itself: a confirmation, not a report.
RESTART_CONTEXT = (
    "You are calling the user back because the Jarvis service — you — has just restarted "
    "and is running again. He asked for the restart {when}{reason}{change}, and this call "
    "is the confirmation. Status: {status}. Greet him and tell him in one or two sentences "
    "whether it worked. If the status mentions errors in the log, or says the checkout did "
    "not change, that is the headline: say plainly that the update may not have taken, say "
    "what the error was, and offer to put Claude on it. Otherwise say it went through, "
    "mention anything else in the status he would want to know, and ask if he needs "
    "anything else. Keep it short: he asked for a restart, not a report. This is a new "
    "call: he may have to give the PIN again before you can start more work."
)
#: The clause that names the work a restart was loading, for the context above.
LOADING_TASK = ", to load the work from task {task_id}"
#: The same confirmation as a text, when no call can be placed.
RESTART_SMS = "Jarvis restarted and is back up: {status}"
FAILED_SMS = "Jarvis tried to restart and it did not go through: {error}"

#: What the model is told to say when a restart has to wait for the call to end.
DEFERRED_MESSAGE = (
    "Tell him you will restart as soon as this call ends — a restart drops the call — and "
    "that you will ring him straight back when you are up again."
)
RESTARTING_MESSAGE = (
    "Tell him you are restarting now, that this call is about to drop, and that you will "
    "ring him back when you are up again."
)
NO_CALLBACK_MESSAGE = (
    "Tell him you are restarting now and that this call is about to drop. Warn him that "
    "you cannot ring him back afterwards, so he should call in to check."
)
UNSUPPORTED_MESSAGE = (
    "Tell him you cannot restart yourself: nothing on this machine is supervising the "
    "service, so stopping would leave nothing to start it again. He can restart it by "
    "hand with `jarvis restart` once the service is installed."
)
ALREADY_PENDING_MESSAGE = "Tell him a restart is already scheduled for when this call ends."

#: What `jarvis restart` prints when there is no service manager to ask.
UNSUPPORTED_HINT = """no service manager to restart through on this machine.

Install the service first (it is what starts Jarvis again after it stops):

  scripts/install-systemd.sh    # Linux
  scripts/install-launchd.sh    # macOS

Or set SERVICE_MANAGER / SERVICE_UNIT in .env if the unit is named something else. To
restart by hand instead, stop the process and start `jarvis serve` again — nothing else
will do it for you."""


# --- the service manager ----------------------------------------------------


@dataclass(frozen=True)
class ServiceTarget:
    """The unit this process runs as, and how to ask its manager to restart it."""

    manager: str  # "systemd" | "launchd"
    unit: str

    def command(self) -> list[str]:
        """The restart command, as a list (never a shell string)."""
        if self.manager == "systemd":
            return ["systemctl", "--user", "restart", self.unit]
        # `kickstart -k` kills the job and starts it again; `launchctl restart` is gone.
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{self.unit}"]

    def describe(self) -> str:
        return f"{self.manager} {self.unit}"


def resolve_target(
    settings: Settings,
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> ServiceTarget | None:
    """The service manager to restart through, or None when nothing supervises us.

    `SERVICE_MANAGER=auto` (the default) picks systemd on Linux and launchd on macOS, and
    only when the tool is actually on PATH — a Jarvis started by hand from a terminal has
    nothing that would bring it back, and must refuse to stop rather than take itself off
    the air. `none` refuses outright.
    """
    manager = settings.service_manager
    if manager == "auto":
        if platform.startswith("linux") and which("systemctl"):
            manager = "systemd"
        elif platform == "darwin" and which("launchctl"):
            manager = "launchd"
        else:
            manager = "none"
    if manager == "none":
        return None
    if not which("systemctl" if manager == "systemd" else "launchctl"):
        log.warning("SERVICE_MANAGER=%s but its command is not on PATH", manager)
        return None
    default = SYSTEMD_UNIT if manager == "systemd" else LAUNCHD_LABEL
    return ServiceTarget(manager, settings.service_unit or default)


def current_version(repo: Path | None = None) -> str | None:
    """`git describe` of the checkout we are running from — decoration, never required."""
    root = repo or Path(__file__).resolve().parents[2]
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "describe", "--always", "--dirty", "--abbrev=7"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


# --- the record that survives the restart -----------------------------------


@dataclass
class RestartRecord:
    """What the process that asked for the restart left for the one that comes back."""

    requested_at: str = ""
    reason: str = ""
    number: str | None = None
    origin_channel: str = "local"
    origin_session_id: str | None = None
    target: str = ""
    version: str | None = None
    state: str = "pending"  # "pending" until delivered, then the file is gone or "failed"
    attempts: int = 0
    error: str | None = None
    #: The task whose work this restart is loading, when it is loading one. A restart that
    #: exists to pick up a change Jarvis made to its own code is the only kind where "did
    #: it work" is a question about the *change* and not just about the process, so the
    #: confirmation names it and the version check below is only worth making with one.
    task_id: int | None = None
    #: How long each service log file was when the restart was asked for, so the process
    #: that comes back can tell this restart's errors from every earlier one (`logscan`).
    log_marks: dict[str, int] = field(default_factory=dict)
    #: How the out-of-process watchdog was started, or why it was not — the only thing that
    #: notices a service that never came back at all. See `jarvis.restart_watch`.
    watchdog: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "RestartRecord":
        """Build from JSON, ignoring anything an older or newer version wrote."""
        known = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the restart was asked for, or None if the stamp is unreadable."""
        try:
            asked = datetime.fromisoformat(self.requested_at)
        except ValueError:
            return None
        moment = now or datetime.now(UTC)
        return max((moment - asked).total_seconds(), 0.0)


class RestartStore:
    """The restart record on disk. Every method swallows I/O errors: this is a breadcrumb,
    and losing it must never be what takes the service down."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RestartRecord | None:
        """The record, or None when there is none (or it is unreadable/corrupt)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            log.warning("could not read the restart record at %s; ignoring it", self._path)
            return None
        if not isinstance(data, dict):
            return None
        try:
            return RestartRecord.from_dict(data)
        except TypeError:
            log.warning("the restart record at %s has an unexpected shape", self._path)
            return None

    def save(self, record: RestartRecord) -> bool:
        """Write the record atomically (0600). False if it could not be written."""
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(dataclasses.asdict(record), indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)  # it holds a phone number
            os.replace(tmp, self._path)
            return True
        except OSError:
            log.exception("could not write the restart record at %s", self._path)
            with contextlib.suppress(OSError):
                tmp.unlink()
            return False

    def clear(self) -> None:
        """Remove the record; a missing one is already cleared."""
        with contextlib.suppress(OSError):
            self._path.unlink(missing_ok=True)


# --- summary helpers --------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    """A spoken-length duration: seconds under two minutes, else whole minutes."""
    if seconds is None:
        return "an unknown time"
    if seconds < 90:
        count = max(int(round(seconds)), 1)
        return f"{count} second{'s' if count != 1 else ''}"
    minutes = int(round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def mask_number(number: str | None) -> str:
    """A phone number as it may appear in a terminal or a log: last four digits only."""
    if not number:
        return "nobody"
    return f"…{number[-4:]}" if len(number) > 4 else number


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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._twilio = twilio_out
        self._stream_tokens = stream_tokens
        self._tasks = task_store
        self._store = store or RestartStore(settings.data_dir / RECORD_NAME)
        self._spawn = spawn or _spawn_detached
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
            log.warning("refusing a restart: no service manager on this machine")
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
            version=await asyncio.to_thread(current_version),
            task_id=task_id,
            log_marks=await asyncio.to_thread(marks, self._settings.data_dir),
        )
        if not self._store.save(record):
            # Without the record the new process has no idea it should call anyone, and a
            # restart that goes quiet is worse than one that does not happen.
            return {"status": "failed", "message": "Tell him the restart could not be set up."}

        message = RESTARTING_MESSAGE if self._can_call_back(record) else NO_CALLBACK_MESSAGE
        if self._sessions.live() and not force:
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
        """Wait for every session to end, then restart. Give up rather than cut a call off."""
        if not await self._wait_for_quiet(DEFER_TIMEOUT_S):
            log.warning("abandoning the restart: a session has been live for %ss", DEFER_TIMEOUT_S)
            self._store.clear()
            return
        await self._execute(target)

    async def _wait_for_quiet(self, timeout: float) -> bool:
        """Poll until no session is live; False if `timeout` passes first."""
        waited = 0.0
        while self._sessions.live():
            if waited >= timeout:
                return False
            await self._sleep(QUIET_POLL_S)
            waited += QUIET_POLL_S
        return True

    async def _execute(self, target: ServiceTarget) -> None:
        """Hand the restart to the service manager; we expect to be killed doing it."""
        command = target.command()
        log.info("restarting through %s: %s", target.manager, " ".join(command))
        try:
            code = await self._spawn(command)
        except Exception as exc:  # a missing binary, a refused fork
            await self._failed(f"{type(exc).__name__}: {exc}")
            return
        if code:
            await self._failed(f"{command[0]} exited {code}")
            return
        # systemd kills us as part of the restart, so anything past here is the command
        # having quietly done nothing — the one failure that would otherwise go unnoticed.
        await self._sleep(EXEC_CONFIRM_S)
        await self._failed(f"{target.describe()} accepted the restart but nothing happened")

    async def _failed(self, error: str) -> None:
        """Record, announce and text a restart that did not go through."""
        log.error("the restart failed: %s", error)
        record = self._store.load() or RestartRecord()
        record.state = "failed"
        record.error = error
        self._store.save(record)
        spoken = f"The restart did not go through: {error}."
        if await self._announce(spoken):
            return
        await self._send_sms(record.number, FAILED_SMS.format(error=error))

    # --- (2) confirming it afterwards --------------------------------------

    async def resume(
        self,
        *,
        wait_ready: Callable[[], Awaitable[bool]] | None = None,
        wakeword: bool = False,
    ) -> None:
        """Deliver the confirmation for a pending restart, if there is one. Never raises.

        Called once per `jarvis serve`. `wait_ready` returns True when the phone server is
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

        errors = await asyncio.to_thread(errors_since, self._settings.data_dir, record.log_marks)
        if errors:
            parts.append(f"but {errors.spoken()}")

        version = await asyncio.to_thread(current_version)
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
            parts.append(f"{queued} task(s) left queued")
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
        reason = f", because {record.reason}" if record.reason else ""
        context = RESTART_CONTEXT.format(
            when=f"{format_duration(record.age_seconds())} ago",
            reason=reason,
            change=LOADING_TASK.format(task_id=record.task_id) if record.task_id else "",
            status=status,
        )
        token = self._stream_tokens.issue(
            caller=record.number,
            extra={"opening_context": context, "restart": True},
            ttl_s=CALLBACK_TOKEN_TTL_S,
        )
        twiml = stream_twiml(host, {"token": token, "caller": record.number})
        await self._twilio.place_call(
            record.number, twiml=twiml, status_callback=f"https://{host}/twilio/status"
        )
        log.info("called %s back to confirm the restart", mask_number(record.number))

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
        """Speak `text` into every live session; True if one of them took it."""
        heard = False
        try:
            for session in self._sessions.live():
                heard = await session.announce(text) or heard
        except Exception:
            log.exception("could not announce the restart into the live sessions")
        return heard

    async def _send_sms(self, number: str | None, body: str) -> bool:
        """Text `body`, if there is anything to text it with. Never raises."""
        to = number or self._settings.owner_number
        if not to or self._twilio is None or not self._twilio.configured:
            log.error("could not text about the restart (%s); it is only in the log", body)
            return False
        try:
            await self._twilio.send_sms(to, body)
        except Exception:
            log.exception("could not text about the restart")
            return False
        return True

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


def health_probe(settings: Settings, *, timeout: float = 2.0) -> int | None:
    """How many sessions the running Jarvis has live, or None if it is not answering.

    Used by `jarvis restart` from *outside* the process: the answer is what decides whether
    a restart would cut somebody off mid-call. `urllib` rather than a client library —
    this is one GET against localhost, on a machine that may be half-broken.
    """
    import urllib.error
    import urllib.request

    url = f"http://{settings.host}:{settings.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - localhost
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return None
    live = payload.get("live_sessions")
    return live if isinstance(live, int) else None


async def wait_until_serving(
    server: Any,
    *,
    timeout: float = READY_TIMEOUT_S,
    poll_s: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True once uvicorn reports it is serving; False if it never does inside `timeout`."""
    deadline = clock() + timeout
    while not getattr(server, "started", False):
        if clock() >= deadline:
            return False
        await asyncio.sleep(poll_s)
    return True
