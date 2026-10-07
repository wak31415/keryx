"""The phone: the owner's Twilio number, and pointing it at this machine.

One implementation behind both the wizard's phone section (`setup/phone.py`, which only asks
and tells) and `keryx phone`. Writing the webhooks is the one write either makes to anything
outside the machine, and both make it only after the owner has seen both addresses and said
yes — at the terminal, or in the Mac app.

A phone number here is the owner's own, shown to the owner; in a log line it is only ever
`mask_number`'s shape.
"""

import logging

from keryx.doctor import STATUS_PATH, VOICE_PATH
from keryx.logging_util import mask_number
from keryx.notify.twilio_out import TwilioAdmin, TwilioNumber

log = logging.getLogger("keryx.phone")


def webhook_urls(public_host: str) -> tuple[str, str]:
    """The voice webhook and the status callback, for a hostname."""
    return f"https://{public_host}{VOICE_PATH}", f"https://{public_host}{STATUS_PATH}"


def _same(url: str | None, want: str) -> bool:
    return (url or "").rstrip("/") == want


def matches(number: TwilioNumber, public_host: str) -> bool:
    """Whether both of `number`'s webhooks already point at `public_host`."""
    voice, status = webhook_urls(public_host)
    return _same(number.voice_url, voice) and _same(number.status_callback, status)


def point_here(admin: TwilioAdmin, number: TwilioNumber, public_host: str) -> None:
    """Point `number` at `public_host`: both webhooks, as POST. Raises `TwilioError`."""
    voice, status = webhook_urls(public_host)
    admin.set_webhooks(number.sid, voice_url=voice, status_url=status)
    log.info("pointed %s at %s", mask_number(number.phone_number), public_host)
