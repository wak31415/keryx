"""Which voice session is holding the line for which task (spec §3.3).

`dispatch_task(wait_seconds=…)` blocks inside the tool call while a task finishes, and
hands the summary back as the tool result. The task manager publishes `TaskCompleted`
*before* that wait returns, so without this the Notifier would announce the very same
result into the very same session a moment before the tool result arrives — the user
hears it twice, and on the local channel gets a text about it as well.

The tool marks `(session_id, task_id)` for the duration of its wait and the Notifier
skips exactly that pair. One shared instance per process, wired in `app.py`.
"""

import contextlib
from collections.abc import Iterator


class InlineWaits:
    """The `(session_id, task_id)` pairs currently waiting inside `dispatch_task`."""

    def __init__(self) -> None:
        self._waits: set[tuple[str, int]] = set()

    def __contains__(self, wait: tuple[str, int]) -> bool:
        """True while that session is holding the line for that task."""
        return wait in self._waits

    @contextlib.contextmanager
    def holding(self, session_id: str, task_id: int) -> Iterator[None]:
        """Mark the wait for as long as the block runs; always cleared afterwards."""
        wait = (session_id, task_id)
        self._waits.add(wait)
        try:
            yield
        finally:
            self._waits.discard(wait)
