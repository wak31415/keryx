"""The calendar client: a stretch of days read out, and one event added.

No network: Google Calendar is a fake keyed by API path. Every calendar, event and person
in here is invented, and the times asserted on were worked out by hand from the fixtures.
"""

import asyncio
import json
from datetime import UTC, date, datetime, time
from urllib.parse import unquote

import httpx
import pytest

from keryx.integrations import calendar, google
from keryx.integrations.calendar import (
    CREATED_MESSAGE,
    MAYBE_MORE_NOTE,
    MORE_NOTE,
    NOTHING_FOUND,
    PAST_MESSAGE,
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
from keryx.integrations.google_api import CALENDAR, MESSAGES, GoogleApiError, GoogleWriter

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
    """Calendar by path: the calendar list, and each calendar's events by its id.

    As Google does it: events in the order given, cut at `maxResults`, with a token for
    the next page while there are more. `pages` overrides that for one calendar with the
    pages to hand out, whatever was asked for, which is how a short or empty page with a
    token is played.
    """

    def __init__(self, events=None, *, listing=LISTING, fail=None, delay=0.0, zone=NEW_YORK,
                 pages=None, listing_pages=None):
        self.events = events or {}
        self.listing = listing
        self.fail = fail or {}
        self.delay = delay
        self.zone = zone
        self.pages = pages or {}
        self.listing_pages = listing_pages
        self.calls: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict, dict]] = []
        self.closed = 0

    async def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        await asyncio.sleep(self.delay)
        at = int(params.get("pageToken") or 0)
        if path == "users/me/calendarList":
            if self.listing_pages is None:
                return self.listing
            more = at + 1 < len(self.listing_pages)
            return {"items": self.listing_pages[at], **({"nextPageToken": str(at + 1)} if more
                                                        else {})}
        calendar_id = unquote(path.split("/")[1])
        if calendar_id in self.fail:
            raise self.fail[calendar_id]
        if calendar_id in self.pages:
            pages = self.pages[calendar_id]
            more = at + 1 < len(pages)
            return {"timeZone": self.zone, "items": pages[at],
                    **({"nextPageToken": str(at + 1)} if more else {})}
        items = self.events.get(calendar_id, [])
        size = params.get("maxResults", 250)
        page = {"timeZone": self.zone, "items": items[at : at + size]}
        if at + size < len(items):
            page["nextPageToken"] = str(at + size)
        return page

    async def post(self, path, body, params=None, *, sending=None):
        if sending is not None:
            sending()
        self.posts.append((path, dict(body), dict(params or {})))
        await asyncio.sleep(self.delay)
        if "post" in self.fail:
            raise self.fail["post"]
        return {"id": "made"}

    async def aclose(self):
        self.closed += 1

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
        ("2026-2-3", date(2026, 2, 3)),
    ],
)
def test_a_day_is_taken_the_way_it_is_said(said, day):
    assert resolve_day(said, TODAY) == day


@pytest.mark.parametrize(
    "said",
    ["", "next week", "the 3rd", "2026-13-40", "thursdayish", "2026-11-234",
     "2026-12-03T09:00:00", "2026-12-03 or so"],
)
def test_a_day_that_is_not_one_says_what_is_wanted(said):
    with pytest.raises(Unclear, match="today, tomorrow, yesterday, a weekday"):
        resolve_day(said, TODAY)


@pytest.mark.parametrize(
    ("said", "at"),
    [
        ("14:30", time(14, 30)),
        ("9:00", time(9)),
        ("09.05", time(9, 5)),
        ("15h30", time(15, 30)),
        ("2:30 pm", time(14, 30)),
        ("3pm", time(15)),
        ("12am", time(0)),
        ("12:30 a.m.", time(0, 30)),
        ("12 p.m.", time(12)),
        ("noon", time(12)),
        ("midnight", time(0)),
    ],
)
def test_a_time_is_read_on_either_clock(said, at):
    assert parse_time(said) == at


@pytest.mark.parametrize(
    "said", ["", "25:00", "24:00", "9:75", "13pm", "0 am", "afternoon", "3ish", "1430"]
)
def test_a_time_that_is_not_one_says_what_is_wanted(said):
    with pytest.raises(Unclear, match="24-hour clock"):
        parse_time(said)


