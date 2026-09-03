"""The broker, driven through its real Unix socket.

Every test here speaks the wire protocol the Claude Code hook speaks, because the socket
*is* the interface: a test that called the methods directly would not catch the thing that
matters most — that a hook which is told nothing leaves the on-screen prompt alone.

Nothing touches the network: Twilio is a recording double, and the "live session" is a stub
with the two attributes the broker looks at.
"""

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field

import pytest

from jarvis.approvals.broker import ApprovalBroker
from jarvis.config import Settings
from jarvis.stream_tokens import StreamTokenStore

TIMEOUT = 3.0


# --- harness ---------------------------------------------------------------


@dataclass
class FakeTwilio:
    configured: bool = True
    calls: list[dict] = field(default_factory=list)
    error: Exception | None = None

    async def place_call(self, to, *, twiml, status_callback=None):
        if self.error is not None:
            raise self.error
        self.calls.append({"to": to, "twiml": twiml})
        return f"CA{len(self.calls)}"


@dataclass
class FakeSession:
    session_id: str = "call1"
    announcements: list[str] = field(default_factory=list)

    async def announce(self, text: str) -> bool:
        self.announcements.append(text)
        return True


@dataclass
class FakeSessions:
    sessions: list[FakeSession] = field(default_factory=list)

    def live(self):
        return list(self.sessions)


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


@pytest.fixture
def settings(tmp_path, short_tmp_path):
    (tmp_path / "roots" / "myproject").mkdir(parents=True)
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=short_tmp_path / "jarvis",
        google_client_secrets_file=tmp_path / "none.json",
        approval_roots=[str(tmp_path / "roots")],
        public_host="jarvis.example",
        owner_number_explicit="+491700000000",
        approval_escalate_seconds=0.05,
        approval_call_window_seconds=1.5,
    )


@pytest.fixture
def twilio():
    return FakeTwilio()


@pytest.fixture
def sessions():
    return FakeSessions()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def broker(settings, sessions, twilio, clock):
    settings.ensure_dirs()
    made = ApprovalBroker(settings, sessions, twilio, StreamTokenStore(), now=clock)
    assert await made.start()
    try:
        yield made
    finally:
        await made.stop()


async def test_a_data_dir_too_deep_for_a_unix_socket_is_explained(settings, sessions, twilio):
    """`OSError: AF_UNIX path too long` says nothing anyone can act on; this says the fix."""
    deep = settings.data_dir.joinpath(*["a-fairly-long-directory-name"] * 5)
    broker = ApprovalBroker(
        settings.model_copy(update={"data_dir": deep}), sessions, twilio, StreamTokenStore()
    )

    assert await broker.start() is False
    assert not broker.socket_path.exists()


def permission_event(tool="Bash", tool_input=None, session_id="claude1", cwd=None, **over):
    event = {
        "hook_event_name": "PermissionRequest",
        "session_id": session_id,
        "cwd": cwd or "/tmp",
        "permission_mode": "default",
        "tool_name": tool,
        "tool_input": tool_input if tool_input is not None else {"command": "git push"},
    }
    event.update(over)
    return event


class Hook:
    """One connection, held open the way the real hook holds it open."""

    def __init__(self, path):
        self._path = path
        self.reader = self.writer = None

    async def send(self, payload):
        self.reader, self.writer = await asyncio.open_unix_connection(str(self._path))
        self.writer.write((json.dumps(payload) + "\n").encode())
        await self.writer.drain()

    async def reply(self, timeout=TIMEOUT):
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        return json.loads(line)

    async def close(self):
        if self.writer is None:
            return
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


@pytest.fixture
async def hooks(broker):
    """Opens hook connections and always closes them: an unclosed one is a test error."""
    opened: list[Hook] = []

    class Hooks:
        async def raise_request(self, event):
            hook = Hook(broker.socket_path)
            opened.append(hook)
            await hook.send({"op": "raise", "protocol": 1, "event": event})
            return hook

        async def resolve(self, event):
            hook = Hook(broker.socket_path)
            opened.append(hook)
            await hook.send({"op": "resolve", "protocol": 1, "event": event})
            await hook.reply()
            await hook.close()

        async def pending_one(self, tmp_path, **over):
            hook = await self.raise_request(
                permission_event(cwd=str(tmp_path / "roots" / "myproject"), **over)
            )
            await until(lambda: broker.pending_requests())
            return hook

    yield Hooks()
    for hook in opened:
        await hook.close()


