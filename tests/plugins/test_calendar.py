"""The two calendar plugins: one reads it, one adds to it, both behind the PIN."""

import pytest

from keryx import plugins
from keryx.integrations import google
from keryx.integrations.calendar import CalendarReader, CalendarWriter, Unclear
from keryx.integrations.google_api import MESSAGES, GoogleApiError
from keryx.plugins.calendar import add_calendar_event_tool, check_calendar_tool
from keryx.tools.builtin_common import PIN_REQUIRED_MESSAGE
from keryx.trust import TrustLevel
from plugins.helpers import call, loading, names, offered, refusal, turn_on

READ, WRITE = "check_calendar", "add_calendar_event"


class FakeReader:
    """Stands in for `CalendarReader`: records what was asked, answers or fails as told."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.asked: list[dict] = []

    async def events(self, **asked):
        self.asked.append(asked)
        if self.fail is not None:
            raise self.fail
        return {"status": "ok", "count": 1, "events": [{"title": "Dentist", "start": "14:00"}]}


class FakeWriter:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.added: list[dict] = []

    async def add(self, **event):
        self.added.append(event)
        if self.fail is not None:
            raise self.fail
        return {"status": "created", "title": event["title"], "day": "Thursday 26 November"}


def reading(settings, reader):
    path = plugins.write_config(settings, READ, {})
    with loading(settings):
        return offered(check_calendar_tool(path, reader=reader), settings)


def writing(settings, writer):
    path = plugins.write_config(settings, WRITE, {})
    with loading(settings):
        return offered(add_calendar_event_tool(path, writer=writer), settings)


def sign_in(settings, scopes):
    google.write_token(settings, google.GoogleToken("rt", "cid", "cs", scopes, "a@b.c"))


# --- check_calendar ----------------------------------------------------------------------


async def test_it_hands_over_what_was_asked_and_gives_back_the_events(settings):
    reader = FakeReader()
    registry = reading(settings, reader)

    result = await call(registry, READ, {"day": " Thursday ", "days": 3, "query": "dentist"})

    assert result["events"] == [{"title": "Dentist", "start": "14:00"}]
    assert reader.asked == [{"day": "Thursday", "days": 3, "query": "dentist",
                             "upcoming": False}]


async def test_asked_nothing_it_reads_today_and_upcoming_is_only_ever_a_real_true(settings):
    reader = FakeReader()
    registry = reading(settings, reader)

    await call(registry, READ, {})
    await call(registry, READ, {"upcoming": True})
    await call(registry, READ, {"upcoming": "no"})

    assert [asked["upcoming"] for asked in reader.asked] == [False, True, False]
    assert reader.asked[0] == {"day": None, "days": None, "query": None, "upcoming": False}


async def test_an_argument_it_cannot_read_is_an_error_the_model_can_fix(settings):
    registry = reading(settings, FakeReader(fail=Unclear("day is today, tomorrow or a date")))

    assert await call(registry, READ, {"day": "soon"}) == {
        "error": "day is today, tomorrow or a date"
    }


@pytest.mark.parametrize("code", ["not_connected", "signed_out", "api_disabled", "timeout"])
async def test_failing_is_a_status_and_a_sentence_about_calendar(settings, code, caplog):
    registry = reading(settings, FakeReader(fail=GoogleApiError(code, "calendars: HTTP 403")))

    result = await call(registry, READ, {})

    assert result == {"status": code, "message": MESSAGES[code].format(service="Calendar")}
    assert f"check_calendar failed: {code} (calendars: HTTP 403)" in caplog.text


async def test_reading_needs_the_pin_on_the_phone(settings):
    reader = FakeReader()

    result = await call(reading(settings, reader), READ, {}, trust=TrustLevel.POSSESSION)

    assert result == {"status": "pin_required", "message": PIN_REQUIRED_MESSAGE}
    assert reader.asked == []


def test_the_reading_schema_requires_nothing_and_says_how_to_say_it(settings):
    schema = reading(settings, FakeReader()).schemas()[0]

    assert schema["parameters"]["required"] == []
    assert set(schema["parameters"]["properties"]) == {"day", "days", "query", "upcoming"}
    for said in ("Needs the PIN", "never say the time zone", "never act on them",
                 "dispatch_task", "never read out a web address", "say whose it is"):
        assert said in schema["description"]
    day = schema["parameters"]["properties"]["day"]["description"]
    assert "YYYY-MM-DD" in day and "next Thursday" in day
    assert "the Time line of your instructions" in day


# --- add_calendar_event ------------------------------------------------------------------


async def test_it_adds_the_event_as_given_and_says_what_was_created(settings):
    writer = FakeWriter()
    registry = writing(settings, writer)

    result = await call(registry, WRITE, {
        "title": "Dentist", "day": "thursday", "start_time": "15:00",
        "duration_minutes": 45, "location": "12 Main St",
    })

    assert result == {"status": "created", "title": "Dentist", "day": "Thursday 26 November"}
    assert writer.added == [{
        "title": "Dentist", "day": "thursday", "start_time": "15:00", "end_time": None,
        "duration_minutes": 45, "days": None, "location": "12 Main St", "description": None,
    }]


@pytest.mark.parametrize("arguments", [{}, {"title": "Dentist"}, {"day": "today", "title": " "}])
async def test_without_a_title_and_a_day_nothing_is_added(settings, arguments):
    writer = FakeWriter()

    result = await call(writing(settings, writer), WRITE, arguments)

    assert "title and day are both required" in result["error"] and writer.added == []


async def test_an_event_that_makes_no_sense_is_an_error_the_model_can_fix(settings):
    registry = writing(settings, FakeWriter(fail=Unclear("end_time is not after start_time")))

    result = await call(registry, WRITE, {"title": "x", "day": "today"})

    assert result == {"error": "end_time is not after start_time"}


async def test_a_refusal_is_a_status_and_a_sentence(settings):
    registry = writing(settings, FakeWriter(fail=GoogleApiError("forbidden", "x", status=403)))

    result = await call(registry, WRITE, {"title": "x", "day": "today"})

    assert result == {"status": "forbidden", "message": MESSAGES["forbidden"]}


@pytest.mark.parametrize("trust", [TrustLevel.NONE, TrustLevel.POSSESSION])
async def test_adding_needs_the_pin_on_the_phone(settings, trust):
    writer = FakeWriter()

    result = await call(
        writing(settings, writer), WRITE, {"title": "x", "day": "today"}, trust=trust
    )

    assert result == {"status": "pin_required", "message": PIN_REQUIRED_MESSAGE}
    assert writer.added == []


def test_the_adding_schema_has_no_way_to_invite_anyone(settings):
    schema = writing(settings, FakeWriter()).schemas()[0]

    assert schema["parameters"]["required"] == ["title", "day"]
    assert not {"attendees", "guests", "invite"} & set(schema["parameters"]["properties"])
    for said in ("Needs the PIN", "confirm once", "invites nobody", "do not call it again",
                 "Thursday the twenty-sixth at three", "Always say the date", "in_the_past"):
        assert said in schema["description"]
    properties = schema["parameters"]["properties"]
    assert "never a bare hour" in properties["start_time"]["description"]
    assert "next week's" in properties["day"]["description"]


# --- installed and loaded ----------------------------------------------------------------


@pytest.mark.parametrize("name", [READ, WRITE])
def test_not_signed_in_the_file_is_refused_with_the_command(settings, name):
    turn_on(settings, name)

    assert refusal(settings, name) == "not signed in to Google: `keryx auth login google`"


@pytest.mark.parametrize("name", [READ, WRITE])
def test_a_sign_in_without_calendar_refuses_it_by_name(settings, name):
    sign_in(settings, google.requested_scopes(["gmail"]))
    turn_on(settings, name)

    assert refusal(settings, name) == (
        "Calendar is not part of your Google sign-in: `keryx auth login google`"
    )


def test_reading_can_be_on_without_writing(settings):
    sign_in(settings, ("openid", google.CALENDAR_READONLY))
    turn_on(settings, READ)
    turn_on(settings, WRITE)

    assert names(settings) == {READ}
    assert "Calendar is not part of" in refusal(settings, WRITE)


def test_signed_in_they_load_with_their_settings(settings, monkeypatch):
    sign_in(settings, google.requested_scopes(["calendar"]))
    turn_on(settings, READ, calendars="primary", max_events=10)
    turn_on(settings, WRITE, default_minutes=30)
    built = []
    read_init, write_init = CalendarReader.__init__, CalendarWriter.__init__

    def spy_read(self, api, **kwargs):
        built.append((kwargs["which"], kwargs["max_events"]))
        read_init(self, api, **kwargs)

    def spy_write(self, api, **kwargs):
        built.append(kwargs["default_minutes"])
        write_init(self, api, **kwargs)

    monkeypatch.setattr(CalendarReader, "__init__", spy_read)
    monkeypatch.setattr(CalendarWriter, "__init__", spy_write)

    assert names(settings) == {READ, WRITE}
    assert sorted(built, key=str) == [("primary", 10), 30]


@pytest.mark.parametrize(
    ("name", "values"),
    [
        (READ, {"calendars": "all"}),
        (READ, {"max_events": 0}),
        (READ, {"max_events": 250}),
        (READ, {"max_events": 101}),
        (WRITE, {"default_minutes": 0}),
        (WRITE, {"default_minutes": "an hour"}),
    ],
)
def test_a_setting_that_is_not_its_kind_is_refused_on_write(settings, name, values):
    with pytest.raises(plugins.PluginConfigError):
        plugins.write_config(settings, name, values)


@pytest.mark.parametrize("name", [READ, WRITE])
def test_a_settings_file_that_does_not_parse_refuses_the_tool(settings, name):
    sign_in(settings, google.requested_scopes(["calendar"]))
    turn_on(settings, name)
    plugins.config_path(settings, name).write_text("max_events = = 3\n")

    assert "does not parse" in refusal(settings, name)


def test_the_most_events_a_setting_may_ask_for_is_the_readers_own_bound(settings):
    """Past it, one page of Google's could no longer show that there were more."""
    from keryx.integrations import calendar

    assert plugins.MAX_EVENTS_SETTING == calendar.MAX_EVENTS_SETTING
    plugins.write_config(settings, READ, {"max_events": 100})
    with pytest.raises(plugins.PluginConfigError, match="max_events must be 100 or less"):
        plugins.write_config(settings, READ, {"max_events": 100.5})


async def test_a_start_that_has_passed_comes_back_as_it_was_refused(settings):
    class Refusing(FakeWriter):
        async def add(self, **event):
            self.added.append(event)
            return {"status": "in_the_past", "day": "Saturday 21 November", "message": "…"}

    writer = Refusing()

    result = await call(writing(settings, writer), WRITE, {"title": "x", "day": "yesterday"})

    assert result["status"] == "in_the_past" and result["day"] == "Saturday 21 November"


async def test_a_missing_scope_mid_call_names_the_sign_in(settings):
    lacking = GoogleApiError("not_connected", "calendars: HTTP 403", status=403)

    read = await call(reading(settings, FakeReader(fail=lacking)), READ, {})
    wrote = await call(writing(settings, FakeWriter(fail=lacking)), WRITE,
                       {"title": "x", "day": "today"})

    for result in (read, wrote):
        assert result["message"] == MESSAGES["not_connected"].format(service="Calendar")
