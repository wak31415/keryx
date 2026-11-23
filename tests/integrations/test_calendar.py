"""The calendar client: a stretch of days read out, and one event added.

No network: Google Calendar is a fake keyed by API path. Every calendar, event and person
in here is invented, and the times asserted on were worked out by hand from the fixtures.
"""

import asyncio
from datetime import UTC, date, datetime, time
from urllib.parse import unquote

import pytest

from keryx.integrations import calendar, google
from keryx.integrations.calendar import (
    CREATED_MESSAGE,
    MORE_NOTE,
    NOTHING_FOUND,
    TEXT_NOTE,
    UNCONFIRMED_MESSAGE,
    CalendarReader,
    CalendarWriter,
    Unclear,
    build_calendar_reader,
    build_calendar_writer,
    clip,
    parse_time,
    read_problem,
    resolve_day,
    say_day,
    say_time,
    write_problem,
)
from keryx.integrations.google_api import GoogleApiError

#: Sunday 22 November 2026, 18:30 in New York (23:30 UTC).
NOW = datetime(2026, 11, 22, 23, 30, tzinfo=UTC)
NEW_YORK = "America/New_York"
HOLIDAYS = "en.usa#holiday@group.v.calendar.google.com"

OWN = {"id": "ada@example.org", "summary": "Ada", "primary": True, "selected": True,
       "timeZone": NEW_YORK}
TEAM = {"id": "team@example.org", "summary": "Team", "selected": True, "timeZone": "UTC"}
LISTING = {"items": [
    TEAM,
    OWN,
    {"id": HOLIDAYS, "summary": "Holidays in the US", "summaryOverride": "Holidays",
     "selected": True},
    {"id": "unticked@example.org", "summary": "Unticked"},
    {"id": "hidden@example.org", "summary": "Hidden", "selected": True, "hidden": True},
]}


def timed(title, start, end, **extra) -> dict:
    return {"summary": title, "iCalUID": f"{title}@x", "start": {"dateTime": start},
            "end": {"dateTime": end}, **extra}


def all_day(title, start, end, **extra) -> dict:
    return {"summary": title, "iCalUID": f"{title}@x", "start": {"date": start},
            "end": {"date": end}, **extra}


class FakeCalendar:
    """Calendar by path: the calendar list, and each calendar's events by its id."""

    def __init__(self, events=None, *, listing=LISTING, fail=None, delay=0.0, zone=NEW_YORK):
        self.events = events or {}
        self.listing = listing
        self.fail = fail or {}
        self.delay = delay
        self.zone = zone
        self.calls: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict, dict]] = []

    async def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        await asyncio.sleep(self.delay)
        if path == "users/me/calendarList":
            return self.listing
        calendar_id = unquote(path.split("/")[1])
        if calendar_id in self.fail:
            raise self.fail[calendar_id]
        return {"timeZone": self.zone, "items": self.events.get(calendar_id, [])}

    async def post(self, path, body, params=None):
        self.posts.append((path, dict(body), dict(params or {})))
        await asyncio.sleep(self.delay)
        if "post" in self.fail:
            raise self.fail["post"]
        return {"id": "made"}

    def asked(self) -> list[str]:
        return [path for path, _ in self.calls]

    def window(self) -> tuple[str, str]:
        params = next(params for path, params in self.calls if path.endswith("/events"))
        return params["timeMin"], params["timeMax"]


def reader(api, **kw) -> CalendarReader:
    return CalendarReader(api, now=lambda: NOW, **kw)


def writer(api, **kw) -> CalendarWriter:
    return CalendarWriter(api, now=lambda: NOW, **kw)


# -------------------------------------------------------------------- days and times


TODAY = date(2026, 11, 22)  # a Sunday


@pytest.mark.parametrize(
    ("said", "day"),
    [
        ("today", date(2026, 11, 22)),
        (" Tomorrow ", date(2026, 11, 23)),
        ("yesterday", date(2026, 11, 21)),
        ("sunday", date(2026, 11, 22)),
        ("Thursday", date(2026, 11, 26)),
        ("sat", date(2026, 11, 28)),
        ("2026-12-03", date(2026, 12, 3)),
        ("2026-12-03T09:00:00", date(2026, 12, 3)),
    ],
)
def test_a_day_is_taken_the_way_it_is_said(said, day):
    assert resolve_day(said, TODAY) == day