async def until(predicate, timeout=TIMEOUT):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("still false after the timeout")
        await asyncio.sleep(0.01)


# --- the socket ------------------------------------------------------------


async def test_the_socket_is_private(broker):
    assert os.stat(broker.socket_path).st_mode & 0o777 == 0o600


async def test_a_malformed_client_is_told_nothing(broker):
    reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
    writer.write(b"not json at all\n")
    await writer.drain()
    answer = json.loads(await asyncio.wait_for(reader.readline(), TIMEOUT))
    assert answer["decision"] == "none"
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def test_an_unknown_protocol_is_told_nothing(broker):
    reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
    writer.write(json.dumps({"op": "raise", "protocol": 99, "event": {}}).encode() + b"\n")
    await writer.drain()
    answer = json.loads(await asyncio.wait_for(reader.readline(), TIMEOUT))
    assert answer["decision"] == "none"
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def test_a_second_broker_does_not_take_the_socket(broker, settings, sessions, twilio):
    """Two brokers answering the same hook would both try to ring him."""
    other = ApprovalBroker(settings, sessions, twilio, StreamTokenStore())
    assert await other.start() is False


# --- eligibility -----------------------------------------------------------


async def test_an_ineligible_prompt_is_answered_at_once_and_never_pends(broker, hooks):
    hook = await hooks.raise_request(permission_event("WebFetch", {"url": "http://x"}))
    assert (await hook.reply())["decision"] == "none"
    assert broker.pending_requests() == []
    assert not (broker.state_dir / "PENDING").exists()


async def test_the_kill_switch_stops_everything(broker, hooks):
    (broker.state_dir / "DISABLED").touch()
    hook = await hooks.raise_request(permission_event(cwd=str(broker._settings.approval_roots[0])))
    assert (await hook.reply())["decision"] == "none"
    assert broker.pending_requests() == []


async def test_an_eligible_prompt_pends_and_marks(broker, tmp_path, hooks):
    await hooks.raise_request(permission_event(cwd=str(tmp_path / "roots" / "myproject")))
    await until(lambda: broker.pending_requests())
    waiting = broker.pending_requests()
    assert waiting[0]["request_id"] == 1
    assert "git push" in waiting[0]["summary"]
    assert "press 1 for approve" in waiting[0]["options"]
    assert (broker.state_dir / "PENDING").exists()


# --- escalation ------------------------------------------------------------


async def test_it_rings_him_when_nobody_answers(broker, twilio, tmp_path, hooks):
    await hooks.raise_request(permission_event(cwd=str(tmp_path / "roots" / "myproject")))
    await until(lambda: twilio.calls)
    assert twilio.calls[0]["to"] == "+491700000000"
    assert "jarvis.example" in twilio.calls[0]["twiml"]


async def test_the_call_carries_the_request_and_the_menu(broker, twilio, tmp_path, hooks):
    await hooks.raise_request(permission_event(cwd=str(tmp_path / "roots" / "myproject")))
    await until(lambda: twilio.calls)
    token = broker._stream_tokens._entries  # the only copy of the context before it is redeemed
    context = next(iter(token.values())).info.extra["opening_context"]
    assert "Request 1" in context
    assert "git push" in context
    assert "press 1 for approve" in context
    assert "keypad" in context


async def test_a_live_call_is_told_instead_of_a_second_one_being_placed(
    broker, hooks, sessions, twilio, tmp_path
):
    """The duplicate this feature exists to avoid: ringing a phone he is already on."""
    session = FakeSession()
    sessions.sessions.append(session)
    await hooks.raise_request(permission_event(cwd=str(tmp_path / "roots" / "myproject")))
    await until(lambda: session.announcements)
    assert twilio.calls == []
    assert "Request 1" in session.announcements[0]


async def test_a_second_request_rides_the_call_already_going_out(broker, twilio, tmp_path, hooks):
    cwd = str(tmp_path / "roots" / "myproject")
    await hooks.raise_request(permission_event(cwd=cwd, session_id="a"))
    await until(lambda: twilio.calls)
    await hooks.raise_request(
        permission_event(cwd=cwd, session_id="b", tool_input={"command": "git commit"})
    )
    await asyncio.sleep(0.2)
    assert len(twilio.calls) == 1


