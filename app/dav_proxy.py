"""CalDAV reverse proxy.

Authenticates the request with an app password, then forwards the WebDAV verb
to Radicale. Radicale runs with ``auth type = http_x_remote_user`` and is only
reachable on the internal network, so it trusts the ``X-Remote-User`` header we
set here. Every user is mapped to the same principal, giving one shared calendar.
"""

from __future__ import annotations

import logging

import anyio
import httpx
from fastapi import APIRouter, Request, Response

from .config import Settings
from .passwords import parse_basic_auth

log = logging.getLogger(__name__)

DAV_METHODS = [
    "GET",
    "HEAD",
    "OPTIONS",
    "POST",
    "PUT",
    "DELETE",
    "PROPFIND",
    "PROPPATCH",
    "REPORT",
    "MKCALENDAR",
    "MKCOL",
    "MOVE",
    "COPY",
    "LOCK",
    "UNLOCK",
]

# Hop-by-hop headers must not be forwarded in either direction (RFC 9110 7.6.1).
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Headers we always replace or must never let a client inject: the auth headers
# are ours to set, and X-Remote-User is exactly what Radicale trusts.
REQUEST_STRIP = HOP_BY_HOP | {
    "authorization",
    "cookie",
    "host",
    "content-length",
    "x-remote-user",
    "x-script-name",
}

# httpx transparently decodes the body, so the original framing headers are stale.
RESPONSE_STRIP = HOP_BY_HOP | {"content-encoding", "content-length"}

UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Basic realm="Shared Calendar", charset="UTF-8"'}


def filter_request_headers(headers, *, remote_user: str, script_name: str) -> dict[str, str]:
    """Build the header set sent to Radicale from the client's headers."""
    out = {k: v for k, v in headers.items() if k.lower() not in REQUEST_STRIP}
    out["X-Remote-User"] = remote_user
    # Tells Radicale which path prefix it is mounted under, so the hrefs it
    # returns in multistatus bodies resolve correctly on the client side.
    out["X-Script-Name"] = script_name
    return out


def filter_response_headers(headers) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers.multi_items() if k.lower() not in RESPONSE_STRIP]


def create_dav_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.api_route(settings.dav_prefix + "{path:path}", methods=DAV_METHODS, include_in_schema=False)
    async def dav(request: Request, path: str) -> Response:
        store = request.app.state.passwords
        limiter = request.app.state.auth_limiter

        credentials = parse_basic_auth(request.headers.get("authorization"))
        if credentials is None:
            return Response(status_code=401, headers=UNAUTHORIZED_HEADERS)

        username, secret = credentials
        client_ip = request.client.host if request.client else "-"
        limit_key = f"{client_ip}|{username}"
        if limiter.is_blocked(limit_key):
            log.warning("rate limited CalDAV auth attempts for %s from %s", username, client_ip)
            return Response(status_code=429, headers={"Retry-After": str(settings.auth_rate_window)})

        ok = await anyio.to_thread.run_sync(store.verify, username, secret)
        if not ok:
            limiter.record_failure(limit_key)
            log.info("failed CalDAV auth for %s from %s", username, client_ip)
            return Response(status_code=401, headers=UNAUTHORIZED_HEADERS)
        limiter.reset(limit_key)

        backend: httpx.AsyncClient = request.app.state.backend
        body = await request.body()
        headers = filter_request_headers(
            request.headers,
            remote_user=settings.shared_principal,
            script_name=settings.dav_prefix,
        )
        # Radicale's MOVE/COPY handler compares the Destination header's host
        # against the request host, so keep the client's Host intact.
        if host := request.headers.get("host"):
            headers["Host"] = host

        target = settings.radicale_url.rstrip("/") + (path or "/")
        try:
            upstream = await backend.request(
                request.method,
                target,
                params=dict(request.query_params),
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            log.error("CalDAV backend request failed: %s", exc)
            return Response(status_code=502, content=b"calendar backend unavailable")

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=dict(filter_response_headers(upstream.headers)),
        )

    return router
