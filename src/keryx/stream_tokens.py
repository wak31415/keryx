"""One-time tokens that tie a Twilio media stream to the call that was authorized.

`<Stream url="…">` cannot carry a query string and the websocket handshake carries no
Twilio signature, so the media socket has no authentication of its own. Instead the
signature-validated `POST /twilio/voice` mints a short-lived random token, hands it to
Twilio as a `<Parameter>`, and the websocket must present it back exactly once.
Anything else — replayed, unknown or stale — never reaches a session.

The store is in-memory on purpose: a token outliving the process it was minted by would
be a liability, not a feature.

A token minted for a call *Keryx placed* carries one more fact, and it is the only thing
in Keryx allowed to carry it. Caller id on the way in is spoofable, which is why the PIN
exists; a number Keryx dialled on the way out is not, because reaching it means holding
that phone. `outbound_extra()` records both halves — that Keryx placed the call, and the
number it dialled — and `confers_possession()` is the one place the rule is applied: the
dialled number has to be one of `Settings.owner_numbers`, and never Twilio's own
`From`/`To` form fields, which the caller's carrier supplies.
"""

import logging
import secrets
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

log = logging.getLogger("keryx.stream_tokens")

TOKEN_TTL_SECONDS = 60.0
TOKEN_BYTES = 24

#: `extra` keys on a token for a call Keryx placed itself. Spelled once, here.
PLACED_BY_KERYX = "keryx_placed"
DIALLED_NUMBER = "dialled"


@dataclass(frozen=True)
class TokenInfo:
    """What a redeemed token says about the call it was minted for."""

    caller: str | None
    extra: dict = field(default_factory=dict)


def outbound_extra(number: str, **extra: object) -> dict:
    """The `extra` for a token on a call Keryx is placing itself, plus whatever else.

    Every outbound `<Connect><Stream>` goes through this, so there is one spelling of "we
    dialled this" and one place to look for who mints it.
    """
    return {PLACED_BY_KERYX: True, DIALLED_NUMBER: number, **extra}


def confers_possession(info: TokenInfo, owner_numbers: Collection[str]) -> bool:
    """True when this redeemed token proves the call reached a phone of the owner's.

    Both halves have to be there: Keryx placed the call, *and* the number it dialled is
    one of theirs (`Settings.owner_numbers` — every entry in the allowlist, because this
    is a single-owner agent and a second entry is a second handset, not a second person).
    A token that merely names a number — an inbound one, whose `caller` is the spoofable
    `From` — proves nothing, and with no number of theirs configured there is nothing to
    compare against, which is not a match.
    """
    if not info.extra.get(PLACED_BY_KERYX):
        return False
    dialled = info.extra.get(DIALLED_NUMBER)
    return bool(dialled) and dialled in (owner_numbers or ())


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
        self._entries[token] = _Entry(
            TokenInfo(caller=caller, extra=dict(extra or {})),
            expires_at=self._now() + ttl_s,
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