@pytest.mark.parametrize("said", ["", "next week", "the 3rd", "2026-13-40", "thursdayish"])
def test_a_day_that_is_not_one_says_what_is_wanted(said):
    with pytest.raises(Unclear, match="today, tomorrow, yesterday, a weekday"):
        resolve_day(said, TODAY)


@pytest.mark.parametrize(
    ("said", "at"),
    [
        ("14:30", time(14, 30)),
        ("9", time(9)),
        ("09.05", time(9, 5)),
        ("2:30 pm", time(14, 30)),
        ("12am", time(0)),
        ("12 p.m.", time(12)),
        ("noon", time(12)),
        ("midnight", time(0)),
    ],
)
def test_a_time_is_read_on_either_clock(said, at):
    assert parse_time(said) == at


@pytest.mark.parametrize("said", ["", "25:00", "9:75", "13pm", "afternoon", "3ish"])
def test_a_time_that_is_not_one_says_what_is_wanted(said):
    with pytest.raises(Unclear, match="24-hour clock"):
        parse_time(said)


def test_days_and_times_are_written_to_be_said():
    assert say_day(date(2026, 11, 23), TODAY) == "Monday 23 November"
    assert say_day(date(2027, 1, 4), TODAY) == "Monday 4 January 2027"
    assert say_time(time(14, 5), "24h") == "14:05"
    assert say_time(time(14, 5), "12h") == "2:05 PM"
    assert say_time(time(0, 30), "12h") == "12:30 AM"


def test_somebody_elses_text_is_one_clipped_line():
    assert clip("  Budget\n\nreview\t now ") == "Budget review now"
    assert clip("x" * 500) == "x" * 119 + "…"
    assert clip(None) == ""


# ---------------------------------------------------------------------------- reading


async def test_today_is_the_day_where_their_calendar_is_not_where_the_machine_is():
    api = FakeCalendar()

    result = await reader(api).events()

    assert api.window() == ("2026-11-22T00:00:00-05:00", "2026-11-23T00:00:00-05:00")
    assert (result["today"], result["now"]) == ("Sunday 22 November", "18:30")
    assert result["timezone"] == NEW_YORK and result["from"] == "Sunday 22 November"
    assert "to" not in result

    tokyo = {"items": [{**OWN, "timeZone": "Asia/Tokyo"}]}
    api = FakeCalendar(listing=tokyo)
    result = await reader(api).events()
    assert api.window() == ("2026-11-23T00:00:00+09:00", "2026-11-24T00:00:00+09:00")
    assert result["today"] == "Monday 23 November"


async def test_every_ticked_calendar_is_read_their_own_first():
    api = FakeCalendar()

    await reader(api).events()

    assert api.asked() == [
        "users/me/calendarList",
        "calendars/primary/events",
        "calendars/team%40example.org/events",
        "calendars/en.usa%23holiday%40group.v.calendar.google.com/events",
    ]
    _, params = api.calls[1]
    assert params["singleEvents"] == "true" and params["orderBy"] == "startTime"
    assert params["timeZone"] == NEW_YORK and "q" not in params


async def test_with_calendars_primary_only_their_own_is_read():
    api = FakeCalendar()

    await reader(api, which="primary").events()

    assert api.asked() == ["users/me/calendarList", "calendars/primary/events"]


async def test_the_calendar_list_is_asked_for_once_a_call():
    api = FakeCalendar()
    one = reader(api)

    await one.events()
    await one.events(day="tomorrow")

    assert api.asked().count("users/me/calendarList") == 1


