"""SITE_TITLE brands the UI; SHARED_DISPLAY_NAME names the CalDAV collection."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.caldav import ensure_collection, ensure_collection_with_retry, parse_display_name
from app.config import Settings
from app.main import create_app

from .conftest import SESSION_SECRET, RecordingBackend, login

MULTISTATUS_EMPTY = b"<multistatus/>"


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        public_base_url="http://testserver",
        oidc_issuer="https://idm.example.org/oauth2/openid/calendar",
        oidc_client_id="calendar",
        oidc_client_secret="secret",
        session_secret=SESSION_SECRET,
        cookie_secure=False,
        database_path=str(tmp_path / "branding.db"),
        radicale_url="http://radicale.test:5232",
        **overrides,
    )


@pytest.fixture
def branded(tmp_path):
    settings = make_settings(
        tmp_path,
        site_title="Fort Awesome",
        site_tagline="When we are doing things.",
    )
    backend = RecordingBackend()
    app = create_app(settings, backend=backend.client, bootstrap=False)
    with TestClient(app) as client:
        yield client, backend


def test_title_appears_on_the_sign_in_page(branded):
    client, _ = branded
    page = client.get("/")
    assert "<title>Fort Awesome</title>" in page.text
    assert "<h1>Fort Awesome</h1>" in page.text
    assert "When we are doing things." in page.text
    assert "Community Calendar" not in page.text


def test_title_appears_in_the_header_and_page_titles(branded):
    client, backend = branded
    backend.response = httpx.Response(207, content=MULTISTATUS_EMPTY)
    login(client)

    calendar = client.get("/calendar?year=2026&month=3")
    assert 'class="brand"' in calendar.text
    assert "Fort Awesome" in calendar.text
    assert "— Fort Awesome</title>" in calendar.text

    passwords = client.get("/passwords")
    assert "CalDAV access — Fort Awesome</title>" in passwords.text
    assert "Community Calendar" not in passwords.text


def test_tagline_can_be_omitted(tmp_path):
    settings = make_settings(tmp_path, site_title="Fort Awesome", site_tagline="")
    backend = RecordingBackend()
    with TestClient(create_app(settings, backend=backend.client, bootstrap=False)) as client:
        page = client.get("/")
        assert "Fort Awesome" in page.text
        assert '<p class="muted"></p>' not in page.text


def test_site_title_is_independent_of_the_calendar_name(tmp_path):
    """The header can differ from what CalDAV clients call the collection."""
    settings = make_settings(
        tmp_path, site_title="Fort Awesome", shared_display_name="Fort Awesome Events"
    )
    backend = RecordingBackend()
    with TestClient(create_app(settings, backend=backend.client, bootstrap=False)) as client:
        assert "Fort Awesome</title>" in client.get("/").text
    assert settings.shared_display_name == "Fort Awesome Events"


# -- the display name reaching Radicale ---------------------------------------


def existing_collection_named(name: str) -> bytes:
    return (
        b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response>'
        b"<D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>"
        b"<D:displayname>" + name.encode() + b"</D:displayname>"
        b"</D:prop></D:propstat></D:response></D:multistatus>"
    )


async def test_bootstrap_sends_the_display_name_when_creating(tmp_path):
    settings = make_settings(tmp_path, shared_display_name="Fort Awesome Events")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        if request.method == "PROPFIND":
            return httpx.Response(404)
        return httpx.Response(201)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await ensure_collection(client, settings)

    (mkcalendar,) = [r for r in seen if r.method == "MKCALENDAR"]
    assert b"Fort Awesome Events" in mkcalendar.content


async def test_changing_the_display_name_renames_an_existing_collection(tmp_path):
    settings = make_settings(tmp_path, shared_display_name="Fort Awesome Events")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        if request.method == "PROPFIND":
            return httpx.Response(207, content=existing_collection_named("Old Name"))
        return httpx.Response(207)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await ensure_collection(client, settings)

    (proppatch,) = [r for r in seen if r.method == "PROPPATCH"]
    assert b"Fort Awesome Events" in proppatch.content


async def test_unchanged_display_name_does_not_rename(tmp_path):
    settings = make_settings(tmp_path, shared_display_name="Fort Awesome Events")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(207, content=existing_collection_named("Fort Awesome Events"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await ensure_collection(client, settings)

    assert [r.method for r in seen] == ["PROPFIND"]


async def test_display_name_with_xml_characters_is_escaped(tmp_path):
    settings = make_settings(tmp_path, shared_display_name="Bob & Alice's <Calendar>")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(404) if request.method == "PROPFIND" else httpx.Response(201)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await ensure_collection(client, settings)

    (mkcalendar,) = [r for r in seen if r.method == "MKCALENDAR"]
    assert b"Bob &amp; Alice's &lt;Calendar&gt;" in mkcalendar.content


def test_parse_display_name():
    assert parse_display_name(existing_collection_named("Old Name")) == "Old Name"
    assert parse_display_name(b"<not xml") is None


# -- bootstrap resilience -----------------------------------------------------


async def test_bootstrap_retries_until_the_backend_comes_up(tmp_path):
    """Radicale may still be starting; the proxy must not give up on the first try."""
    settings = make_settings(tmp_path)
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(207, content=existing_collection_named(settings.shared_display_name))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert await ensure_collection_with_retry(client, settings, attempts=5, delay=0)
    assert calls["n"] == 3


async def test_bootstrap_gives_up_after_the_last_attempt(tmp_path):
    settings = make_settings(tmp_path)
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert not await ensure_collection_with_retry(client, settings, attempts=4, delay=0)
    assert calls["n"] == 4
