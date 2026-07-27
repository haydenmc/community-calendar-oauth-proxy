from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.caldav import parse_calendar_data
from app.viewer import expand_events, group_by_date, month_weeks, shift_month, upcoming

UTC = ZoneInfo("UTC")

WEEKLY_GAME_NIGHT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:game-night
DTSTART:20260302T190000Z
DTEND:20260302T210000Z
RRULE:FREQ=WEEKLY;COUNT=4
SUMMARY:Game night
LOCATION:Mumble
END:VEVENT
END:VCALENDAR
"""

ALL_DAY_LAN = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:lan-party
DTSTART;VALUE=DATE:20260314
DTEND;VALUE=DATE:20260316
SUMMARY:LAN party
END:VEVENT
END:VCALENDAR
"""


def march_2026_window():
    return datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)


def test_recurrences_are_expanded():
    start, end = march_2026_window()
    events = expand_events([WEEKLY_GAME_NIGHT], start, end, UTC)

    assert [e.start.date() for e in events] == [
        date(2026, 3, 2),
        date(2026, 3, 9),
        date(2026, 3, 16),
        date(2026, 3, 23),
    ]
    assert all(e.summary == "Game night" and e.location == "Mumble" for e in events)
    assert events[0].time_label == "19:00"
    assert not events[0].all_day


def test_all_day_event_spans_its_dates():
    start, end = march_2026_window()
    (event,) = expand_events([ALL_DAY_LAN], start, end, UTC)

    assert event.all_day
    assert event.time_label == "All day"
    # DTEND is exclusive, so the event occupies the 14th and 15th only.
    assert list(event.dates()) == [date(2026, 3, 14), date(2026, 3, 15)]


def test_events_are_sorted_and_grouped_by_day():
    start, end = march_2026_window()
    events = expand_events([ALL_DAY_LAN, WEEKLY_GAME_NIGHT], start, end, UTC)
    assert events == sorted(events, key=lambda e: (e.start, e.summary))

    grouped = group_by_date(events)
    assert [e.summary for e in grouped[date(2026, 3, 16)]] == ["Game night"]
    assert [e.summary for e in grouped[date(2026, 3, 15)]] == ["LAN party"]


def test_timezone_conversion_shifts_local_time():
    start, end = march_2026_window()
    (event,) = expand_events([WEEKLY_GAME_NIGHT.replace("COUNT=4", "COUNT=1")], start, end,
                             ZoneInfo("America/Los_Angeles"))
    # 19:00 UTC on 2 March is 11:00 in Los Angeles.
    assert event.time_label == "11:00"


def test_unparseable_document_is_skipped_not_fatal():
    start, end = march_2026_window()
    events = expand_events(["this is not iCalendar", WEEKLY_GAME_NIGHT], start, end, UTC)
    assert len(events) == 4


def test_month_weeks_cover_whole_weeks_starting_sunday():
    weeks = month_weeks(2026, 3)
    assert all(len(week) == 7 for week in weeks)
    assert weeks[0][0].weekday() == 6  # Sunday
    assert date(2026, 3, 1) in weeks[0]
    assert date(2026, 3, 31) in weeks[-1]


def test_shift_month_wraps_years():
    assert shift_month(2026, 1, -1) == (2025, 12)
    assert shift_month(2026, 12, 1) == (2027, 1)
    assert shift_month(2026, 3, 1) == (2026, 4)


def test_upcoming_drops_finished_events():
    start, end = march_2026_window()
    events = expand_events([WEEKLY_GAME_NIGHT], start, end, UTC)
    later = upcoming(events, datetime(2026, 3, 15, tzinfo=UTC))
    assert [e.start.date() for e in later] == [date(2026, 3, 16), date(2026, 3, 23)]


def test_parse_calendar_data_extracts_documents():
    body = b"""<?xml version="1.0"?>
    <D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
      <D:response>
        <D:href>/community/shared/a.ics</D:href>
        <D:propstat><D:prop>
          <D:getetag>"1"</D:getetag>
          <C:calendar-data>BEGIN:VCALENDAR
END:VCALENDAR</C:calendar-data>
        </D:prop></D:propstat>
      </D:response>
    </D:multistatus>"""
    assert parse_calendar_data(body) == ["BEGIN:VCALENDAR\nEND:VCALENDAR"]


def test_parse_calendar_data_survives_malformed_xml():
    assert parse_calendar_data(b"<not xml") == []