async def test_events_come_back_in_order_each_as_something_to_say():
    api = FakeCalendar({
        "primary": [
            timed("Dentist", "2026-11-23T14:00:00-05:00", "2026-11-23T15:00:00-05:00",
                  location="12 Main St\nSuite 4"),
            all_day("Rent due", "2026-11-23", "2026-11-24"),
        ],
        "team@example.org": [
            timed("Design review", "2026-11-23T15:00:00Z", "2026-11-23T16:30:00Z",
                  attendees=[{"self": True, "responseStatus": "tentative"}, {}, {}]),
        ],
    })

    result = await reader(api).events(day="tomorrow")

    assert result["count"] == 3 and result["note"] == TEXT_NOTE
    assert result["events"] == [
        {"title": "Rent due", "day": "Monday 23 November", "all_day": True},
        {"title": "Design review", "day": "Monday 23 November", "start": "10:00",
         "end": "11:30", "calendar": "Team", "attendees": 3, "response": "maybe"},
        {"title": "Dentist", "day": "Monday 23 November", "start": "14:00", "end": "15:00",
         "location": "12 Main St Suite 4"},
    ]


async def test_the_twelve_hour_clock_is_the_owners_setting():
    api = FakeCalendar({"primary": [
        timed("Dentist", "2026-11-22T14:00:00-05:00", "2026-11-22T15:05:00-05:00"),
    ]})

    result = await reader(api, clock="12h").events()

    assert (result["now"], result["events"][0]["start"], result["events"][0]["end"]) == (
        "6:30 PM", "2:00 PM", "3:05 PM",
    )


async def test_events_that_run_past_their_day_say_until_when():
    api = FakeCalendar({"primary": [
        all_day("Conference", "2026-11-23", "2026-11-26"),
        timed("Night shift", "2026-11-23T22:00:00-05:00", "2026-11-24T06:00:00-05:00"),
    ]})

    result = await reader(api).events(day="monday", days=3)

    assert result["to"] == "Wednesday 25 November"
    assert result["events"][0] == {"title": "Conference", "day": "Monday 23 November",
                                   "all_day": True, "until": "Wednesday 25 November"}
    assert result["events"][1]["end_day"] == "Tuesday 24 November"


async def test_what_is_not_an_event_to_them_is_left_out():
    api = FakeCalendar({"primary": [
        timed("Cancelled", "2026-11-22T09:00:00-05:00", "2026-11-22T10:00:00-05:00",
              status="cancelled"),
        all_day("Home", "2026-11-22", "2026-11-23", eventType="workingLocation"),
        {"summary": "No times at all"},
        {"start": {"dateTime": "2026-11-22T11:00:00-05:00"},
         "end": {"dateTime": "2026-11-22T12:00:00-05:00"}},
    ]})

    result = await reader(api).events()

    assert [e["title"] for e in result["events"]] == ["(no title)"]


async def test_the_same_event_on_two_calendars_is_said_once_as_their_own():
    shared = timed("Standup", "2026-11-22T09:00:00-05:00", "2026-11-22T09:15:00-05:00")
    api = FakeCalendar({"primary": [shared], "team@example.org": [dict(shared)]})

    result = await reader(api).events()

    assert result["events"] == [
        {"title": "Standup", "day": "Sunday 22 November", "start": "09:00", "end": "09:15"}
    ]


async def test_their_answer_to_an_invitation_is_said_only_when_it_was_not_yes():
    def invited(title, status):
        return timed(title, "2026-11-22T09:00:00-05:00", "2026-11-22T10:00:00-05:00",
                     attendees=[{"self": True, "responseStatus": status}, {}])

    api = FakeCalendar({"primary": [
        invited("a", "accepted"), invited("b", "declined"), invited("c", "needsAction"),
    ]})

    result = await reader(api).events()

    assert [e.get("response") for e in result["events"]] == [None, "declined", "not answered"]


async def test_a_search_is_googles_and_looks_ninety_days_ahead_without_a_day():
    api = FakeCalendar({"primary": [
        timed("Dentist", "2026-12-08T14:00:00-05:00", "2026-12-08T15:00:00-05:00"),
    ]})

    result = await reader(api).events(query="  the   dentist ")

    assert api.calls[1][1]["q"] == "the dentist"
    assert api.window() == ("2026-11-22T00:00:00-05:00", "2027-02-20T00:00:00-05:00")
    assert result["searched"] == "the dentist"
    assert result["events"][0]["day"] == "Tuesday 8 December"
    assert result["to"] == "Friday 19 February 2027"


