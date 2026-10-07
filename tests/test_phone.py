"""`keryx.phone`: the owner's Twilio numbers, where they point, and the tunnel — the one
implementation behind the wizard's phone section and `keryx phone`.

Twilio is a fake admin and the network a fake getter: nothing here leaves the process.
"""

import logging
from dataclasses import dataclass, field

from keryx import phone
from keryx.notify.twilio_out import TwilioError, TwilioNumber

HOST = "keryx.example.com"
VOICE = f"https://{HOST}/twilio/voice"
STATUS = f"https://{HOST}/twilio/status"
NUMBER = "+15550001111"


@dataclass
class FakeAdmin:
    """A Twilio account: its name, its numbers, and every update it was asked for. `error`
    is raised by every call, `write_error` by the update alone."""

    numbers_: list[TwilioNumber] = field(
        default_factory=lambda: [TwilioNumber("PN1", NUMBER, None, None)]
    )
    name: str = "Ada's account"
    error: TwilioError | None = None
    write_error: TwilioError | None = None
    updates: list[tuple[str, str, str]] = field(default_factory=list)

    def account_name(self) -> str:
        if self.error:
            raise self.error
        return self.name

    def numbers(self) -> list[TwilioNumber]:
        if self.error:
            raise self.error
        return list(self.numbers_)

    def set_webhooks(self, number_sid: str, *, voice_url: str, status_url: str) -> None:
        if self.error or self.write_error:
            raise self.error or self.write_error
        self.updates.append((number_sid, voice_url, status_url))


# --- where a number points ---------------------------------------------------------------


def test_the_webhooks_for_a_hostname():
    assert phone.webhook_urls(HOST) == (VOICE, STATUS)


def test_a_number_matches_only_when_both_webhooks_point_here():
    assert phone.matches(TwilioNumber("PN1", NUMBER, VOICE, STATUS), HOST)
    assert phone.matches(TwilioNumber("PN1", NUMBER, VOICE + "/", STATUS + "/"), HOST)
    assert not phone.matches(TwilioNumber("PN1", NUMBER, VOICE, None), HOST)
    assert not phone.matches(TwilioNumber("PN1", NUMBER, "https://old.example/v", STATUS), HOST)
    assert not phone.matches(TwilioNumber("PN1", NUMBER, None, None), HOST)


def test_pointing_a_number_here_writes_both_webhooks_and_logs_it_masked(caplog):
    admin = FakeAdmin()

    with caplog.at_level(logging.INFO, logger="keryx.phone"):
        phone.point_here(admin, TwilioNumber("PN1", NUMBER, None, None), HOST)

    assert admin.updates == [("PN1", VOICE, STATUS)]
    assert NUMBER not in caplog.text
    assert "1111" in caplog.text
