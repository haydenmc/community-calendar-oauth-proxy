from __future__ import annotations

import re

import httpx
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import login

MULTISTATUS = b"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:response>
    <D:href>/community/shared/game.ics</D:href>
    <D:propstat><D:prop><C:calendar-data>BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:game-night
DTSTART:20260302T190000Z
DTEND:20260302T210000Z
SUMMARY:Game night
END:VEVENT
END:VCALENDAR
</C:calendar-data></D:prop></D:propstat>
  </D:response>
</D:multistatus>"""


def test_anonymous_visitor_sees_sign_in(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Sign in with SSO" in response.text


def test_anonymous_calendar_redirects_to_landing(client):
    response = client.get("/calendar", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_calendar_view_renders_events(client, backend):
    backend.response = httpx.Response(207, content=MULTISTATUS)
    login(client)

    response = client.get("/calendar?year=2026&month=3")
    assert response.status_code == 200
    assert "March 2026" in response.text
    assert "Game night" in response.text
    # The viewer talks to Radicale directly rather than through the DAV proxy.
    assert backend.last.method == "REPORT"
    assert str(backend.last.url) == "http://radicale.test:5232/community/shared/"


def test_calendar_query_is_limited_to_the_displayed_window(client, backend):
    backend.response = httpx.Response(207, content=MULTISTATUS)
    login(client)
    client.get("/calendar?year=2026&month=3")

    body = backend.last.content.decode()
    # The grid for March 2026 runs Sun 1 Mar through Sat 4 Apr inclusive.
    assert '<C:time-range start="20260301T000000Z" end="20260405T000000Z"/>' in body


def test_oversized_calendar_response_renders_an_empty_month(settings, backend):
    capped = settings.model_copy(update={"max_calendar_bytes": 128})
    backend.response = httpx.Response(207, content=b"<multistatus>" + b"x" * 4096)
    app = create_app(capped, backend=backend.client, bootstrap=False)
    with TestClient(app) as client:
        login(client)
        response = client.get("/calendar?year=2026&month=3")
        # Degraded rather than fatal: the page still renders, without events.
        assert response.status_code == 200
        assert "March 2026" in response.text


def test_calendar_view_falls_back_on_bad_month(client, backend):
    backend.response = httpx.Response(207, content=MULTISTATUS)
    login(client)
    assert client.get("/calendar?year=abc&month=99").status_code == 200


def test_passwords_page_shows_caldav_url(client):
    login(client)
    response = client.get("/passwords")
    assert response.status_code == 200
    assert "http://testserver/dav/community/shared/" in response.text


def test_create_and_revoke_password_through_the_ui(client):
    login(client)
    created = client.post("/passwords", data={"label": "Phone", "csrf": "test-csrf"})
    assert created.status_code == 200  # after following the redirect
    assert "Your new app password" in created.text
    assert "Phone" in created.text

    (record,) = client.app.state.passwords.list_for_user("alice")
    revoked = client.post(f"/passwords/{record.id}/revoke", data={"csrf": "test-csrf"})
    assert revoked.status_code == 200
    assert client.app.state.passwords.list_for_user("alice") == []


def test_shown_secret_works_for_caldav_and_is_shown_only_once(client):
    login(client)
    page = client.post("/passwords", data={"label": "Phone", "csrf": "test-csrf"})
    match = re.search(r'<pre class="secret">(.+?)</pre>', page.text, re.DOTALL)
    assert match, "the generated secret should be displayed"
    secret = match.group(1).strip()

    assert client.app.state.passwords.verify("alice", secret)
    assert "Your new app password" not in client.get("/passwords").text


def test_password_limit_is_reported_in_the_ui(settings, backend):
    # A small cap keeps the test quick - each creation runs a real argon2 hash.
    capped = settings.model_copy(update={"max_app_passwords": 2})
    app = create_app(capped, backend=backend.client, bootstrap=False)
    with TestClient(app) as client:
        login(client)
        for _ in range(2):
            client.post("/passwords", data={"label": "Client", "csrf": "test-csrf"})

        refused = client.post("/passwords", data={"label": "One more", "csrf": "test-csrf"})
        assert refused.status_code == 200  # after following the redirect
        assert "Revoke one you no longer use" in refused.text
        assert len(client.app.state.passwords.list_for_user("alice")) == 2
        # The generate form is gone while at the limit, and the error is a flash.
        reloaded = client.get("/passwords")
        assert "Generate app password" not in reloaded.text
        assert "Revoke one you no longer use" not in reloaded.text


def test_post_without_csrf_token_is_rejected(client):
    login(client)
    assert client.post("/passwords", data={"label": "Phone"}).status_code == 403
    assert client.post("/passwords", data={"csrf": "wrong"}).status_code == 403


def test_password_actions_require_a_session(client):
    assert client.post("/passwords", data={"csrf": "test-csrf"}).status_code == 401


def test_users_cannot_revoke_another_users_password(client):
    login(client, "alice")
    client.post("/passwords", data={"label": "Phone", "csrf": "test-csrf"})
    (record,) = client.app.state.passwords.list_for_user("alice")

    login(client, "mallory")
    client.post(f"/passwords/{record.id}/revoke", data={"csrf": "test-csrf"})
    assert len(client.app.state.passwords.list_for_user("alice")) == 1


def test_healthz_reports_backend_state(client, backend):
    backend.response = httpx.Response(200)
    assert client.get("/healthz").json() == {"status": "ok"}

    backend.response = httpx.Response(500)
    assert client.get("/healthz").status_code == 503
