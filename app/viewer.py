"""Turn raw iCalendar documents into something a month grid can render."""

from __future__ import annotations

import calendar as pycalendar
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
import recurring_ical_events

log = logging.getLogger(__name__)

# Weeks start on Sunday, matching what most calendar clients show.
FIRST_WEEKDAY = pycalendar.SUNDAY
WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def get_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r, falling back to UTC", name)
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class Event:
    uid: str
    summary: str
    location: str
    description: str
    start: datetime
    end: datetime
    all_day: bool

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        """Last calendar day the event occupies (inclusive)."""
        if self.end <= self.start:
            return self.start.date()
        # An end of exactly midnight belongs to the previous day.
        adjusted = self.end - timedelta(microseconds=1)
        return max(self.start.date(), adjusted.date())

    def dates(self):
        current = self.start_date
        while current <= self.end_date:
            yield current
            current += timedelta(days=1)

    @property
    def time_label(self) -> str:
        if self.all_day:
            return "All day"
        return self.start.strftime("%H:%M")


def _as_datetime(value, tz: tzinfo) -> tuple[datetime, bool]:
    """Normalise a DTSTART/DTEND value to an aware datetime in ``tz``."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz), False
        return value.astimezone(tz), False
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=tz), True
    raise TypeError(f"unsupported date value: {value!r}")


def _component_to_event(component, tz: tzinfo) -> Event | None:
    dtstart = component.get("DTSTART")
    if dtstart is None:
        return None
    start, all_day = _as_datetime(dtstart.dt, tz)

    dtend = component.get("DTEND")
    if dtend is not None:
        end, _ = _as_datetime(dtend.dt, tz)
    elif (duration := component.get("DURATION")) is not None:
        end = start + duration.dt
    elif all_day:
        end = start + timedelta(days=1)
    else:
        end = start

    return Event(
        uid=str(component.get("UID", "")),
        summary=str(component.get("SUMMARY", "(untitled)")),
        location=str(component.get("LOCATION", "")),
        description=str(component.get("DESCRIPTION", "")),
        start=start,
        end=end,
        all_day=all_day,
    )


def expand_events(documents: list[str], start: datetime, end: datetime, tz: tzinfo) -> list[Event]:
    """Expand recurrences across every document and return events in the window."""
    events: list[Event] = []
    for document in documents:
        try:
            cal = icalendar.Calendar.from_ical(document)
        except ValueError as exc:
            log.warning("skipping unparseable calendar object: %s", exc)
            continue
        try:
            occurrences = recurring_ical_events.of(cal).between(start, end)
        except Exception as exc:  # noqa: BLE001 - the expansion library raises many error types
            log.warning("skipping calendar object that failed recurrence expansion: %s", exc)
            continue
        for occurrence in occurrences:
            if occurrence.name != "VEVENT":
                continue
            try:
                event = _component_to_event(occurrence, tz)
            except TypeError as exc:
                log.warning("skipping event with unusable dates: %s", exc)
                continue
            if event is not None:
                events.append(event)
    events.sort(key=lambda e: (e.start, e.summary))
    return events


def month_weeks(year: int, month: int) -> list[list[date]]:
    """The date grid for a month, padded out to whole weeks."""
    return pycalendar.Calendar(firstweekday=FIRST_WEEKDAY).monthdatescalendar(year, month)


def group_by_date(events: list[Event]) -> dict[date, list[Event]]:
    grouped: dict[date, list[Event]] = defaultdict(list)
    for event in events:
        for day in event.dates():
            grouped[day].append(event)
    return grouped


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def upcoming(events: list[Event], now: datetime, limit: int = 10) -> list[Event]:
    return [e for e in events if e.end >= now][:limit]
