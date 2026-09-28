"""Everything Jarvis sends *out* through Twilio: one SMS, one outbound call —
plus `TwilioAdmin`, the account calls `jarvis setup` and `jarvis doctor` make.

The Twilio helper library is synchronous, so every REST call goes through
`asyncio.to_thread` — a text or a call-back must never stall the event loop that is
also pumping somebody's audio.

Errors are deliberately not swallowed here: this is the thin edge, and the Notifier above
it is the one that decides a failed text must not cost the call-back. They are *reworded*,
though, into a `TwilioError` with every phone number masked: Twilio's own error text quotes
the number back ("The 'To' number … is not a valid phone number"), and every caller up the
stack logs a failure with its traceback.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from jarvis.config import Settings
from jarvis.logging_util import mask_number

log = logging.getLogger("jarvis.notify.twilio_out")

#: A number in the E.164 shape Twilio writes them in, wherever it turns up in its errors.
E164_NUMBER = re.compile(r"\+\d{7,15}")


class TwilioError(RuntimeError):
    """A Twilio REST call that failed, in words that carry no phone number."""


def _without_numbers(text: str, number: str) -> str:
    """`text` with `number`, and anything else shaped like one, masked."""
    if number:
        text = text.replace(number, mask_number(number))
    return E164_NUMBER.sub(lambda match: mask_number(match.group()), text)


def stream_twiml(public_host: str, params: dict[str, str]) -> str:
    """`<Connect><Stream>` TwiML with one `<Parameter>` per entry in `params`.

    The same shape the inbound webhook answers with: the media-stream URL
    cannot carry a query string, so anything the socket needs travels as a parameter.
    """
    response = VoiceResponse()
    stream = response.connect().stream(url=f"wss://{public_host}/twilio/media")
    for name, value in params.items():
        stream.parameter(name=name, value=value)
    return str(response)


def say_twiml(text: str, *, loop: int = 2) -> str:
    """TwiML that simply speaks `text`, with no media stream behind it.

    The `<Connect><Stream>` above needs our own phone server to answer it, which makes it
    exactly the wrong shape for the one call that matters most: the alert that says the
    service never came back. This one is hosted by Twilio and needs nothing of ours to be
    running. Said twice by default — a call answered mid-sentence loses the start of it.
    """
    response = VoiceResponse()
    response.say(text, loop=loop)
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
    def can_text(self) -> bool:
        """True when a text could actually go out — credentials *and* `SMS_ENABLED`.

        Separate from `configured` because calls and texts are not the same capability:
        with texting off, every outbound call still works, and the alert that matters most
        (the restart watchdog's) is a call.
        """
        return self.configured and self._settings.sms_enabled

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
        message = await self._create(
            self.client.messages.create, to, from_=self._settings.twilio_number, body=body
        )
        log.info("texted %s (message %s)", mask_number(to), message.sid)
        return message.sid

    async def place_call(self, to: str, *, twiml: str, status_callback: str | None = None) -> str:
        """Call `to` and answer it with `twiml`; returns the call sid."""
        kwargs: dict[str, Any] = {"from_": self._settings.twilio_number, "twiml": twiml}
        if status_callback:
            kwargs["status_callback"] = status_callback
        call = await self._create(self.client.calls.create, to, **kwargs)
        log.info("calling %s (call %s)", mask_number(to), call.sid)
        return call.sid

    async def _create(self, create: Callable[..., Any], to: str, **kwargs: Any) -> Any:
        """One REST `create` off the event loop; a failure comes back as a `TwilioError`.

        `from None`, because a chained traceback would print the original message — number
        and all — right above the reworded one.
        """
        try:
            return await asyncio.to_thread(create, to=to, **kwargs)
        except Exception as error:
            raise TwilioError(_without_numbers(str(error), to)) from None


# --- the account, for `jarvis setup` and `jarvis doctor` ----------------------------------


@dataclass(frozen=True)
class TwilioNumber:
    """One number on the account, and where its calls go now."""

    sid: str
    phone_number: str
    voice_url: str | None
    status_callback: str | None


class TwilioAdmin(Protocol):
    """The three account calls setup makes: who you are, your numbers, and one update."""

    def account_name(self) -> str: ...
    def numbers(self) -> list[TwilioNumber]: ...
    def set_webhooks(self, number_sid: str, *, voice_url: str, status_url: str) -> None: ...


class RestTwilioAdmin:
    """`TwilioAdmin` over the Twilio REST client. Synchronous: setup and doctor are.

    Reads, apart from `set_webhooks`, which `jarvis setup` calls only after the owner has
    said yes to exactly the two URLs it is about to write.
    """

    def __init__(self, account_sid: str, auth_token: str, client: Any | None = None) -> None:
        self._client = client or Client(account_sid, auth_token)
        self._sid = account_sid

    def account_name(self) -> str:
        try:
            return str(self._client.api.accounts(self._sid).fetch().friendly_name)
        except Exception as error:
            raise TwilioError(_without_numbers(str(error), "")) from None

    def numbers(self) -> list[TwilioNumber]:
        try:
            listed = self._client.incoming_phone_numbers.list(limit=50)
        except Exception as error:
            raise TwilioError(_without_numbers(str(error), "")) from None
        return [
            TwilioNumber(
                sid=number.sid,
                phone_number=number.phone_number,
                voice_url=number.voice_url or None,
                status_callback=number.status_callback or None,
            )
            for number in listed
        ]

    def set_webhooks(self, number_sid: str, *, voice_url: str, status_url: str) -> None:
        try:
            self._client.incoming_phone_numbers(number_sid).update(
                voice_url=voice_url,
                voice_method="POST",
                status_callback=status_url,
                status_callback_method="POST",
            )
        except Exception as error:
            raise TwilioError(_without_numbers(str(error), "")) from None