@pytest.mark.parametrize("said", ["3", "9", "15", " 7 "])
def test_a_bare_hour_is_refused_because_it_could_be_either_half_of_the_day(said):
    """`3` is 03:00 on the clock that was asked for and three in the afternoon to whoever
    said it. Twelve hours on a calendar is not a guess to make."""
    with pytest.raises(Unclear, match="a bare hour could be either half of the day"):
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


DECLINED = [{"self": True, "responseStatus": "declined"}, {"responseStatus": "accepted"}]


async def test_what_is_next_is_never_something_they_declined():
    """Three declined invitations used to be the next three, and the meeting after them
    never said."""
    api = FakeCalendar({"primary": [
        *(timed(f"Declined {n}", f"2026-11-22T19:{n}0:00-05:00", f"2026-11-22T19:{n}5:00-05:00",
                attendees=DECLINED) for n in range(3)),
        timed("Dinner", "2026-11-22T20:00:00-05:00", "2026-11-22T21:00:00-05:00"),
    ]})

    result = await reader(api).events(upcoming=True)

    assert [e["title"] for e in result["events"]] == ["Dinner"]

    said_all_the_same = await reader(api).events()
    assert [e.get("response") for e in said_all_the_same["events"]] == ["declined"] * 3 + [None]


async def test_what_is_not_next_is_left_out_before_the_count_is_taken():
    """With `max_events` at 2, two all-day events and one already running filled the fetch
    and the afternoon's appointment was never seen."""
    api = FakeCalendar({"primary": [
        all_day("Conference", "2026-11-22", "2026-11-25"),
        all_day("Birthday", "2026-11-22", "2026-11-23"),
        timed("Already on", "2026-11-22T18:00:00-05:00", "2026-11-22T19:00:00-05:00"),
        timed("Dentist", "2026-11-22T19:00:00-05:00", "2026-11-22T20:00:00-05:00"),
    ]})

    result = await reader(api, max_events=2).events(upcoming=True)

    assert [e["title"] for e in result["events"]] == ["Dentist"]
    assert api.calls[1][1]["maxResults"] == 50


async def test_what_is_next_is_looked_for_past_the_first_page():
    busy = [timed(f"Declined {n}", "2026-11-22T19:00:00-05:00", "2026-11-22T19:30:00-05:00",
                  attendees=DECLINED) for n in range(50)]
    api = FakeCalendar({"primary": [
        *busy, timed("Dinner", "2026-11-22T20:00:00-05:00", "2026-11-22T21:00:00-05:00"),
    ]})

    result = await reader(api, which="primary").events(upcoming=True)

    assert [e["title"] for e in result["events"]] == ["Dinner"]
    assert [params.get("pageToken") for _, params in api.calls[1:]] == [None, "50"]


async def test_what_is_next_says_so_when_it_ran_out_of_pages_before_it_found_three():
    declined = [timed(f"Declined {n}", "2026-11-22T19:00:00-05:00",
                      "2026-11-22T19:30:00-05:00", attendees=DECLINED) for n in range(50)]
    api = FakeCalendar(pages={"primary": [declined] * (calendar.MAX_PAGES + 1)})

    unfinished = await reader(api, which="primary").events(upcoming=True)

    assert unfinished["count"] == 0 and unfinished["more"] == MAYBE_MORE_NOTE
    assert len(api.calls) == 1 + calendar.MAX_PAGES

    three = [timed(f"Next {n}", f"2026-11-22T2{n}:00:00-05:00", f"2026-11-22T2{n}:30:00-05:00")
             for n in range(4)]
    found = await reader(FakeCalendar({"primary": three}), which="primary").events(upcoming=True)
    assert found["count"] == 3 and "more" not in found


async def test_what_is_next_from_a_day_to_come_starts_at_that_day():
    api = FakeCalendar({"primary": [
        timed("Review", "2026-11-26T10:00:00-05:00", "2026-11-26T11:00:00-05:00"),
    ]})

    result = await reader(api).events(day="thursday", upcoming=True)

    assert api.window() == ("2026-11-26T00:00:00-05:00", "2026-12-10T00:00:00-05:00")
    assert (result["from"], result["to"]) == ("Thursday 26 November", "Thursday 10 December")
    assert [e["title"] for e in result["events"]] == ["Review"]


