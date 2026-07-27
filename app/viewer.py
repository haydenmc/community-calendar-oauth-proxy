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

    @property
    def day_span(self) -> int:
        """Calendar days the event occupies, inclusive of both ends."""
        return (self.end_date - self.start_date).days + 1

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


@dataclass(frozen=True)
class Countdown:
    event: Event
    days_away: int

    @property
    def label(self) -> str:
        return "tomorrow" if self.days_away == 1 else f"in {self.days_away} days"


def countdowns(
    events: list[Event],
    today: date,
    visible_through: date,
    *,
    min_span: int = 3,
    limit: int = 5,
) -> list[Countdown]:
    """Countdowns for big events starting after the last day of the rendered grid.

    ``visible_through`` is that last grid day, so an event already drawn on
    screen loses its countdown - the grid is the better signal once it is
    there. It is never earlier than today, so this also drops events that are
    already under way.

    ``events`` is expected sorted by start, as expand_events leaves it, so the
    occurrence kept for a recurring series is the nearest one.
    """
    seen: set[str] = set()
    found: list[Countdown] = []
    for event in events:
        if event.day_span < min_span or event.start_date <= visible_through:
            continue
        # An empty UID is the fallback in _component_to_event, not an identity;
        # deduplicating on it would collapse unrelated events into one.
        if event.uid:
            if event.uid in seen:
                continue
            seen.add(event.uid)
        found.append(Countdown(event=event, days_away=(event.start_date - today).days))
        if len(found) == limit:
            break
    return found


class ExpansionCache:
    """One expanded window, valid only while the collection's ctag holds.

    Fetching a year of documents is cheap, but expanding a year of recurrences
    runs on the event loop on every load of the busiest page. The ctag moves on
    any write and the window key moves at the month boundary, so this needs no
    invalidation of its own.
    """

    def __init__(self) -> None:
        self._key: tuple[str, str, str] | None = None
        self._events: list[Event] = []

    def get(self, key: tuple[str, str, str] | None) -> list[Event] | None:
        if key is None or key != self._key:
            return None
        return self._events

    def put(self, key: tuple[str, str, str] | None, events: list[Event]) -> None:
        if key is None:
            return  # Without a ctag there is nothing to invalidate against.
        self._key = key
        self._events = events
