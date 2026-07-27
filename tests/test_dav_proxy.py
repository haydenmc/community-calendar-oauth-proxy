from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app.dav_proxy import filter_request_headers, filter_response_headers
from app.main import create_app

from .conftest import basic_auth


def make_password(client, username="alice"):
    """Create an app password directly in the running app's store."""
    _, secret = client.app.state.passwords.create(username, "test")
    return secret


def test_missing_credentials_challenge(client):
    response = client.request("PROPFIND", "/dav/community/shared/")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic ")


def test_bad_credentials_rejected(client, backend):
    response = client.request(
        "PROPFIND", "/dav/community/shared/", headers=basic_auth("alice", "nope")
    )
    assert response.status_code == 401
    assert backend.requests == []


def test_valid_credentials_are_forwarded(client, backend):
    secret = make_password(client)
    response = client.request(
        "PROPFIND",
        "/dav/community/shared/",
        headers={**basic_auth("alice", secret), "Depth": "1"},
        content=b"<propfind/>",
    )
    assert response.status_code == 207

    forwarded = backend.last
    assert forwarded.method == "PROPFIND"
    assert str(forwarded.url) == "http://radicale.test:5232/community/shared/"
    assert forwarded.headers["Depth"] == "1"
    assert forwarded.content == b"<propfind/>"


def test_forwarded_request_carries_shared_principal(client, backend):
    secret = make_password(client, "bob")
    client.request("PROPFIND", "/dav/community/shared/", headers=basic_auth("bob", secret))

    forwarded = backend.last
    # Everyone is mapped onto the one shared principal, never their own name.
    assert forwarded.headers["X-Remote-User"] == "community"
    assert forwarded.headers["X-Script-Name"] == "/dav"
    assert "authorization" not in forwarded.headers


def test_client_cannot_spoof_remote_user(client, backend):
    secret = make_password(client)
    client.request(
        "PROPFIND",
        "/dav/community/shared/",
        headers={**basic_auth("alice", secret), "X-Remote-User": "root"},
    )
    assert backend.last.headers["X-Remote-User"] == "community"


def test_query_string_and_body_survive(client, backend):
    secret = make_password(client)
    client.request(
        "PUT",
        "/dav/community/shared/event.ics?foo=bar",
        headers=basic_auth("alice", secret),
        content=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    )
    forwarded = backend.last
    assert forwarded.url.params["foo"] == "bar"
    assert forwarded.content.startswith(b"BEGIN:VCALENDAR")


def test_revoked_password_stops_working(client, backend):
    store = client.app.state.passwords
    record, secret = store.create("alice", "test")
    assert client.request("PROPFIND", "/dav/community/shared/", headers=basic_auth("alice", secret)).status_code == 207

    store.revoke("alice", record.id)
    assert client.request("PROPFIND", "/dav/community/shared/", headers=basic_auth("alice", secret)).status_code == 401


def test_repeated_failures_are_rate_limited(client):
    for _ in range(client.app.state.settings.auth_rate_limit):
        client.request("GET", "/dav/community/shared/", headers=basic_auth("alice", "wrong"))
    response = client.request("GET", "/dav/community/shared/", headers=basic_auth("alice", "wrong"))
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_oversized_body_is_rejected(settings, backend):
    capped = settings.model_copy(update={"max_body_bytes": 1024})
    app = create_app(capped, backend=backend.client, bootstrap=False)
    with TestClient(app) as client:
        secret = make_password(client)
        response = client.request(
            "PUT",
            "/dav/community/shared/big.ics",
            headers=basic_auth("alice", secret),
            content=b"x" * 2048,
        )
        assert response.status_code == 413
        assert backend.requests == []

        # A body within the limit still goes through.
        ok = client.request(
            "PUT",
            "/dav/community/shared/small.ics",
            headers=basic_auth("alice", secret),
            content=b"x" * 512,
        )
        assert ok.status_code == 207
        assert backend.last.content == b"x" * 512


def test_backend_failure_becomes_bad_gateway(client, backend):
    secret = make_password(client)

    def explode(request):
        raise httpx.ConnectError("radicale down")

    backend.client._transport = httpx.MockTransport(explode)
    response = client.request("GET", "/dav/community/shared/", headers=basic_auth("alice", secret))
    assert response.status_code == 502


def test_upstream_status_and_headers_pass_through(client, backend):
    secret = make_password(client)
    backend.response = httpx.Response(
        201, headers={"ETag": '"abc123"', "Transfer-Encoding": "chunked"}, content=b""
    )
    response = client.request(
        "PUT", "/dav/community/shared/e.ics", headers=basic_auth("alice", secret), content=b"x"
    )
    assert response.status_code == 201
    assert response.headers["ETag"] == '"abc123"'
    # Hop-by-hop framing headers from upstream must not be replayed.
    assert "transfer-encoding" not in {k.lower() for k in response.headers}


def test_filter_request_headers_strips_sensitive_and_hop_by_hop():
    headers = Headers(
        {
            "Authorization": "Basic xxx",
            "Cookie": "session=abc",
            "Connection": "keep-alive",
            "X-Remote-User": "root",
            "X-Script-Name": "/evil",
            "Depth": "1",
            "Content-Type": "application/xml",
        }
    )
    # Starlette lower-cases header names; HTTP treats them case-insensitively.
    filtered = {
        k.lower(): v
        for k, v in filter_request_headers(
            headers, remote_user="community", script_name="/dav"
        ).items()
    }

    assert filtered["x-remote-user"] == "community"
    assert filtered["x-script-name"] == "/dav"
    assert filtered["depth"] == "1"
    assert filtered["content-type"] == "application/xml"
    for dropped in ("authorization", "cookie", "connection"):
        assert dropped not in filtered


def test_filter_response_headers_drops_stale_framing():
    headers = httpx.Headers(
        {"Content-Type": "application/xml", "Content-Encoding": "gzip", "Content-Length": "9"}
    )
    filtered = {k.lower() for k, _ in filter_response_headers(headers)}
    assert filtered == {"content-type"}


def test_well_known_caldav_redirects(client):
    response = client.get("/.well-known/caldav", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/dav/"