async def test_a_search_with_a_day_keeps_to_that_day_and_one_that_misses_says_so():
    api = FakeCalendar()

    result = await reader(api).events(query="dentist", day="tomorrow")

    assert api.window() == ("2026-11-23T00:00:00-05:00", "2026-11-24T00:00:00-05:00")
    assert result["count"] == 0 and result["message"] == NOTHING_FOUND
    assert "note" not in result


async def test_an_empty_day_is_a_count_of_nothing_and_no_search_sentence():
    result = await reader(FakeCalendar()).events()

    assert result["count"] == 0 and result["events"] == [] and "message" not in result


async def test_what_is_next_starts_now_and_leaves_out_what_has_begun_and_all_day():
    api = FakeCalendar({"primary": [
        all_day("Birthday", "2026-11-22", "2026-11-23"),
        timed("Already on", "2026-11-22T18:00:00-05:00", "2026-11-22T19:00:00-05:00"),
        timed("Dinner", "2026-11-22T19:30:00-05:00", "2026-11-22T21:00:00-05:00"),
        timed("Standup", "2026-11-23T09:00:00-05:00", "2026-11-23T09:15:00-05:00"),
        timed("Review", "2026-11-23T10:00:00-05:00", "2026-11-23T11:00:00-05:00"),
        timed("Lunch", "2026-11-23T12:00:00-05:00", "2026-11-23T13:00:00-05:00"),
    ]})

    result = await reader(api).events(upcoming=True)

    assert api.window() == ("2026-11-22T18:30:00-05:00", "2026-12-06T18:30:00-05:00")
    assert [e["title"] for e in result["events"]] == ["Dinner", "Standup", "Review"]
    assert result["from"] == "now" and "more" not in result


async def test_more_than_the_cap_is_cut_and_said():
    events = [
        timed(f"Event {n}", f"2026-11-22T{n + 8:02d}:00:00-05:00",
              f"2026-11-22T{n + 8:02d}:30:00-05:00")
        for n in range(5)
    ]
    api = FakeCalendar({"primary": events})

    result = await reader(api, max_events=3).events()

    assert api.calls[1][1]["maxResults"] == 4
    assert result["count"] == 3 and result["more"] == MORE_NOTE.format(count=3)
    assert [e["title"] for e in result["events"]] == ["Event 0", "Event 1", "Event 2"]


@pytest.mark.parametrize("days", [0, 367, "a week", -1])
async def test_a_stretch_that_is_not_a_number_of_days_is_refused(days):
    with pytest.raises(Unclear, match="days is"):
        await reader(FakeCalendar()).events(days=days)


async def test_a_day_that_is_not_one_is_refused_after_only_the_calendar_list():
    api = FakeCalendar()

    with pytest.raises(Unclear):
        await reader(api).events(day="soon")

    assert api.asked() == ["users/me/calendarList"]


async def test_a_calendar_that_has_gone_away_is_one_fewer_and_said():
    gone = GoogleApiError("not_found", "calendars: HTTP 404", status=404)
    api = FakeCalendar(
        {"primary": [timed("Dentist", "2026-11-22T14:00:00-05:00", "2026-11-22T15:00:00-05:00")]},
        fail={"team@example.org": gone},
    )

    result = await reader(api).events()

    assert result["count"] == 1 and result["calendars_not_read"] == 1


async def test_their_own_calendar_failing_is_the_answer_failing():
    api = FakeCalendar(fail={"primary": GoogleApiError("api_disabled", "x", status=403)})

    with pytest.raises(GoogleApiError) as raised:
        await reader(api).events()

    assert raised.value.code == "api_disabled"


async def test_anything_else_going_wrong_is_not_swallowed():
    api = FakeCalendar(fail={"team@example.org": RuntimeError("a bug")})

    with pytest.raises(RuntimeError, match="a bug"):
        await reader(api).events()


