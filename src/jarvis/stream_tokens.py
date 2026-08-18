"""One-time tokens that tie a Twilio media stream to the call that was authorized.

`<Stream url="…">` cannot carry a query string and the websocket handshake carries no
Twilio signature, so the media socket has no authentication of its own. Instead the
signature-validated `POST /twilio/voice` mints a short-lived random token, hands it to
Twilio as a `<Parameter>`, and the websocket must present it back exactly once (spec
§3.3, §5). Anything else — replayed, unknown or stale — never reaches a session.

The store is in-memory on purpose: a token outliving the process it was minted by would
be a liability, not a feature.
"""

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("jarvis.stream_tokens")

TOKEN_TTL_SECONDS = 60.0
TOKEN_BYTES = 24


@dataclass(frozen=True)
class TokenInfo:
    """What a redeemed token says about the call it was minted for."""

    caller: str | None
    extra: dict = field(default_factory=dict)
    issued_at: float = 0.0


@dataclass(frozen=True)
class _Entry:
    info: TokenInfo
    expires_at: float


class StreamTokenStore:
    """Single-use, TTL-bounded stream tokens. `now` is injectable for tests."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._entries: dict[str, _Entry] = {}

    def __len__(self) -> int:
        """How many unredeemed, unexpired tokens are outstanding."""
        self._purge()
        return len(self._entries)

    def issue(
        self,
        caller: str | None,
        extra: dict | None = None,
        ttl_s: float = TOKEN_TTL_SECONDS,
    ) -> str:
        """Mint a token for `caller`, valid once, for `ttl_s` seconds."""
        self._purge()
        token = secrets.token_urlsafe(TOKEN_BYTES)
        issued_at = self._now()
        self._entries[token] = _Entry(
            TokenInfo(caller=caller, extra=dict(extra or {}), issued_at=issued_at),
            expires_at=issued_at + ttl_s,
        )
        return token

    def redeem(self, token: str) -> TokenInfo | None:
        """Consume `token` and return what it stood for, or None if it is not valid."""
        self._purge()
        entry = self._entries.pop(token, None)
        if entry is None:
            log.warning("rejected an unknown or already-used stream token")
            return None
        return entry.info

    def _purge(self) -> None:
        """Drop everything that has timed out; nothing else ever removes entries."""
        now = self._now()
        stale = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in stale:
            del self._entries[token]