@pytest.mark.parametrize("day", ["today", "yesterday", "sunday"])
async def test_what_is_next_never_starts_before_this_minute(day):
    api = FakeCalendar()

    result = await reader(api).events(day=day, upcoming=True)

    assert api.window()[0] == "2026-11-22T18:30:00-05:00" and result["from"] == "now"


# ----------------------------------------------------------------- a list in pages


DENTIST = timed("Dentist", "2026-12-08T14:00:00-05:00", "2026-12-08T15:00:00-05:00")


async def test_an_empty_page_with_more_behind_it_is_not_the_answer():
    """Google hands back short and empty pages with a token for the next, readily for a
    search over a long stretch. Stopping at the first read as "nothing matched"."""
    api = FakeCalendar(pages={"primary": [[], [], [DENTIST]]})

    result = await reader(api, which="primary").events(query="dentist")

    assert [e["title"] for e in result["events"]] == ["Dentist"]
    assert [params.get("pageToken") for _, params in api.calls[1:]] == [None, "1", "2"]
    assert "more" not in result and "message" not in result


async def test_a_list_is_followed_for_so_many_pages_and_then_says_it_may_not_be_all():
    api = FakeCalendar(pages={"primary": [[], [], [], [], [DENTIST]]})

    result = await reader(api, which="primary").events(query="dentist")

    assert len(api.calls) == 1 + calendar.MAX_PAGES
    assert result["count"] == 0 and result["more"] == MAYBE_MORE_NOTE
    assert "message" not in result  # not "nothing matched": it was not all read


async def test_a_page_of_what_is_not_an_event_does_not_end_the_list_short():
    def cancelled(n):
        return timed(f"Off {n}", "2026-11-22T09:00:00-05:00", "2026-11-22T10:00:00-05:00",
                     status="cancelled")

    api = FakeCalendar({"primary": [
        *(cancelled(n) for n in range(3)),
        *(timed(f"Event {n}", f"2026-11-22T1{n}:00:00-05:00", f"2026-11-22T1{n}:30:00-05:00")
          for n in range(3)),
    ]})

    result = await reader(api, which="primary", max_events=2).events()

    assert [e["title"] for e in result["events"]] == ["Event 0", "Event 1"]
    assert result["more"] == MORE_NOTE.format(count=2)


async def test_one_page_that_is_the_whole_answer_is_one_request_a_calendar():
    api = FakeCalendar({"primary": [DENTIST]})

    await reader(api).events(query="dentist")

    assert len(api.calls) == 1 + 3  # the list, and each of three calendars once
    assert not any("pageToken" in params for _, params in api.calls)


async def test_their_own_calendar_on_a_second_page_of_the_list_is_still_found():
    """Without it the zone fell back to the machine's, and every day was read in that."""
    api = FakeCalendar(listing_pages=[[TEAM], [{**OWN, "timeZone": "Asia/Tokyo"}]])

    result = await reader(api).events()

    assert api.asked()[:2] == ["users/me/calendarList"] * 2
    assert api.calls[1][1]["pageToken"] == "1"
    assert result["timezone"] == "Asia/Tokyo" and result["today"] == "Monday 23 November"


def test_the_cap_is_held_under_a_page_so_more_can_always_be_seen():
    assert reader(FakeCalendar(), max_events=250)._max == calendar.MAX_EVENTS_SETTING == 100
    assert calendar.MAX_EVENTS_SETTING < calendar.PAGE_SIZE


async def test_the_connections_are_closed_when_it_has_answered_or_failed():
    api = FakeCalendar()
    one = reader(api)

    await one.events()
    with pytest.raises(Unclear):
        await one.events(day="soon")

    assert api.closed == 2


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


@pytest.mark.parametrize("days", [367, "a week", -1])
async def test_a_stretch_that_is_not_a_number_of_days_is_refused(days):
    with pytest.raises(Unclear, match="days is"):
        await reader(FakeCalendar()).events(days=days)


async def test_a_zero_for_days_is_an_argument_filled_in_for_nothing_and_reads_one_day():
    api = FakeCalendar()

    result = await reader(api).events(days=0)

    assert api.window() == ("2026-11-22T00:00:00-05:00", "2026-11-23T00:00:00-05:00")
    assert "to" not in result


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
    assert "the weekday and the date" in CREATED_MESSAGE and api.closed == 1


