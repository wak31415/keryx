"""The two voice tools, and the one thing they are not allowed to do.

`answer_approval` never answers anything. The most it can do is hand the model a menu to
read out; the digit that follows is the only thing in Keryx that can approve a tool call,
and it does not come through here. Both gates in front of the menu — the trust level and
the phone — are asserted below, because a spoken "yes" reaching an `allow` is the failure
this whole design exists to make impossible.
"""

from dataclasses import dataclass, field

import pytest

from keryx.config import Settings
from keryx.events import EventBus
from keryx.inline_waits import InlineWaits
from keryx.tasks.agent_runner import FakeAgentRunner
from keryx.tasks.manager import TaskManager
from keryx.tasks.store import TaskStore
from keryx.tools import ToolContext, ToolRegistry
from keryx.tools.builtin import register_builtin_tools
from keryx.trust import TrustLevel

PIN = "424242"


@dataclass
class StubSession:
    authorized: bool = True
    possession: bool = False
    keypressed: bool = False
    channel: str = "phone"
    caller: str | None = "+15557000000"
    session_id: str = "call1"

    @property
    def trust(self) -> TrustLevel:
        if self.channel != "phone" or self.authorized:
            return TrustLevel.FULL
        return TrustLevel.POSSESSION if self.possession else TrustLevel.NONE


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
            "summary": "Claude wants to run: git push, in keryx",
            "options": "press 1 for approve, press 2 for reject, or 0 to leave it on screen",
        }


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "keryx",
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


async def test_a_caller_who_has_proved_nothing_gets_the_pin_gate(registry, broker):
    answer = await registry.call("answer_approval", {"request_id": 1}, context(authorized=False))
    assert answer["status"] == "pin_required"
    assert broker.armed == []


async def test_a_call_keryx_placed_may_answer_one_without_the_pin(registry, broker):
    """The tier's point: the escalation call rings the owner's own phone, and
    `approvals/policy.py`'s allowlist is already the filter on what a key may run."""
    answer = await registry.call(
        "answer_approval", {"request_id": 1}, context(authorized=False, possession=True)
    )
    assert answer["status"] == "awaiting_keypad"
    assert broker.armed == [(1, "call1")]


async def test_the_menu_needs_no_keypress_of_its_own(registry, broker):
    """The answer is a keypress. Asking for one first would be asking twice."""
    await registry.call(
        "answer_approval",
        {"request_id": 1},
        context(authorized=False, possession=True, keypressed=False),
    )
    assert broker.armed == [(1, "call1")]


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


async def test_a_call_keryx_placed_may_hear_what_is_waiting(registry, broker):
    """It is usually the call the broker placed about one of them."""
    broker.waiting = [{"request_id": 1, "summary": "git push", "options": "press 1"}]
    answer = await registry.call(
        "list_pending_approvals", {}, context(authorized=False, possession=True)
    )
    assert answer["requests"] == broker.waiting