async def test_the_hourly_cap_stops_it_ringing_again(broker, twilio, clock, tmp_path, hooks):
    cwd = str(tmp_path / "roots" / "myproject")
    for index in range(broker._settings.approval_max_per_hour):
        await hooks.raise_request(
            permission_event(
                cwd=cwd,
                session_id=f"s{index}",
                tool_input={"command": f"git commit -m {index}"},
            )
        )
        await until(lambda expected=index + 1: len(twilio.calls) == expected)
        clock.value += 300  # past the "a call is already going out" window
    await hooks.raise_request(
        permission_event(cwd=cwd, session_id="last", tool_input={"command": "pytest"})
    )
    await asyncio.sleep(0.2)
    assert len(twilio.calls) == broker._settings.approval_max_per_hour
    assert any("already rang" in line for line in _audit(broker, "escalation_skipped"))


async def test_quiet_hours_are_honoured(broker, twilio, tmp_path, monkeypatch, hooks):
    broker._settings.approval_quiet_hours = "00:00-23:59"
    await hooks.raise_request(permission_event(cwd=str(tmp_path / "roots" / "myproject")))
    await asyncio.sleep(0.2)
    assert twilio.calls == []


async def test_a_twilio_failure_leaves_the_prompt_where_it_was(broker, twilio, tmp_path, hooks):
    twilio.error = RuntimeError("no credit")
    hook = await hooks.raise_request(permission_event(cwd=str(tmp_path / "roots" / "myproject")))
    assert (await hook.reply())["decision"] == "none"


# --- resolution ------------------------------------------------------------


async def test_answering_at_the_keyboard_releases_the_hook(broker, tmp_path, hooks):
    """The sharp edge: the hook is not killed when he answers, so it has to be told."""
    cwd = str(tmp_path / "roots" / "myproject")
    event = permission_event(cwd=cwd)
    hook = await hooks.raise_request(event)
    await until(lambda: broker.pending_requests())
    await hooks.resolve(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "claude1",
            "tool_name": "Bash",
            "tool_input": event["tool_input"],
        },
    )
    assert (await hook.reply())["decision"] == "none"
    assert broker.pending_requests() == []


async def test_a_different_tool_call_does_not_resolve_it(broker, tmp_path, hooks):
    cwd = str(tmp_path / "roots" / "myproject")
    await hooks.raise_request(permission_event(cwd=cwd))
    await until(lambda: broker.pending_requests())
    await hooks.resolve(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "claude1",
            "tool_name": "Bash",
            "tool_input": {"command": "something else entirely"},
        },
    )
    assert broker.pending_requests()


async def test_the_session_ending_clears_everything_it_had_waiting(broker, tmp_path, hooks):
    cwd = str(tmp_path / "roots" / "myproject")
    await hooks.raise_request(permission_event(cwd=cwd))
    await until(lambda: broker.pending_requests())
    await hooks.resolve({"hook_event_name": "SessionEnd", "session_id": "claude1"})
    assert broker.pending_requests() == []


async def test_another_sessions_stop_leaves_it_alone(broker, tmp_path, hooks):
    cwd = str(tmp_path / "roots" / "myproject")
    await hooks.raise_request(permission_event(cwd=cwd))
    await until(lambda: broker.pending_requests())
    await hooks.resolve({"hook_event_name": "Stop", "session_id": "somebody-else"})
    assert broker.pending_requests()


async def test_a_hook_that_goes_away_abandons_its_request(broker, tmp_path, hooks):
    cwd = str(tmp_path / "roots" / "myproject")
    hook = await hooks.raise_request(permission_event(cwd=cwd))
    await until(lambda: broker.pending_requests())
    hook.writer.close()
    await until(lambda: not broker.pending_requests())


async def test_a_prompt_nobody_answers_expires_silently(broker, tmp_path, hooks):
    broker._settings.approval_call_window_seconds = 0.05
    hook = await hooks.raise_request(permission_event(cwd=str(tmp_path / "roots" / "myproject")))
    assert (await hook.reply())["decision"] == "none"
    assert broker.pending_requests() == []


# --- the keypad, which is the only thing that can answer -------------------


async def test_arming_answers_nothing_by_itself(broker, tmp_path, hooks):
    await hooks.pending_one(tmp_path)
    armed = broker.arm(1, "call1")
    assert armed["status"] == "awaiting_keypad"
    await asyncio.sleep(0.1)
    assert broker.pending_requests()  # still waiting: a menu is not an answer