async def test_the_zone_is_read_once_from_an_events_listing():
    api = FakeCalendar()
    one = writer(api)

    await one.add(title="a", day="tomorrow", start_time="9:00")
    await one.add(title="b", day="tomorrow", start_time="10:00")

    assert api.calls == [("calendars/primary/events", {"maxResults": 1, "fields": "timeZone"})]


async def test_with_no_end_it_gets_the_usual_length_and_an_end_time_wins():
    api = FakeCalendar()

    usual = await writer(api, default_minutes=30).add(
        title="a", day="tomorrow", start_time="9:00"
    )
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
        ({"title": "x", "day": "today", "end_time": "22:00"}, "start_time is needed"),
        ({"title": "x", "day": "today", "duration_minutes": 30}, "start_time is needed"),
        ({"title": "x", "day": "today", "start_time": "22:00", "end_time": "21:00"},
         "not after"),
        ({"title": "x", "day": "today", "start_time": "soon"}, "24-hour clock"),
        ({"title": "x", "day": "today", "start_time": "9"}, "a bare hour could be either"),
        ({"title": "x", "day": "today", "start_time": "22:00", "end_time": "11"},
         "a bare hour could be either"),
        ({"title": "x", "day": "today", "start_time": "22:00", "duration_minutes": -5},
         "between"),
        ({"title": "x", "day": "today", "start_time": "22:00", "duration_minutes": "long"},
         "whole number of minutes"),
        ({"title": "x", "day": "tomorrow", "start_time": "10:00", "days": 3},
         "days is for an event that lasts all day"),
        ({"title": "x", "day": "someday"}, "a weekday"),
        ({"title": "x", "day": "2026-11-234", "start_time": "9:00"}, "a weekday"),
    ],
)
async def test_what_does_not_make_an_event_is_refused_and_nothing_is_created(arguments, says):
    api = FakeCalendar()

    with pytest.raises(Unclear, match=says):
        await writer(api).add(**arguments)

    assert api.posts == []


async def test_a_zero_in_an_optional_number_is_read_as_nothing_given():
    """A model fills in arguments it had nothing for, and a 0 is how it does it."""
    api = FakeCalendar()

    timed_one = await writer(api).add(
        title="a", day="tomorrow", start_time="10:00", duration_minutes=0, days=0
    )
    whole_day = await writer(api).add(title="b", day="tomorrow", duration_minutes=0, days=0)

    assert (timed_one["start"], timed_one["end"]) == ("10:00", "11:00")
    assert whole_day["all_day"] is True and "until" not in whole_day


async def test_a_calendar_with_no_zone_gets_the_machines_offset_on_each_time():
    api = FakeCalendar(zone="")

    await writer(api).add(title="a", day="tomorrow", start_time="9:00")

    start = api.posts[0][1]["start"]
    assert "timeZone" not in start
    assert datetime.fromisoformat(start["dateTime"]).tzinfo is not None


# --- never into the past ---------------------------------------------------------------------


async def test_a_weekday_whose_time_has_gone_today_is_next_weeks():
    """NOW is a Sunday at 18:30. "Sunday at nine" used to be created this morning."""
    api = FakeCalendar()

    gone = await writer(api).add(title="Brunch", day="sunday", start_time="09:00")
    to_come = await writer(api).add(title="Film", day="Sunday", start_time="20:00")

    assert (gone["status"], gone["day"]) == ("created", "Sunday 29 November")
    assert api.posts[0][1]["start"]["dateTime"] == "2026-11-29T09:00:00"
    assert (to_come["status"], to_come["day"]) == ("created", "Sunday 22 November")


