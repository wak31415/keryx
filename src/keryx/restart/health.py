"""Is it answering, and is it listening yet — the two questions asked from outside.

Both of these are read-only probes and neither belongs to the coordinator, even though
that is where they grew. `health_probe` is one localhost GET asking the *running* Keryx
how many calls are live, which is what decides whether a restart would cut somebody off
mid-sentence; `wait_until_serving` waits on a uvicorn server object before a call-back is
dialled, because the call-back is answered by our own `<Connect><Stream>` and ringing
before the socket is up rings into nothing.

They live here because of who asks them. `keryx restart` asks from a separate process
that has not started Keryx at all, and `restart/watchdog.py` asks on a machine where
Keryx may not import — and importing the coordinator for one `urllib` call brought the
whole application, `session` and the notifier included, with it.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from keryx.config import Settings

log = logging.getLogger("keryx.restart")

#: How long `resume()` waits for the phone server to start listening.
READY_TIMEOUT_S = 60.0


def health_probe(settings: Settings, *, timeout: float = 2.0) -> int | None:
    """How many sessions the running Keryx has live, or None if it is not answering.

    Used by `keryx restart` from *outside* the process: the answer is what decides whether
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
