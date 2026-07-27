from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.caldav import CalendarCache, parse_ctag
from app.main import create_app

from .conftest import login
from .test_web import MULTISTATUS

WINDOW = ("20260301T000000Z", "20260405T000000Z")
OTHER_WINDOW = ("20260405T000000Z", "20260503T000000Z")


def ctag_body(value: str) -> bytes:
    return f"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:CS="http://calendarserver.org/ns/">
  <D:response>
    <D:href>/community/shared/</D:href>
    <D:propstat>
      <D:prop><CS:getctag>{value}</CS:getctag></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""".encode()


# -- the cache in isolation ---------------------------------------------------


def test_entries_are_reused_only_for_the_same_ctag_and_window():
    cache = CalendarCache()
    cache.put("ctag-1", WINDOW, ["doc"])

    assert cache.get("ctag-1", WINDOW) == ["doc"]
    assert cache.get("ctag-1", OTHER_WINDOW) is None
    assert cache.get("ctag-2", WINDOW) is None


def test_a_changed_ctag_drops_every_window():
    cache = CalendarCache()
    cache.put("ctag-1", WINDOW, ["old"])
    cache.put("ctag-1", OTHER_WINDOW, ["old"])

    cache.put("ctag-2", WINDOW, ["new"])
    assert cache.get("ctag-2", WINDOW) == ["new"]
    # The other window was cached under the superseded ctag, so it is gone.
    assert cache.get("ctag-2", OTHER_WINDOW) is None


def test_without_a_ctag_nothing_is_cached():
    cache = CalendarCache()
    cache.put(None, WINDOW, ["doc"])
    assert cache.get(None, WINDOW) is None


def test_windows_are_bounded():
    cache = CalendarCache(max_windows=3)
    for month in range(1, 7):
        cache.put("ctag-1", (f"2026{month:02d}01T000000Z", "end"), [f"doc{month}"])

    assert cache.get("ctag-1", ("20260101T000000Z", "end")) is None  # evicted
    assert cache.get("ctag-1", ("20260601T000000Z", "end")) == ["doc6"]


def test_parse_ctag_handles_missing_and_malformed_bodies():
    assert parse_ctag(ctag_body("abc123")) == "abc123"
    assert parse_ctag(b"<multistatus/>") is None
    assert parse_ctag(b"not xml at all") is None


# -- the cache in the running app ---------------------------------------------


class CtagBackend:
    """Radicale stand-in that answers ctag PROPFINDs and counts REPORTs."""

    def __init__(self) -> None:
        self.ctag = "ctag-1"
        self.reports = 0
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        if request.method == "PROPFIND":
            return httpx.Response(207, content=ctag_body(self.ctag))
        if request.method == "REPORT":
            self.reports += 1
            return httpx.Response(207, content=MULTISTATUS)
        return httpx.Response(207, content=b"<multistatus/>")


@pytest.fixture
def ctag_client(settings):
    backend = CtagBackend()
    app = create_app(settings, backend=backend.client, bootstrap=False)
    with TestClient(app) as client:
        yield client, backend


def test_repeat_views_of_a_month_do_not_refetch(ctag_client):
    client, backend = ctag_client
    login(client)

    for _ in range(3):
        assert "Game night" in client.get("/calendar?year=2026&month=3").text
    assert backend.reports == 1


def test_a_changed_collection_is_picked_up(ctag_client):
    client, backend = ctag_client
    login(client)
    client.get("/calendar?year=2026&month=3")

    backend.ctag = "ctag-2"
    client.get("/calendar?year=2026&month=3")
    assert backend.reports == 2


def test_each_month_is_fetched_once(ctag_client):
    client, backend = ctag_client
    login(client)

    client.get("/calendar?year=2026&month=3")
    client.get("/calendar?year=2026&month=4")
    client.get("/calendar?year=2026&month=3")
    assert backend.reports == 2


def test_a_failed_fetch_is_not_cached(settings):
    """A transient backend failure must not leave the month looking empty."""
    backend = CtagBackend()
    failing = {"value": True}

    def handle(request: httpx.Request) -> httpx.Response:
        request.read()
        if request.method == "PROPFIND":
            return httpx.Response(207, content=ctag_body(backend.ctag))
        if failing["value"]:
            return httpx.Response(500, content=b"boom")
        backend.reports += 1
        return httpx.Response(207, content=MULTISTATUS)

    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = create_app(settings, backend=backend.client, bootstrap=False)
    with TestClient(app) as client:
        login(client)
        assert "Game night" not in client.get("/calendar?year=2026&month=3").text

        failing["value"] = False
        # Same ctag as the failed attempt, so a cached failure would persist.
        assert "Game night" in client.get("/calendar?year=2026&month=3").text