async def test_an_all_day_event_named_by_todays_weekday_is_today():
    api = FakeCalendar()

    result = await writer(api).add(title="Laundry", day="sunday")

    assert result["day"] == "Sunday 22 November" and api.posts[0][1]["start"] == {
        "date": "2026-11-22"
    }


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ({"day": "today", "start_time": "09:00"},
         {"day": "Sunday 22 November", "start": "09:00", "end": "10:00"}),
        ({"day": "yesterday"}, {"day": "Saturday 21 November", "all_day": True}),
        ({"day": "yesterday", "start_time": "23:00"},
         {"day": "Saturday 21 November", "start": "23:00", "end": "00:00",
          "end_day": "Sunday 22 November"}),
        ({"day": "2026-11-20", "start_time": "15:00"},
         {"day": "Friday 20 November", "start": "15:00", "end": "16:00"}),
        ({"day": "2025-12-25"}, {"day": "Thursday 25 December 2025", "all_day": True}),
    ],
)
async def test_a_start_that_has_passed_is_refused_with_when_it_would_have_been(arguments, said):
    api = FakeCalendar()

    result = await writer(api).add(title="Dentist", **arguments)

    assert result == {
        "status": "in_the_past", "title": "Dentist", **said,
        "today": "Sunday 22 November", "now": "18:30", "message": PAST_MESSAGE,
    }
    assert api.posts == []


async def test_a_start_in_the_last_quarter_of_an_hour_is_now_not_the_past():
    """The model's "now" is the Time line it was given when the call began, minutes ago.
    "Block from now until seven" was always refused, to the second."""
    api = FakeCalendar()

    just_now = await writer(api).add(title="Focus", day="today", start_time="18:27",
                                     end_time="19:00")
    at_the_edge = await writer(api).add(title="Focus", day="today", start_time="18:15")
    too_late = await writer(api).add(title="Focus", day="today", start_time="18:14")

    assert (just_now["status"], just_now["start"], just_now["end"]) == (
        "created", "18:27", "19:00",
    )
    assert api.posts[0][1]["start"]["dateTime"] == "2026-11-22T18:27:00"  # as given
    assert at_the_edge["status"] == "created"
    assert too_late["status"] == "in_the_past" and len(api.posts) == 2


async def test_a_weekday_that_is_today_rolls_a_week_only_once_the_grace_has_gone():
    api = FakeCalendar()

    this_minute = await writer(api).add(title="Call", day="sunday", start_time="18:30")
    minutes_ago = await writer(api).add(title="Call", day="sunday", start_time="18:20")
    long_gone = await writer(api).add(title="Call", day="sunday", start_time="18:10")

    assert [r["day"] for r in (this_minute, minutes_ago, long_gone)] == [
        "Sunday 22 November", "Sunday 22 November", "Sunday 29 November",
    ]
    assert all(r["status"] == "created" for r in (this_minute, minutes_ago, long_gone))


async def test_today_is_not_the_past_for_what_has_not_started():
    api = FakeCalendar()

    all_day_today = await writer(api).add(title="Laundry", day="today")
    tonight = await writer(api).add(title="Film", day="2026-11-22", start_time="18:31")

    assert (all_day_today["status"], tonight["status"]) == ("created", "created")


# --- what is known to have happened, and what is not -----------------------------------------


async def test_a_refusal_is_raised_since_nothing_was_created():
    api = FakeCalendar(fail={"post": GoogleApiError("forbidden", "x", status=403)})

    with pytest.raises(GoogleApiError) as raised:
        await writer(api).add(title="a", day="today")

    assert raised.value.code == "forbidden"


def google_writer(tmp_path, post, *, token=None, scopes=None, seen=None):
    """A real `GoogleWriter` on a MockTransport: the token endpoint answers as `token`
    says (a working one by default), the zone is read, and a POST meets `post`."""
    path = tmp_path / "google_token.json"
    path.write_text(json.dumps({
        "client_id": "cid", "refresh_token": "rtoken",
        "scopes": scopes or [google.CALENDAR_EVENTS],
    }))
    refreshes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            refreshes.append(1)
            if token is not None:
                return token(len(refreshes))
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if request.method == "GET":
            return httpx.Response(200, json={"timeZone": NEW_YORK})
        if seen is not None:
            seen.append(request)
        if isinstance(post, Exception):
            raise post
        return post

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleWriter(path, CALENDAR, needs=calendar.WRITE_SCOPES, client=client)


