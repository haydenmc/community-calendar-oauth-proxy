from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.passwords import PasswordStore

SESSION_SECRET = "test-session-secret"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        public_base_url="http://testserver",
        oidc_issuer="https://idm.example.org/oauth2/openid/calendar",
        oidc_client_id="calendar",
        oidc_client_secret="secret",
        session_secret=SESSION_SECRET,
        cookie_secure=False,
        database_path=str(tmp_path / "test.db"),
        radicale_url="http://radicale.test:5232",
        display_timezone="UTC",
    )


@pytest.fixture
def store(tmp_path) -> PasswordStore:
    return PasswordStore(Database(str(tmp_path / "passwords.db")), cache_ttl=60, last_used_throttle=0)


class RecordingBackend:
    """Stands in for Radicale, capturing what the proxy forwards."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(207, content=b"<multistatus/>")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.response

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def backend() -> RecordingBackend:
    return RecordingBackend()


@pytest.fixture
def client(settings, backend):
    app = create_app(settings, backend=backend.client, bootstrap=False)
    with TestClient(app) as test_client:
        yield test_client


def session_cookie(username: str = "alice", csrf: str = "test-csrf") -> str:
    """Forge the cookie SessionMiddleware would have set after an OIDC login."""
    payload = {
        "user": {"username": username, "display_name": username.title(), "email": ""},
        "csrf": csrf,
    }
    data = base64.b64encode(json.dumps(payload).encode())
    return TimestampSigner(SESSION_SECRET).sign(data).decode()


def login(test_client: TestClient, username: str = "alice") -> None:
    # Clear first, otherwise switching users leaves the previous session cookie
    # in the jar and httpx sends both.
    test_client.cookies.clear()
    test_client.cookies.set("session", session_cookie(username))


def basic_auth(username: str, secret: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}