async def test_calendars_past_the_cap_are_counted_as_not_read(monkeypatch):
    monkeypatch.setattr(calendar, "MAX_CALENDARS", 2)
    api = FakeCalendar()

    result = await reader(api).events()

    assert len(api.asked()) == 3 and result["calendars_not_read"] == 1


async def test_a_calendar_list_without_their_own_still_reads_primary_in_the_machines_zone():
    api = FakeCalendar(listing={"items": ["junk", TEAM]})

    result = await reader(api).events()

    assert api.asked()[1] == "calendars/primary/events"
    assert "timeZone" not in api.calls[1][1] and result["timezone"]


async def test_an_answer_that_takes_too_long_is_stopped_and_said():
    with pytest.raises(GoogleApiError) as raised:
        await reader(FakeCalendar(delay=5), timeout_s=0.05).events()

    assert raised.value.code == "timeout" and "Calendar" in raised.value.spoken("Calendar")


# ---------------------------------------------------------------------------- writing


async def test_a_timed_event_goes_on_their_own_calendar_in_its_zone_inviting_nobody():
    api = FakeCalendar()

    result = await writer(api).add(
        title="Dentist", day="thursday", start_time="15:00", duration_minutes=45,
        location="12 Main St", description="Bring the x-rays.",
    )

    [(path, body, params)] = api.posts
    assert path == "calendars/primary/events" and params == {"sendUpdates": "none"}
    assert body == {
        "summary": "Dentist",
        "start": {"dateTime": "2026-11-26T15:00:00", "timeZone": NEW_YORK},
        "end": {"dateTime": "2026-11-26T15:45:00", "timeZone": NEW_YORK},
        "location": "12 Main St",
        "description": "Bring the x-rays.",
    }
    assert "attendees" not in body
    assert result == {
        "status": "created", "title": "Dentist", "day": "Thursday 26 November",
        "start": "15:00", "end": "15:45", "location": "12 Main St",
        "message": CREATED_MESSAGE,
    }


async def test_the_zone_is_read_once_from_an_events_listing():
    api = FakeCalendar()
    one = writer(api)

    await one.add(title="a", day="today", start_time="9")
    await one.add(title="b", day="today", start_time="10")

    assert api.calls == [("calendars/primary/events", {"maxResults": 1, "fields": "timeZone"})]


async def test_with_no_end_it_gets_the_usual_length_and_an_end_time_wins():
    api = FakeCalendar()

    usual = await writer(api, default_minutes=30).add(title="a", day="today", start_time="9:00")
    ended = await writer(api).add(
        title="b", day="today", start_time="23:00", end_time="23:30", duration_minutes=5
    )
    late = await writer(api).add(title="c", day="today", start_time="23:30")

    assert (usual["start"], usual["end"]) == ("09:00", "09:30")
    assert (ended["start"], ended["end"]) == ("23:00", "23:30")
    assert late["end"] == "00:30" and late["end_day"] == "Monday 23 November"
    assert api.posts[2][1]["end"]["dateTime"] == "2026-11-23T00:30:00"


async def test_without_a_time_it_lasts_all_day_and_days_makes_it_several():
    api = FakeCalendar()

    one = await writer(api).add(title="Rent due", day="2026-12-01")
    three = await writer(api, clock="12h").add(title="Conference", day="monday", days=3)

    assert api.posts[0][1] == {"summary": "Rent due", "start": {"date": "2026-12-01"},
                               "end": {"date": "2026-12-02"}}
    assert one == {"status": "created", "title": "Rent due", "day": "Tuesday 1 December",
                   "all_day": True, "message": CREATED_MESSAGE}
    assert api.posts[1][1]["end"] == {"date": "2026-11-26"}
    assert three["until"] == "Wednesday 25 November"


