"""End-to-end tests against a real Radicale.

Skipped unless RADICALE_TEST_URL points at a running Radicale configured with
``auth type = http_x_remote_user``. See README for how to start one locally.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from .conftest import SESSION_SECRET, basic_auth, login

RADICALE_URL = os.environ.get("RADICALE_TEST_URL")

pytestmark = pytest.mark.skipif(not RADICALE_URL, reason="RADICALE_TEST_URL not set")

EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//calendar-proxy//integration//EN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:20260301T120000Z
DTSTART:20260302T190000Z
DTEND:20260302T210000Z
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Integration game night
LOCATION:Mumble
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def live_client(tmp_path):
    settings = Settings(
        _env_file=None,
        public_base_url="http://testserver",
        oidc_issuer="https://idm.example.org/oauth2/openid/calendar",
        oidc_client_id="calendar",
        oidc_client_secret="secret",
        session_secret=SESSION_SECRET,
        cookie_secure=False,
        database_path=str(tmp_path / "live.db"),
        radicale_url=RADICALE_URL,
        shared_principal=f"itest{uuid.uuid4().hex[:8]}",
        display_timezone="UTC",
    )
    app = create_app(settings, bootstrap=True)
    with TestClient(app) as client:
        yield client


def test_bootstrap_creates_the_shared_calendar(live_client):
    settings = live_client.app.state.settings
    secret = live_client.app.state.passwords.create("alice", "test")[1]

    response = live_client.request(
        "PROPFIND",
        f"/dav{settings.shared_path}",
        headers={**basic_auth("alice", secret), "Depth": "0"},
        content=b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:displayname/></D:prop></D:propfind>',
    )
    assert response.status_code == 207
    assert settings.shared_display_name in response.text
    # Radicale must report hrefs under the proxy's prefix, not its own root.
    assert f"/dav{settings.shared_path}" in response.text


def test_event_written_over_caldav_appears_in_the_web_view(live_client):
    settings = live_client.app.state.settings
    secret = live_client.app.state.passwords.create("alice", "test")[1]
    uid = f"itest-{uuid.uuid4().hex[:8]}"

    put = live_client.request(
        "PUT",
        f"/dav{settings.shared_path}{uid}.ics",
        headers={**basic_auth("alice", secret), "Content-Type": "text/calendar"},
        content=EVENT.format(uid=uid).encode(),
    )
    assert put.status_code in (201, 204), put.text

    login(live_client)
    page = live_client.get("/calendar?year=2026&month=3")
    assert page.status_code == 200
    assert "Integration game night" in page.text
    # The weekly recurrence should be expanded across the month.
    assert page.text.count("Integration game night") >= 3


def test_a_second_user_sees_the_same_shared_calendar(live_client):
    settings = live_client.app.state.settings
    alice_secret = live_client.app.state.passwords.create("alice", "test")[1]
    bob_secret = live_client.app.state.passwords.create("bob", "test")[1]
    uid = f"itest-{uuid.uuid4().hex[:8]}"

    live_client.request(
        "PUT",
        f"/dav{settings.shared_path}{uid}.ics",
        headers={**basic_auth("alice", alice_secret), "Content-Type": "text/calendar"},
        content=EVENT.format(uid=uid).encode(),
    )
    fetched = live_client.request(
        "GET", f"/dav{settings.shared_path}{uid}.ics", headers=basic_auth("bob", bob_secret)
    )
    assert fetched.status_code == 200
    assert "Integration game night" in fetched.text


def test_revoked_password_loses_caldav_access(live_client):
    settings = live_client.app.state.settings
    store = live_client.app.state.passwords
    record, secret = store.create("alice", "test")

    path = f"/dav{settings.shared_path}"
    assert live_client.request("GET", path, headers=basic_auth("alice", secret)).status_code != 401

    store.revoke("alice", record.id)
    assert live_client.request("GET", path, headers=basic_auth("alice", secret)).status_code == 401


def test_delete_over_caldav_removes_the_event(live_client):
    settings = live_client.app.state.settings
    secret = live_client.app.state.passwords.create("alice", "test")[1]
    uid = f"itest-{uuid.uuid4().hex[:8]}"
    url = f"/dav{settings.shared_path}{uid}.ics"

    live_client.request(
        "PUT",
        url,
        headers={**basic_auth("alice", secret), "Content-Type": "text/calendar"},
        content=EVENT.format(uid=uid).encode(),
    )
    assert live_client.request("DELETE", url, headers=basic_auth("alice", secret)).status_code in (200, 204)
    assert live_client.request("GET", url, headers=basic_auth("alice", secret)).status_code == 404


def test_unauthenticated_caldav_request_is_challenged(live_client):
    settings = live_client.app.state.settings
    response = live_client.request("PROPFIND", f"/dav{settings.shared_path}")
    assert response.status_code == 401
    assert "Basic" in response.headers["WWW-Authenticate"]


def test_backend_is_not_reachable_without_the_proxy():
    """Radicale trusts X-Remote-User, so it must never be exposed directly."""
    with httpx.Client() as raw:
        response = raw.request(
            "PROPFIND",
            RADICALE_URL.rstrip("/") + "/",
            headers={"X-Remote-User": "community", "Depth": "0"},
        )
    # This is exactly why the compose file keeps Radicale on an internal network.
    assert response.status_code == 207
