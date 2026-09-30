"""A scripted `AgentAdapter`: the one double every backend's session tests can share."""

import asyncio
from collections.abc import Iterable, Sequence

from keryx.agents.base import SteerUnavailable
from keryx.agents.session import AgentEvent

#: A turn step that never finishes, as a stream blocked in its reader does.
BLOCK = "block"
#: What `steer()` raises unless told otherwise: an agent with no live steer.
NO_STEER = SteerUnavailable("no live steer")


class ScriptedAdapter:
    """Plays one scripted list of events per turn; records everything asked of it.

    A step that is an exception is raised where it stands; `BLOCK` waits for ever. `steer`
    is what `steer()` does: None records the text, an exception is raised.
    """

    def __init__(
        self,
        *turns: Sequence[AgentEvent | BaseException | str],
        secrets: Iterable[str | None] = (),
        steer: BaseException | None = NO_STEER,
        fail_on: Iterable[str] = (),
    ) -> None:
        self.turns = [list(turn) for turn in turns]
        self.secrets = list(secrets)
        self._steer = steer
        self.fail_on = set(fail_on)
        self.prompts: list[str] = []
        self.steered: list[str] = []
        self.interrupts = 0
        self.closes = 0
        self.turns_closed = 0

    async def turn(self, prompt: str):
        self.prompts.append(prompt)
        try:
            for step in self.turns.pop(0):
                if isinstance(step, BaseException):
                    raise step
                if step == BLOCK:
                    await asyncio.Event().wait()
                yield step
        finally:
            self.turns_closed += 1

    async def steer(self, text: str) -> None:
        if self._steer is not None:
            raise self._steer
        self.steered.append(text)

    async def interrupt(self) -> None:
        self.interrupts += 1
        if "interrupt" in self.fail_on:
            raise RuntimeError(f"cannot interrupt: {self.secrets}")

    async def close(self) -> None:
        self.closes += 1
        if "close" in self.fail_on:
            raise RuntimeError(f"cannot close: {self.secrets}")
