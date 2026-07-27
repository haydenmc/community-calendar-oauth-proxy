from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.caldav import parse_calendar_data
from app.viewer import (
    countdowns,
    expand_events,
    group_by_date,
    month_weeks,
    shift_month,
    upcoming,
)

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


# -- countdowns ---------------------------------------------------------------

MONTHLY_RETREAT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:retreat
DTSTART;VALUE=DATE:20260601
DTEND;VALUE=DATE:20260608
RRULE:FREQ=MONTHLY
SUMMARY:Retreat
END:VEVENT
END:VCALENDAR
"""

BIG_TRIP = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:big-trip
DTSTART;VALUE=DATE:20270214
DTEND;VALUE=DATE:20270221
SUMMARY:Big trip
LOCATION:Somewhere far
END:VEVENT
END:VCALENDAR
"""


def year_from(day: date):
    return datetime.combine(day, time.min, tzinfo=UTC), datetime.combine(
        day + timedelta(days=365), time.min, tzinfo=UTC
    )


def test_day_span_counts_inclusive_days():
    start, end = march_2026_window()
    (lan,) = expand_events([ALL_DAY_LAN], start, end, UTC)
    # DTEND is exclusive, so 14th to 16th is two days occupied, not three.
    assert lan.day_span == 2

    (night,) = expand_events([WEEKLY_GAME_NIGHT.replace("COUNT=4", "COUNT=1")], start, end, UTC)
    assert night.day_span == 1


def test_countdowns_pick_out_big_future_events():
    today = date(2026, 8, 1)
    start, end = year_from(today)
    events = expand_events([BIG_TRIP, ALL_DAY_LAN, WEEKLY_GAME_NIGHT], start, end, UTC)

    (item,) = countdowns(events, today, today, min_span=3)
    assert item.event.summary == "Big trip"
    assert item.days_away == (date(2027, 2, 14) - today).days
    assert item.event.day_span == 7


def test_countdowns_ignore_events_too_short_to_matter():
    today = date(2026, 8, 1)
    start, end = year_from(today)
    events = expand_events([BIG_TRIP], start, end, UTC)

    assert countdowns(events, today, today, min_span=7) != []
    assert countdowns(events, today, today, min_span=8) == []


def test_countdowns_drop_events_already_on_the_grid():
    today = date(2027, 2, 1)
    start, end = year_from(today)
    events = expand_events([BIG_TRIP], start, end, UTC)

    assert countdowns(events, today, today, min_span=3) != []
    # Once the grid runs past the event's start it is on screen already.
    assert countdowns(events, today, date(2027, 2, 28), min_span=3) == []


def test_countdowns_drop_events_already_under_way():
    today = date(2027, 2, 16)  # the trip started on the 14th
    start, end = year_from(today)
    events = expand_events([BIG_TRIP], start, end, UTC)

    assert countdowns(events, today, today, min_span=3) == []


def test_countdowns_keep_only_the_nearest_occurrence_of_a_series():
    today = date(2026, 5, 1)
    start, end = year_from(today)
    events = expand_events([MONTHLY_RETREAT], start, end, UTC)
    # The window is full of occurrences; only the nearest earns a countdown.
    assert len([e for e in events if e.summary == "Retreat"]) > 1

    (item,) = countdowns(events, today, today, min_span=3)
    assert item.event.start_date == date(2026, 6, 1)


def test_countdowns_do_not_deduplicate_events_without_a_uid():
    today = date(2026, 8, 1)
    start, end = year_from(today)
    # A UID is defaulted to "" when absent, which is a fallback, not an identity.
    documents = [
        BIG_TRIP.replace("UID:big-trip\n", ""),
        BIG_TRIP.replace("UID:big-trip\n", "").replace("20270214", "20270314").replace(
            "20270221", "20270321"
        ),
    ]
    events = expand_events(documents, start, end, UTC)
    assert len(countdowns(events, today, today, min_span=3)) == 2


def test_countdowns_are_capped_and_ordered_by_start():
    today = date(2026, 8, 1)
    start, end = year_from(today)
    documents = [
        BIG_TRIP.replace("big-trip", f"trip-{month}")
        .replace("20270214", f"20270{month}14")
        .replace("20270221", f"20270{month}21")
        for month in (5, 3, 4)
    ]
    events = expand_events(documents, start, end, UTC)

    items = countdowns(events, today, today, min_span=3, limit=2)
    assert [i.event.start_date for i in items] == [date(2027, 3, 14), date(2027, 4, 14)]


def test_days_away_is_a_whole_day_count_across_a_dst_change():
    tz = ZoneInfo("America/Los_Angeles")
    today = date(2026, 3, 1)  # US clocks go forward on 8 March 2026
    start = datetime.combine(today, time.min, tzinfo=tz)
    trip = BIG_TRIP.replace("20270214", "20260320").replace("20270221", "20260327")
    events = expand_events([trip], start, start + timedelta(days=365), tz)

    (item,) = countdowns(events, today, today, min_span=3)
    # 19 calendar days, not 18-and-23-hours rounded down.
    assert item.days_away == 19


def test_countdown_label_reads_naturally():
    today = date(2027, 2, 13)
    start, end = year_from(today)
    events = expand_events([BIG_TRIP], start, end, UTC)
    (tomorrow,) = countdowns(events, today, today, min_span=3)
    assert tomorrow.label == "tomorrow"

    (later,) = countdowns(events, date(2027, 2, 10), date(2027, 2, 10), min_span=3)
    assert later.label == "in 4 days"


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
