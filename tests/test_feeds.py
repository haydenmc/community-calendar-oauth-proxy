from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.feeds import FeedLimitReached, FeedStore
from app.main import create_app

from .conftest import login

CALENDAR_BODY = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:game-night
DTSTART:20260302T190000Z
DTEND:20260302T210000Z
RRULE:FREQ=WEEKLY
SUMMARY:Game night
END:VEVENT
END:VCALENDAR
"""


def ctag_multistatus(ctag: str) -> bytes:
    return f"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:" xmlns:CS="http://calendarserver.org/ns/">
  <D:response><D:propstat><D:prop><CS:getctag>{ctag}</CS:getctag></D:prop></D:propstat></D:response>
</D:multistatus>""".encode()


class CollectionBackend:
    """Radicale for the feed route: a ctag PROPFIND and a collection GET."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.ctag = "ctag-1"
        self.body = CALENDAR_BODY
        self.get_status = 200
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if request.method == "PROPFIND":
            return httpx.Response(207, content=ctag_multistatus(self.ctag))
        if request.method == "GET":
            return httpx.Response(self.get_status, content=self.body)
        return httpx.Response(405)

    @property
    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]


@pytest.fixture
def feed_store(tmp_path) -> FeedStore:
    return FeedStore(Database(str(tmp_path / "feeds.db")), last_used_throttle=0, max_feeds=3)


@pytest.fixture
def collection_backend() -> CollectionBackend:
    return CollectionBackend()


@pytest.fixture
def feed_client(settings, collection_backend):
    app = create_app(settings, backend=collection_backend.client, bootstrap=False)
    with TestClient(app) as test_client:
        yield test_client


def create_feed_via_ui(test_client: TestClient, label: str = "Google Calendar") -> str:
    """Create a feed through the UI and return its absolute URL."""
    login(test_client)
    test_client.post("/feeds", data={"label": label, "csrf": "test-csrf"})
    page = test_client.get("/feeds")
    match = next(
        (line for line in page.text.splitlines() if "http://testserver/feeds/" in line), None
    )
    assert match, "the feed page did not show a feed URL"
    start = match.index("http://testserver/feeds/")
    return match[start : match.index('"', start)]


# -- store ---------------------------------------------------------------------


def test_created_feed_is_found_by_its_token(feed_store):
    feed = feed_store.create("alice", "Google Calendar")
    assert feed.label == "Google Calendar"
    assert feed_store.lookup(feed.token) == feed


def test_lookup_rejects_unknown_and_revoked_tokens(feed_store):
    feed = feed_store.create("alice", "Google Calendar")
    assert feed_store.lookup(feed.token + "x") is None
    assert feed_store.lookup("") is None
    assert feed_store.revoke("alice", feed.id)
    assert feed_store.lookup(feed.token) is None


def test_revoked_feeds_are_hidden_and_revoke_is_idempotent(feed_store):
    feed = feed_store.create("alice", "Google Calendar")
    assert feed_store.list_for_user("alice") == [feed]
    assert feed_store.revoke("alice", feed.id)
    assert feed_store.list_for_user("alice") == []
    assert not feed_store.revoke("alice", feed.id)


def test_users_cannot_revoke_another_users_feed(feed_store):
    feed = feed_store.create("alice", "Google Calendar")
    assert not feed_store.revoke("bob", feed.id)
    assert feed_store.lookup(feed.token) is not None


def test_feed_limit_is_enforced_per_user(feed_store):
    for _ in range(feed_store.max_feeds):
        feed_store.create("alice", "Feed")
    with pytest.raises(FeedLimitReached):
        feed_store.create("alice", "One too many")
    # Revoking frees a slot, and another user is unaffected.
    feed_store.create("bob", "Feed")
    feed_store.revoke("alice", feed_store.list_for_user("alice")[0].id)
    assert feed_store.create("alice", "Replacement")


def test_touch_is_throttled(tmp_path):
    store = FeedStore(Database(str(tmp_path / "feeds.db")), last_used_throttle=3600)
    feed = store.create("alice", "Feed")
    store.touch(feed.id)
    first = store.get("alice", feed.id).last_used_at
    assert first is not None
    store.touch(feed.id)
    assert store.get("alice", feed.id).last_used_at == first


# -- the public feed route -----------------------------------------------------


def test_feed_url_serves_the_collection_without_a_session(feed_client, collection_backend):
    url = create_feed_via_ui(feed_client)
    feed_client.cookies.clear()

    response = feed_client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.content == CALENDAR_BODY
    # RRULEs reach the subscriber intact; expansion is the client's job.
    assert "RRULE:FREQ=WEEKLY" in response.text
    assert response.headers["etag"] == '"ctag-1"'
    assert response.headers["referrer-policy"] == "no-referrer"
    assert collection_backend.gets[-1].url.path == "/community/shared/"


def test_unchanged_collection_answers_304_without_refetching(feed_client, collection_backend):
    url = create_feed_via_ui(feed_client)
    feed_client.cookies.clear()
    etag = feed_client.get(url).headers["etag"]
    before = len(collection_backend.gets)

    response = feed_client.get(url, headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert not response.content
    assert len(collection_backend.gets) == before


def test_a_changed_collection_is_served_again(feed_client, collection_backend):
    url = create_feed_via_ui(feed_client)
    feed_client.cookies.clear()
    etag = feed_client.get(url).headers["etag"]

    collection_backend.ctag = "ctag-2"
    collection_backend.body = CALENDAR_BODY.replace(b"Game night", b"Board games")
    response = feed_client.get(url, headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert b"Board games" in response.content
    assert response.headers["etag"] == '"ctag-2"'


def test_unknown_and_revoked_tokens_are_indistinguishable(feed_client):
    url = create_feed_via_ui(feed_client)
    login(feed_client)
    feed_id = feed_client.app.state.feeds.list_for_user("alice")[0].id
    feed_client.post(f"/feeds/{feed_id}/revoke", data={"csrf": "test-csrf"})
    feed_client.cookies.clear()

    revoked = feed_client.get(url)
    unknown = feed_client.get("/feeds/nosuchtoken.ics")
    assert revoked.status_code == unknown.status_code == 404
    assert revoked.content == unknown.content


def test_repeated_bad_tokens_are_rate_limited(feed_client, settings):
    for _ in range(settings.auth_rate_limit):
        assert feed_client.get("/feeds/nosuchtoken.ics").status_code == 404
    blocked = feed_client.get("/feeds/nosuchtoken.ics")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == str(settings.auth_rate_window)


def test_a_valid_token_still_works_while_others_are_blocked(feed_client, settings):
    url = create_feed_via_ui(feed_client)
    feed_client.cookies.clear()
    for _ in range(settings.auth_rate_limit - 1):
        feed_client.get("/feeds/nosuchtoken.ics")
    # A success clears the counter, so one guessing client cannot lock a
    # subscriber out from behind the same NAT.
    assert feed_client.get(url).status_code == 200
    assert feed_client.get("/feeds/nosuchtoken.ics").status_code == 404


def test_backend_failure_reports_service_unavailable(feed_client, collection_backend):
    url = create_feed_via_ui(feed_client)
    feed_client.cookies.clear()
    collection_backend.get_status = 500
    assert feed_client.get(url).status_code == 503


def test_oversized_collection_is_not_served(settings, collection_backend):
    capped = settings.model_copy(update={"max_calendar_bytes": 10})
    app = create_app(capped, backend=collection_backend.client, bootstrap=False)
    with TestClient(app) as test_client:
        url = create_feed_via_ui(test_client)
        test_client.cookies.clear()
        assert test_client.get(url).status_code == 503


def test_the_feed_route_does_not_shadow_the_management_page(feed_client):
    login(feed_client)
    assert feed_client.get("/feeds").status_code == 200


# -- management UI -------------------------------------------------------------


def test_feeds_page_requires_a_session(feed_client):
    response = feed_client.get("/feeds", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_created_feed_is_shown_with_its_full_url(feed_client):
    url = create_feed_via_ui(feed_client, "Work Outlook")
    page = feed_client.get("/feeds")
    assert "Work Outlook" in page.text
    assert url in page.text
    assert url.startswith("http://testserver/feeds/")
    assert url.endswith(".ics")


def test_feed_urls_stay_visible_across_visits(feed_client):
    url = create_feed_via_ui(feed_client)
    # Unlike app passwords these are not one-time: a second visit still shows it.
    assert url in feed_client.get("/feeds").text
    assert url in feed_client.get("/feeds").text


def test_revoking_through_the_ui_kills_the_url(feed_client):
    url = create_feed_via_ui(feed_client)
    login(feed_client)
    feed_id = feed_client.app.state.feeds.list_for_user("alice")[0].id

    feed_client.post(f"/feeds/{feed_id}/revoke", data={"csrf": "test-csrf"})
    page = feed_client.get("/feeds")
    assert url not in page.text
    feed_client.cookies.clear()
    assert feed_client.get(url).status_code == 404


def test_users_only_see_their_own_feeds(feed_client):
    url = create_feed_via_ui(feed_client)
    login(feed_client, "bob")
    page = feed_client.get("/feeds")
    assert url not in page.text
    assert "no feed links yet" in page.text


def test_feed_limit_is_reported_in_the_ui(settings, collection_backend):
    capped = settings.model_copy(update={"max_ics_feeds": 1})
    app = create_app(capped, backend=collection_backend.client, bootstrap=False)
    with TestClient(app) as test_client:
        login(test_client)
        test_client.post("/feeds", data={"label": "First", "csrf": "test-csrf"})
        test_client.post("/feeds", data={"label": "Second", "csrf": "test-csrf"})
        page = test_client.get("/feeds")
        assert "Second" not in page.text
        assert "maximum" in page.text


def test_feed_actions_require_csrf_and_a_session(feed_client):
    login(feed_client)
    assert feed_client.post("/feeds", data={"label": "Feed"}).status_code == 403
    assert feed_client.post("/feeds", data={"csrf": "wrong"}).status_code == 403
    assert feed_client.post("/feeds/1/revoke", data={"csrf": "wrong"}).status_code == 403

    feed_client.cookies.clear()
    assert feed_client.post("/feeds", data={"csrf": "test-csrf"}).status_code == 401
