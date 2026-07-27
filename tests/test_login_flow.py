"""Full OIDC login against the mock provider in dev/.

This drives the real authorization-code flow end to end - discovery, PKCE,
code exchange, id_token signature validation - rather than forging a session
cookie the way the other web tests do.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from .conftest import SESSION_SECRET


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def mock_idp():
    """Run dev/mock_oidc.py on a real port; authlib makes real HTTP calls to it."""
    port = _free_port()
    os.environ["MOCK_OIDC_ISSUER"] = f"http://127.0.0.1:{port}"

    from dev.mock_oidc import app as idp_app

    server = uvicorn.Server(uvicorn.Config(idp_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "mock identity provider did not start"

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)
    os.environ.pop("MOCK_OIDC_ISSUER", None)


@pytest.fixture
def client(mock_idp, tmp_path):
    settings = Settings(
        _env_file=None,
        public_base_url="http://testserver",
        oidc_issuer=mock_idp,
        oidc_client_id="calendar",
        oidc_client_secret="dev-secret",
        oidc_provider_name="the mock provider",
        session_secret=SESSION_SECRET,
        cookie_secure=False,
        database_path=str(tmp_path / "login.db"),
        radicale_url="http://radicale.invalid:5232",
    )
    backend = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(207, content=b"<multistatus/>"))
    )
    app = create_app(settings, backend=backend, bootstrap=False)
    with TestClient(app) as test_client:
        yield test_client


def sign_in(client: TestClient, username: str) -> httpx.Response:
    """Walk the browser's part of the flow: proxy -> IdP -> back to proxy."""
    start = client.get("/login", follow_redirects=False)
    assert start.status_code in (302, 303, 307), start.status_code
    authorize_url = start.headers["location"]

    with httpx.Client(follow_redirects=False) as browser:
        form = browser.get(authorize_url)
        assert form.status_code == 200
        state = re.search(r'name="state" value="([^"]+)"', form.text).group(1)

        submitted = browser.post(authorize_url, data={"state": state, "username": username})

    assert submitted.status_code == 303
    callback = submitted.headers["location"]
    assert callback.startswith("http://testserver/auth/callback")
    return client.get(callback, follow_redirects=False)


def test_full_login_flow_signs_the_user_in(client):
    landing = client.get("/")
    assert "Sign in with the mock provider" in landing.text

    callback = sign_in(client, "alice")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/calendar"

    page = client.get("/passwords")
    assert page.status_code == 200
    assert "alice" in page.text


def test_username_comes_from_the_configured_claim(client):
    sign_in(client, "bob.smith")
    client.post("/passwords", data={"label": "Phone", "csrf": _csrf(client)})
    # preferred_username, not sub ("mock-bob.smith") or name ("Bob Smith").
    assert [p.username for p in client.app.state.passwords.list_for_user("bob.smith")] == ["bob.smith"]


def test_logout_clears_the_session(client):
    sign_in(client, "alice")
    assert client.get("/calendar").status_code == 200

    client.get("/logout")
    assert client.get("/calendar", follow_redirects=False).headers["location"] == "/"


def test_callback_with_a_bogus_code_is_rejected(client):
    client.get("/login", follow_redirects=False)  # establishes state in the session
    response = client.get("/auth/callback?code=not-a-real-code&state=wrong", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?error=login_failed"


def test_app_password_created_after_real_login_authenticates_caldav(client):
    sign_in(client, "alice")
    client.post("/passwords", data={"label": "Phone", "csrf": _csrf(client)})

    page = client.get("/passwords")
    assert "Phone" in page.text
    assert len(client.app.state.passwords.list_for_user("alice")) == 1


def _csrf(client: TestClient) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/passwords").text).group(1)