@pytest.mark.parametrize(
    "post",
    [
        httpx.ReadTimeout("sent, never answered"),
        httpx.RemoteProtocolError("cut off after sending"),
        httpx.Response(503),
        httpx.Response(500, json={"error": {"status": "INTERNAL"}}),
    ],
)
async def test_a_request_that_left_and_was_not_answered_is_unconfirmed_and_never_resent(
    tmp_path, post
):
    seen: list[httpx.Request] = []
    api = google_writer(tmp_path, post, seen=seen)

    result = await writer(api).add(title="Dentist", day="tomorrow", start_time="15:00")

    assert result == {"status": "unconfirmed", "message": UNCONFIRMED_MESSAGE}
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("post", "code"),
    [
        (httpx.ConnectError("no route"), "failed"),
        (httpx.ConnectTimeout("slow to connect"), "timeout"),
        (httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}),
         "not_connected"),
        (httpx.Response(403, json={"error": {"errors": [{"reason": "accessNotConfigured"}]}}),
         "api_disabled"),
        (httpx.Response(400), "rejected"),
        (httpx.Response(404), "not_found"),
    ],
)
async def test_what_is_known_to_have_created_nothing_is_that_failure_never_unconfirmed(
    tmp_path, post, code
):
    """A connection never made and a plain refusal each have a sentence of their own.
    "It may or may not be there" was said for all of them."""
    api = google_writer(tmp_path, post)

    with pytest.raises(GoogleApiError) as raised:
        await writer(api).add(title="Dentist", day="tomorrow", start_time="15:00")

    assert raised.value.code == code
    assert raised.value.spoken("Calendar") == MESSAGES[code].format(service="Calendar")


async def test_signed_out_while_adding_is_signed_out(tmp_path):
    """The access token is refused and the refresh after it is too: nothing was created,
    and the fix is a sign-in, not a look at the calendar."""
    def token(attempt):
        if attempt == 1:
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(400, json={"error": "invalid_grant"})

    seen: list[httpx.Request] = []
    api = google_writer(tmp_path, httpx.Response(401, json={}), token=token, seen=seen)

    with pytest.raises(GoogleApiError) as raised:
        await writer(api).add(title="Dentist", day="tomorrow", start_time="15:00")

    assert raised.value.code == "signed_out" and len(seen) == 1
    assert "`keryx auth login google`" in raised.value.spoken("Calendar")


@pytest.mark.parametrize(
    ("token", "scopes", "code"),
    [
        (lambda n: httpx.Response(400, json={"error": "invalid_grant"}), None, "signed_out"),
        (lambda n: httpx.Response(500), None, "failed"),
        (None, [google.CALENDAR_READONLY], "not_connected"),
    ],
)
async def test_a_sign_in_that_cannot_be_used_is_said_before_anything_is_sent(
    tmp_path, token, scopes, code
):
    seen: list[httpx.Request] = []
    api = google_writer(tmp_path, httpx.Response(200, json={}), token=token, scopes=scopes,
                        seen=seen)

    with pytest.raises(GoogleApiError) as raised:
        await writer(api).add(title="Dentist", day="tomorrow", start_time="15:00")

    assert raised.value.code == code and seen == []


async def test_no_sign_in_at_all_is_not_signed_in(tmp_path):
    api = GoogleWriter(tmp_path / "missing.json", CALENDAR, needs=calendar.WRITE_SCOPES)

    with pytest.raises(GoogleApiError) as raised:
        await writer(api).add(title="Dentist", day="tomorrow", start_time="15:00")

    assert raised.value.code == "not_signed_in"


async def test_a_real_writer_creates_the_event_with_one_post(tmp_path):
    seen: list[httpx.Request] = []
    api = google_writer(tmp_path, httpx.Response(200, json={"id": "made"}), seen=seen)

    result = await writer(api).add(title="Dentist", day="tomorrow", start_time="15:00")

    assert result["status"] == "created" and len(seen) == 1
    assert seen[0].url.params["sendUpdates"] == "none"
    assert json.loads(seen[0].content)["start"] == {
        "dateTime": "2026-11-23T15:00:00", "timeZone": NEW_YORK,
    }


async def test_running_out_of_time_is_unconfirmed_only_once_the_request_went_out():
    slow = FakeCalendar(delay=0.2)
    one = writer(slow, timeout_s=0.3)

    result = await one.add(title="a", day="today")

    assert result["status"] == "unconfirmed" and len(slow.posts) == 1

    never_sent = FakeCalendar(delay=5)
    with pytest.raises(GoogleApiError) as raised:
        await writer(never_sent, timeout_s=0.05).add(title="a", day="today")
    assert raised.value.code == "timeout" and never_sent.posts == []
    assert never_sent.closed == 1


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
