"""Build a single-occurrence VEVENT from what the web form collected.

Deliberately narrow: one occurrence, no recurrence, no attendees, no alarms.
Anything richer is what a CalDAV client is for, and this module exists so people
without one can still put an event on the shared calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from uuid import uuid4

import icalendar

# Radicale stores whatever we PUT, and these fields are rendered straight back
# into the viewer and into every subscriber's calendar, so cap them here rather
# than trusting the maxlength attributes on the form.
MAX_SUMMARY = 200
MAX_LOCATION = 200
MAX_DESCRIPTION = 2000

# Matches the range the calendar viewer accepts in its year query parameter, so
# an event cannot be filed somewhere the month grid will not navigate to.
MIN_YEAR = 1970
MAX_YEAR = 2200

DEFAULT_DURATION = timedelta(hours=1)

# What `<input type="datetime-local">` submits, plus the seconds-bearing form
# some browsers send, plus the date-only form the all-day toggle switches to.
INPUT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")

PRODID = "-//calendar-proxy//EN"

# Non-standard, so no client acts on it; it is here so a confusing or unwelcome
# event can be traced back to whoever posted it.
CREATED_BY_PROPERTY = "X-CALENDAR-PROXY-CREATED-BY"


class EventFormError(ValueError):
    """A submitted form that cannot become an event, with a message to show."""


@dataclass(frozen=True)
class EventDraft:
    summary: str
    location: str
    description: str
    start: datetime
    end: datetime
    all_day: bool


def new_uid() -> str:
    return f"{uuid4()}@calendar-proxy"


def _parse_moment(value: str, field: str, tz: tzinfo) -> datetime:
    value = value.strip()
    for fmt in INPUT_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise EventFormError(f"{field} is not a date and time this form understands.")


def _check_length(value: str, limit: int, field: str) -> str:
    if len(value) > limit:
        raise EventFormError(f"{field} is too long: {limit} characters at most.")
    return value


def parse_event_form(
    *,
    summary: str,
    location: str,
    description: str,
    start: str,
    end: str,
    all_day: bool,
    tz: tzinfo,
) -> EventDraft:
    """Validate the submitted form and normalise it into an EventDraft.

    Times are read as local to ``tz`` - the zone the whole site is presented in -
    because that is the only clock the person filling in the form is looking at.
    """
    summary = _check_length(summary.strip(), MAX_SUMMARY, "The title")
    if not summary:
        raise EventFormError("Please give the event a title.")
    location = _check_length(location.strip(), MAX_LOCATION, "The location")
    description = _check_length(description.strip(), MAX_DESCRIPTION, "The description")

    if not start.strip():
        raise EventFormError("Please say when the event starts.")
    started = _parse_moment(start, "The start", tz)

    if end.strip():
        ended = _parse_moment(end, "The end", tz)
    elif all_day:
        ended = started
    else:
        ended = started + DEFAULT_DURATION

    for moment in (started, ended):
        if not MIN_YEAR <= moment.year <= MAX_YEAR:
            raise EventFormError(f"Dates must fall between {MIN_YEAR} and {MAX_YEAR}.")

    if all_day:
        # Only the dates matter; whatever the time inputs held is discarded.
        if ended.date() < started.date():
            raise EventFormError("The event cannot end before it starts.")
    elif ended <= started:
        raise EventFormError("The event must end after it starts.")

    return EventDraft(
        summary=summary,
        location=location,
        description=description,
        start=started,
        end=ended,
        all_day=all_day,
    )


def build_event_ics(draft: EventDraft, *, uid: str, created_by: str, now: datetime) -> bytes:
    """Render a draft as a one-VEVENT iCalendar document."""
    event = icalendar.Event()
    event.add("UID", uid)
    event.add("DTSTAMP", now.astimezone(UTC))
    event.add("CREATED", now.astimezone(UTC))
    event.add("LAST-MODIFIED", now.astimezone(UTC))
    event.add("SUMMARY", draft.summary)

    if draft.all_day:
        # RFC 5545 3.8.2.2: for a DATE value DTEND is exclusive, so a single-day
        # event ends on the following day.
        event.add("DTSTART", draft.start.date())
        event.add("DTEND", draft.end.date() + timedelta(days=1))
    else:
        # UTC rather than a TZID: it is unambiguous, needs no VTIMEZONE, and with
        # no recurrence in play the originating zone carries no meaning.
        event.add("DTSTART", draft.start.astimezone(UTC))
        event.add("DTEND", draft.end.astimezone(UTC))

    if draft.location:
        event.add("LOCATION", draft.location)
    if draft.description:
        event.add("DESCRIPTION", draft.description)
    event.add(CREATED_BY_PROPERTY, created_by)

    # No VALARM, on purpose. These are community events posted for other people;
    # an event added here must never make someone else's phone go off.

    calendar = icalendar.Calendar()
    calendar.add("PRODID", PRODID)
    calendar.add("VERSION", "2.0")
    calendar.add_component(event)
    return calendar.to_ical()


def filename_for(uid: str) -> str:
    """Collection filename for an event, matching what CalDAV clients use."""
    return f"{uid}.ics"


def month_of(draft: EventDraft) -> tuple[int, int]:
    """Year and month the event starts in, for linking back to the grid."""
    starts: date = draft.start.date()
    return starts.year, starts.month