async def test_one_on_the_keypad_approves(broker, tmp_path, hooks):
    hook = await hooks.pending_one(tmp_path)
    broker.arm(1, "call1")
    assert "approved" in broker.digit("call1", "1")
    answer = await hook.reply()
    assert answer["decision"]["behavior"] == "allow"
    assert "keypad" in answer["decision"]["message"]


async def test_two_on_the_keypad_rejects(broker, tmp_path, hooks):
    hook = await hooks.pending_one(tmp_path)
    broker.arm(1, "call1")
    assert "rejected" in broker.digit("call1", "2")
    answer = await hook.reply()
    assert answer["decision"]["behavior"] == "deny"


async def test_zero_leaves_it_on_his_screen(broker, tmp_path, hooks):
    hook = await hooks.pending_one(tmp_path)
    broker.arm(1, "call1")
    assert "left alone" in broker.digit("call1", "0")
    assert (await hook.reply())["decision"] == "none"


async def test_a_key_that_is_not_on_the_menu_decides_nothing(broker, tmp_path, hooks):
    """An unrecognised key must never be read as agreement."""
    await hooks.pending_one(tmp_path)
    broker.arm(1, "call1")
    message = broker.digit("call1", "7")
    assert "not one of the options" in message
    assert broker.pending_requests()


async def test_a_digit_nobody_armed_is_dropped(broker, tmp_path, hooks):
    await hooks.pending_one(tmp_path)
    assert broker.digit("call1", "1") is None
    assert broker.pending_requests()


async def test_an_armed_confirmation_goes_stale(broker, tmp_path, clock, hooks):
    await hooks.pending_one(tmp_path)
    broker.arm(1, "call1")
    clock.value += 1000
    assert broker.digit("call1", "1") is None
    assert broker.pending_requests()


async def test_a_second_digit_cannot_answer_the_same_request_twice(broker, tmp_path, hooks):
    hook = await hooks.pending_one(tmp_path)
    broker.arm(1, "call1")
    broker.digit("call1", "1")
    await hook.reply()
    assert broker.digit("call1", "2") is None


async def test_arming_something_already_answered_says_so(broker, tmp_path, hooks):
    hook = await hooks.pending_one(tmp_path)
    broker.arm(1, "call1")
    broker.digit("call1", "1")
    await hook.reply()
    assert broker.arm(1, "call1")["status"] == "gone"


async def test_arming_a_request_that_never_existed_says_so(broker):
    assert broker.arm(99, "call1")["status"] == "gone"
    assert broker.arm("one", "call1")["status"] == "gone"


async def test_a_question_is_answered_by_a_denial_carrying_the_answer(broker, tmp_path, hooks):
    """A plain `allow` falls through to the on-screen picker, so the answer rides a deny."""
    hook = await hooks.pending_one(
        tmp_path,
        tool="AskUserQuestion",
        tool_input={
            "questions": [
                {"question": "Tabs or spaces?", "options": [{"label": "Tabs"}, {"label": "Spaces"}]}
            ]
        },
    )
    broker.arm(1, "call1")
    broker.digit("call1", "2")
    answer = await hook.reply()
    assert answer["decision"]["behavior"] == "deny"
    assert "Spaces" in answer["decision"]["message"]


# --- the audit log ---------------------------------------------------------


def _audit(broker, event=None):
    path = broker.state_dir / "audit.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [json.dumps(line) for line in lines if event is None or line["event"] == event]


async def test_every_step_lands_in_the_audit_log(broker, tmp_path, hooks):
    hook = await hooks.pending_one(tmp_path)
    broker.arm(1, "call1")
    broker.digit("call1", "1")
    await hook.reply()
    events = [json.loads(line)["event"] for line in _audit(broker)]
    assert {"raised", "armed", "settled"} <= set(events)


async def test_the_audit_log_is_private_and_carries_no_file_contents(broker, tmp_path, hooks):
    target = tmp_path / "roots" / "myproject" / "notes.md"
    await hooks.pending_one(
        tmp_path,
        tool="Write",
        tool_input={"file_path": str(target), "content": "a very secret sentence"},
    )
    path = broker.state_dir / "audit.jsonl"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert "a very secret sentence" not in path.read_text()


async def test_stopping_releases_every_waiting_hook(broker, tmp_path, hooks):
    hook = await hooks.pending_one(tmp_path)
    await broker.stop()
    assert (await hook.reply())["decision"] == "none"
    assert not broker.socket_path.exists()
