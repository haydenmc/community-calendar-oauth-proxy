from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import icalendar
import pytest

from app import events
from app.config import Settings
from app.main import create_app

from .conftest import login, session_cookie

LOS_ANGELES = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def draft(**overrides) -> events.EventDraft:
    fields = {
        "summary": "Village hall AGM",
        "location": "",
        "description": "",
        "start": datetime(2026, 8, 14, 19, 0, tzinfo=LOS_ANGELES),
        "end": datetime(2026, 8, 14, 20, 30, tzinfo=LOS_ANGELES),
        "all_day": False,
    }
    fields.update(overrides)
    return events.EventDraft(**fields)


def vevent(body: bytes) -> icalendar.Event:
    calendar = icalendar.Calendar.from_ical(body)
    return next(c for c in calendar.walk() if c.name == "VEVENT")


# --- building the iCalendar document ----------------------------------------


def test_timed_event_is_written_in_utc():
    body = events.build_event_ics(draft(), uid="u@calendar-proxy", created_by="alice", now=NOW)
    component = vevent(body)
    # 19:00 in Los Angeles during PDT is 02:00 UTC the following day.
    assert component["DTSTART"].dt == datetime(2026, 8, 15, 2, 0, tzinfo=UTC)
    assert component["DTEND"].dt == datetime(2026, 8, 15, 3, 30, tzinfo=UTC)
    assert b"DTSTART:20260815T020000Z" in body


def test_all_day_event_uses_dates_with_an_exclusive_end():
    body = events.build_event_ics(
        draft(all_day=True), uid="u@calendar-proxy", created_by="alice", now=NOW
    )
    component = vevent(body)
    assert component["DTSTART"].dt == date(2026, 8, 14)
    # DTEND is exclusive for DATE values, so a one-day event ends on the 15th.
    assert component["DTEND"].dt == date(2026, 8, 15)
    assert b"DTSTART;VALUE=DATE:20260814" in body


def test_multi_day_all_day_event_spans_both_ends():
    body = events.build_event_ics(
        draft(all_day=True, end=datetime(2026, 8, 16, 9, 0, tzinfo=LOS_ANGELES)),
        uid="u@calendar-proxy",
        created_by="alice",
        now=NOW,
    )
    assert vevent(body)["DTEND"].dt == date(2026, 8, 17)


def test_event_carries_no_alarm():
    body = events.build_event_ics(
        draft(location="Village Hall", description="Bring a chair"),
        uid="u@calendar-proxy",
        created_by="alice",
        now=NOW,
    )
    assert b"VALARM" not in body
    assert not [c for c in icalendar.Calendar.from_ical(body).walk() if c.name == "VALARM"]


def test_event_records_who_created_it():
    body = events.build_event_ics(draft(), uid="u@calendar-proxy", created_by="alice", now=NOW)
    assert vevent(body)[events.CREATED_BY_PROPERTY] == "alice"


def test_optional_fields_are_omitted_when_blank():
    component = vevent(
        events.build_event_ics(draft(), uid="u@calendar-proxy", created_by="alice", now=NOW)
    )
    assert "LOCATION" not in component
    assert "DESCRIPTION" not in component


def test_special_characters_survive_a_round_trip():
    text = "Tea, cake; and\nnewlines \\ too — plus émoji ☕"
    body = events.build_event_ics(
        draft(summary="Commas, semis; and\nnewlines", description=text),
        uid="u@calendar-proxy",
        created_by="alice",
        now=NOW,
    )
    component = vevent(body)
    assert str(component["DESCRIPTION"]) == text
    assert str(component["SUMMARY"]) == "Commas, semis; and\nnewlines"


# --- form parsing ------------------------------------------------------------


def parse(**overrides) -> events.EventDraft:
    fields = {
        "summary": "Village hall AGM",
        "location": "",
        "description": "",
        "start": "2026-08-14T19:00",
        "end": "2026-08-14T20:30",
        "all_day": False,
        "tz": LOS_ANGELES,
    }
    fields.update(overrides)
    return events.parse_event_form(**fields)


def test_form_times_are_read_in_the_display_timezone():
    parsed = parse()
    assert parsed.start == datetime(2026, 8, 14, 19, 0, tzinfo=LOS_ANGELES)
    assert not parsed.all_day


def test_seconds_and_date_only_values_are_accepted():
    assert parse(start="2026-08-14T19:00:00").start.hour == 19
    assert parse(start="2026-08-14", end="2026-08-14", all_day=True).start.hour == 0


def test_fields_are_trimmed():
    parsed = parse(summary="  AGM  ", location="  Hall  ", description="  Notes  ")
    assert (parsed.summary, parsed.location, parsed.description) == ("AGM", "Hall", "Notes")


def test_blank_end_defaults_to_an_hour_later():
    assert parse(end="").end == datetime(2026, 8, 14, 20, 0, tzinfo=LOS_ANGELES)