@pytest.mark.parametrize(
    ("arguments", "says"),
    [
        ({"title": "  ", "day": "today"}, "title is required"),
        ({"title": "x", "day": "today", "end_time": "10:00"}, "start_time is needed"),
        ({"title": "x", "day": "today", "duration_minutes": 30}, "start_time is needed"),
        ({"title": "x", "day": "today", "start_time": "10", "end_time": "9"}, "not after"),
        ({"title": "x", "day": "today", "start_time": "soon"}, "24-hour clock"),
        ({"title": "x", "day": "today", "start_time": "10", "duration_minutes": 0}, "between"),
        ({"title": "x", "day": "today", "start_time": "10", "duration_minutes": "long"},
         "whole number of minutes"),
        ({"title": "x", "day": "someday"}, "a weekday"),
    ],
)
async def test_what_does_not_make_an_event_is_refused_and_nothing_is_created(arguments, says):
    api = FakeCalendar()

    with pytest.raises(Unclear, match=says):
        await writer(api).add(**arguments)

    assert api.posts == []


async def test_a_calendar_with_no_zone_gets_the_machines_offset_on_each_time():
    api = FakeCalendar(zone="")

    await writer(api).add(title="a", day="today", start_time="9:00")

    start = api.posts[0][1]["start"]
    assert "timeZone" not in start
    assert datetime.fromisoformat(start["dateTime"]).tzinfo is not None


async def test_a_refusal_is_raised_since_nothing_was_created():
    api = FakeCalendar(fail={"post": GoogleApiError("forbidden", "x", status=403)})

    with pytest.raises(GoogleApiError) as raised:
        await writer(api).add(title="a", day="today")

    assert raised.value.code == "forbidden"


@pytest.mark.parametrize(
    "error",
    [
        GoogleApiError("timeout", "events: ReadTimeout"),
        GoogleApiError("failed", "events: ConnectError"),
        GoogleApiError("failed", "events: HTTP 503 (failed)", status=503),
    ],
)
async def test_no_answer_to_the_request_is_unconfirmed_never_a_second_try(error):
    api = FakeCalendar(fail={"post": error})

    result = await writer(api).add(title="a", day="today")

    assert result == {"status": "unconfirmed", "message": UNCONFIRMED_MESSAGE}
    assert len(api.posts) == 1


async def test_running_out_of_time_is_unconfirmed_only_once_the_request_went_out():
    slow = FakeCalendar(delay=0.2)
    one = writer(slow, timeout_s=0.3)

    result = await one.add(title="a", day="today")

    assert result["status"] == "unconfirmed" and len(slow.posts) == 1

    never_sent = FakeCalendar(delay=5)
    with pytest.raises(GoogleApiError) as raised:
        await writer(never_sent, timeout_s=0.05).add(title="a", day="today")
    assert raised.value.code == "timeout" and never_sent.posts == []


# ---------------------------------------------------------------------------- building


def sign_in(settings, scopes):
    settings.ensure_dirs()
    return google.write_token(settings, google.GoogleToken("rt", "cid", "cs", scopes, "a@b.c"))


def test_reading_needs_the_calendar_list_and_adding_needs_only_to_write_events(settings):
    settings.ensure_dirs()
    assert read_problem(settings) == "not signed in to Google: `keryx auth login google`"

    sign_in(settings, (google.CALENDAR_READONLY,))
    assert read_problem(settings) is None
    assert "Calendar is not part of your Google sign-in" in write_problem(settings)

    sign_in(settings, (google.CALENDAR_EVENTS,))
    assert write_problem(settings) is None and read_problem(settings) is not None


def test_the_tools_are_built_on_the_one_token_kept_private(settings):
    path = sign_in(settings, google.requested_scopes(["calendar"]))
    path.chmod(0o644)
    settings.clock_format = "12h"

    read = build_calendar_reader(settings, which="primary", max_events=7)
    write = build_calendar_writer(settings, default_minutes=25)

    assert path.stat().st_mode & 0o777 == 0o600
    assert (read._clock, read._which, read._max) == ("12h", "primary", 7)
    assert (write._clock, write._default_minutes) == ("12h", 25)
    assert not hasattr(read._api, "post") and hasattr(write._api, "post")
    assert read._api._path == path and write._api._path == path


def test_the_clock_it_reads_by_default_is_an_aware_one():
    assert calendar._utc_now().tzinfo is UTC
