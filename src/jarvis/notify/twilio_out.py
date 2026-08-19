"""Everything Jarvis sends *out* through Twilio: one SMS, one outbound call (spec §4).

The Twilio helper library is synchronous, so every REST call goes through
`asyncio.to_thread` — a text or a call-back must never stall the event loop that is
also pumping somebody's audio.

Errors are deliberately not caught here: this is the thin edge, and the Notifier above
it is the one that decides a failed text must not cost the call-back.
"""

import asyncio
import logging
from typing import Any

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from jarvis.config import Settings

log = logging.getLogger("jarvis.notify.twilio_out")


def stream_twiml(public_host: str, params: dict[str, str]) -> str:
    """`<Connect><Stream>` TwiML with one `<Parameter>` per entry in `params`.

    The same shape the inbound webhook answers with (spec §3.3): the media-stream URL
    cannot carry a query string, so anything the socket needs travels as a parameter.
    """
    response = VoiceResponse()
    stream = response.connect().stream(url=f"wss://{public_host}/twilio/media")
    for name, value in params.items():
        stream.parameter(name=name, value=value)
    return str(response)


class TwilioOut:
    """Outbound Twilio, with the REST client injectable (and never built in tests)."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def configured(self) -> bool:
        """True when there are credentials *and* a number to send from."""
        settings = self._settings
        return bool(
            settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_number
        )

    @property
    def client(self) -> Any:
        """The REST client, built on first use so an unconfigured process never makes one."""
        if self._client is None:
            self._client = Client(
                self._settings.twilio_account_sid, self._settings.twilio_auth_token
            )
        return self._client

    async def send_sms(self, to: str, body: str) -> str:
        """Text `body` to `to` from the configured number; returns the message sid."""
        message = await asyncio.to_thread(
            self.client.messages.create, from_=self._settings.twilio_number, to=to, body=body
        )
        log.info("texted %s (message %s)", to, message.sid)
        return message.sid

    async def place_call(self, to: str, *, twiml: str, status_callback: str | None = None) -> str:
        """Call `to` and answer it with `twiml`; returns the call sid."""
        kwargs: dict[str, Any] = {
            "to": to,
            "from_": self._settings.twilio_number,
            "twiml": twiml,
        }
        if status_callback:
            kwargs["status_callback"] = status_callback
        call = await asyncio.to_thread(self.client.calls.create, **kwargs)
        log.info("calling %s (call %s)", to, call.sid)
        return call.sid