def test_blank_end_on_an_all_day_event_is_a_single_day():
    parsed = parse(start="2026-08-14", end="", all_day=True)
    assert parsed.end.date() == parsed.start.date()


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"summary": "   "}, "title"),
        ({"summary": "x" * 201}, "too long"),
        ({"location": "x" * 201}, "too long"),
        ({"description": "x" * 2001}, "too long"),
        ({"start": ""}, "when the event starts"),
        ({"start": "not a date"}, "understands"),
        ({"end": "14/08/2026"}, "understands"),
        ({"end": "2026-08-14T19:00"}, "must end after"),
        ({"end": "2026-08-13T19:00"}, "must end after"),
        ({"start": "1890-08-14T19:00"}, "between"),
        ({"start": "2026-08-14", "end": "2026-08-13", "all_day": True}, "cannot end before"),
    ],
)
def test_invalid_forms_are_rejected(overrides, fragment):
    with pytest.raises(events.EventFormError) as excinfo:
        parse(**overrides)
    assert fragment in str(excinfo.value)


def test_all_day_ignores_the_time_of_day():
    parsed = parse(start="2026-08-14T19:00", end="2026-08-14T09:00", all_day=True)
    # An end earlier in the day is fine once only the dates matter.
    assert parsed.all_day and parsed.end.date() == parsed.start.date()


# --- routes ------------------------------------------------------------------


FORM = {
    "summary": "Village hall AGM",
    "location": "Village Hall",
    "description": "Bring a chair",
    "start": "2026-08-14T19:00",
    "end": "2026-08-14T20:30",
    "csrf": "test-csrf",
}


@pytest.fixture
def stored_backend(backend):
    """A backend that accepts a PUT, as Radicale does for a new event."""
    backend.response = httpx.Response(201)
    return backend


def test_form_requires_a_session(client):
    response = client.get("/events/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_form_renders_with_defaults(client):
    login(client)
    response = client.get("/events/new")
    assert response.status_code == 200
    assert 'name="summary"' in response.text
    assert 'type="datetime-local"' in response.text


def test_create_event_puts_the_document(client, stored_backend, settings):
    login(client)
    response = client.post("/events", data=FORM, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/calendar?year=2026&month=8"

    assert len(stored_backend.requests) == 1
    request = stored_backend.last
    assert request.method == "PUT"
    assert request.url.path.startswith(settings.shared_path)
    assert request.url.path.endswith(".ics")
    assert request.headers["x-remote-user"] == settings.shared_principal
    assert request.headers["content-type"].startswith("text/calendar")
    assert request.headers["if-none-match"] == "*"

    component = vevent(request.content)
    assert str(component["SUMMARY"]) == "Village hall AGM"
    assert str(component["LOCATION"]) == "Village Hall"
    assert component[events.CREATED_BY_PROPERTY] == "alice"
    assert b"VALARM" not in request.content
    # The filename is the UID, which is what CalDAV clients expect.
    assert request.url.path.endswith(f"{component['UID']}.ics")


def test_created_event_uses_the_configured_timezone(tmp_path, backend):
    settings = Settings(
        _env_file=None,
        session_secret="test-session-secret-long-enough-for-the-length-check",
        oidc_issuer="https://idm.example.org/oauth2/openid/calendar",
        oidc_client_id="calendar",
        oidc_client_secret="secret",
        cookie_secure=False,
        database_path=str(tmp_path / "tz.db"),
        radicale_url="http://radicale.test:5232",
        display_timezone="America/Los_Angeles",
    )
    backend.response = httpx.Response(201)
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, backend=backend.client, bootstrap=False)) as client:
        client.cookies.set("session", session_cookie())
        assert client.post("/events", data=FORM, follow_redirects=False).status_code == 303

    assert b"DTSTART:20260815T020000Z" in backend.last.content


def test_all_day_event_is_created_from_dates(client, stored_backend):
    login(client)
    response = client.post(
        "/events",
        data={**FORM, "start": "2026-08-14", "end": "2026-08-14", "all_day": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert b"DTSTART;VALUE=DATE:20260814" in stored_backend.last.content


def test_invalid_form_redisplays_without_touching_the_backend(client, backend):
    login(client)
    response = client.post("/events", data={**FORM, "summary": ""})
    assert response.status_code == 200
    assert "Please give the event a title." in response.text
    # The rest of what was typed is still in the form.
    assert "Bring a chair" in response.text
    assert backend.requests == []


def test_redisplayed_values_are_escaped(client, backend):
    login(client)
    response = client.post(
        "/events", data={**FORM, "start": "nope", "description": "</textarea><script>x</script>"}
    )
    assert response.status_code == 200
    # The template has an inline script of its own, so look for the injected one.
    assert "<script>x</script>" not in response.text
    assert "&lt;/textarea&gt;&lt;script&gt;x&lt;/script&gt;" in response.text


def test_create_event_requires_csrf(client, backend):
    login(client)
    response = client.post("/events", data={**FORM, "csrf": "wrong"})
    assert response.status_code == 403
    assert backend.requests == []


def test_create_event_requires_a_session(client, backend):
    response = client.post("/events", data=FORM)
    assert response.status_code == 401
    assert backend.requests == []


def test_backend_failure_is_reported_on_the_form(client, backend):
    backend.response = httpx.Response(500, content=b"boom")
    login(client)
    response = client.post("/events", data=FORM)
    assert response.status_code == 200
    assert "could not be updated" in response.text


def test_success_notice_appears_on_the_calendar(client, stored_backend):
    login(client)
    client.post("/events", data=FORM, follow_redirects=False)
    stored_backend.response = httpx.Response(207, content=b"<multistatus/>")
    page = client.get("/calendar?year=2026&month=8")
    assert "Village hall AGM" in page.text  # in the flash notice
    # Flashes are shown once.
    assert "Added" not in client.get("/calendar?year=2026&month=8").text
