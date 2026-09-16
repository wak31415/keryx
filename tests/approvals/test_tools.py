"""The two voice tools, and the one thing they are not allowed to do.

`answer_approval` never answers anything. The most it can do is hand the model a menu to
read out; the digit that follows is the only thing in Jarvis that can approve a tool call,
and it does not come through here. Both gates in front of the menu — the PIN and the phone
— are asserted below, because a spoken "yes" reaching an `allow` is the failure this whole
design exists to make impossible.
"""

from dataclasses import dataclass, field

import pytest

from jarvis.config import Settings
from jarvis.events import EventBus
from jarvis.inline_waits import InlineWaits
from jarvis.tasks.agent_runner import FakeAgentRunner
from jarvis.tasks.manager import TaskManager
from jarvis.tasks.store import TaskStore
from jarvis.tools import ToolContext, ToolRegistry
from jarvis.tools.builtin import register_builtin_tools

PIN = "424242"


@dataclass
class StubSession:
    authorized: bool = True
    channel: str = "phone"
    caller: str | None = "+15557000000"
    session_id: str = "call1"


@dataclass
class StubBroker:
    """The slice of `ApprovalBroker` the tools touch, recording what they asked for."""

    waiting: list[dict] = field(default_factory=list)
    armed: list[tuple[int, str]] = field(default_factory=list)
    answer: dict | None = None

    def pending_requests(self):
        return list(self.waiting)

    def arm(self, request_id, session_id):
        self.armed.append((request_id, session_id))
        if self.answer is not None:
            return self.answer
        return {
            "status": "awaiting_keypad",
            "request_id": request_id,
            "summary": "Claude wants to run: git push, in jarvis",
            "options": "press 1 for approve, press 2 for reject, or 0 to leave it on screen",
        }


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "jarvis",
        google_client_secrets_file=tmp_path / "none.json",
        pin=PIN,
    )


@pytest.fixture
def broker():
    return StubBroker()


@pytest.fixture
async def registry(settings, broker, tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    made = ToolRegistry()
    register_builtin_tools(
        made,
        manager=TaskManager(store, FakeAgentRunner(), EventBus(), settings),
        settings=settings,
        inline_waits=InlineWaits(),
        approvals=broker,
    )
    yield made
    await store.close()


def context(**over):
    session = StubSession(**over)
    return ToolContext(session=session, channel=session.channel, caller=session.caller)


async def test_the_tools_are_not_offered_without_a_broker(settings, tmp_path):
    store = TaskStore(tmp_path / "no-broker.db")
    made = ToolRegistry()
    register_builtin_tools(
        made,
        manager=TaskManager(store, FakeAgentRunner(), EventBus(), settings),
        settings=settings,
        inline_waits=InlineWaits(),
    )
    names = {schema["name"] for schema in made.schemas()}
    assert "answer_approval" not in names and "list_pending_approvals" not in names
    await store.close()


async def test_it_hands_back_the_menu_and_nothing_else(registry, broker):
    answer = await registry.call("answer_approval", {"request_id": 1}, context())
    assert answer["status"] == "awaiting_keypad"
    assert "press 1 for approve" in answer["options"]
    assert "keypad" in answer["message"]
    assert broker.armed == [(1, "call1")]


async def test_an_unauthorized_caller_gets_the_pin_gate(registry, broker):
    answer = await registry.call("answer_approval", {"request_id": 1}, context(authorized=False))
    assert answer["status"] == "pin_required"
    assert broker.armed == []


async def test_the_microphone_cannot_answer_an_approval(registry, broker):
    """There is no keypad on the Mac, and the keypad is the only way in."""
    answer = await registry.call(
        "answer_approval", {"request_id": 1}, context(channel="local", caller=None)
    )
    assert answer["status"] == "phone_only"
    assert broker.armed == []


async def test_a_request_id_that_is_not_a_number_is_refused(registry, broker):
    answer = await registry.call("answer_approval", {"request_id": "the first one"}, context())
    assert "error" in answer
    assert broker.armed == []


async def test_a_number_the_model_spelled_as_a_string_still_works(registry, broker):
    await registry.call("answer_approval", {"request_id": "2"}, context())
    assert broker.armed == [(2, "call1")]


async def test_a_request_that_has_gone_says_so(registry, broker):
    broker.answer = {"status": "gone", "message": "That request is not waiting any more."}
    answer = await registry.call("answer_approval", {"request_id": 1}, context())
    assert answer["status"] == "gone"
    assert "message" in answer


async def test_listing_says_plainly_when_nothing_is_waiting(registry):
    answer = await registry.call("list_pending_approvals", {}, context())
    assert answer["status"] == "none"


async def test_listing_hands_back_what_is_waiting(registry, broker):
    broker.waiting = [{"request_id": 1, "summary": "s", "options": "press 1 for approve"}]
    answer = await registry.call("list_pending_approvals", {}, context())
    assert answer["requests"] == broker.waiting


async def test_listing_needs_the_pin_because_a_waiting_command_is_private(registry, broker):
    """What their screen is waiting on names their projects and their commands."""
    broker.waiting = [{"request_id": 1, "summary": "git push", "options": "press 1"}]
    answer = await registry.call("list_pending_approvals", {}, context(authorized=False))
    assert answer["status"] == "pin_required"
    assert "git push" not in str(answer)
