"""Web UI: the read-only calendar viewer and app-password management."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

import anyio
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import viewer
from .auth import csrf_token, current_user, require_user, verify_csrf
from .caldav import fetch_collection_ics, fetch_ctag, fetch_window
from .config import Settings
from .feeds import FeedLimitReached, feed_url
from .passwords import PasswordLimitReached

log = logging.getLogger(__name__)

FLASH_SECRET_KEY = "new_secret"
FLASH_ERROR_KEY = "password_error"
FLASH_FEED_ERROR_KEY = "feed_error"

# Long enough that a subscriber refetching immediately is served from cache, far
# short of how long a calendar edit may wait to appear. Private so nothing in
# between keeps a copy of a body the token is supposed to gate.
FEED_CACHE_CONTROL = "private, max-age=300"


def _matches_etag(header: str | None, etag: str) -> bool:
    """Whether an If-None-Match header covers ``etag`` (RFC 9110 13.1.2)."""
    if not header:
        return False
    candidates = [value.strip() for value in header.split(",")]
    if "*" in candidates:
        return True
    # A cache that stored the body after a compressing proxy touched it may send
    # the tag back weakened, and it is still the same collection state.
    return any(value.removeprefix("W/") == etag for value in candidates)


def create_web_router(settings: Settings, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    tz = viewer.get_timezone(settings.display_timezone)

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            name,
            {
                "user": current_user(request),
                "settings": settings,
                "csrf_token": csrf_token(request),
                **context,
            },
        )

    @router.get("/", include_in_schema=False)
    async def index(request: Request):
        if current_user(request) is not None:
            return RedirectResponse("/calendar", status_code=303)
        errors = {
            "login_failed": "Sign-in did not complete. Please try again.",
            "no_username": (
                f"{settings.oidc_provider_name} did not return a username for your account."
            ),
        }
        return render(request, "login.html", error=errors.get(request.query_params.get("error", "")))

    async def _expand_horizon(
        backend,
        calendar_cache,
        expansion_cache: viewer.ExpansionCache,
        ctag: str | None,
        start: datetime,
        end: datetime,
    ) -> list[viewer.Event]:
        """Events across the countdown horizon, expanded at most once per ctag."""
        key = None if ctag is None else (ctag, start.isoformat(), end.isoformat())
        cached = expansion_cache.get(key)
        if cached is not None:
            return cached
        documents = await fetch_window(backend, settings, calendar_cache, ctag, start, end)
        events = viewer.expand_events(documents, start, end, tz)
        if documents:
            # A failed fetch yields no documents and must not be memoised, or
            # the horizon would look empty until the collection next changed.
            expansion_cache.put(key, events)
        return events

    @router.get("/calendar", include_in_schema=False)
    async def calendar_view(request: Request):
        if current_user(request) is None:
            return RedirectResponse("/", status_code=303)

        now = datetime.now(tz)
        try:
            year = int(request.query_params.get("year", now.year))
            month = int(request.query_params.get("month", now.month))
        except ValueError:
            year, month = now.year, now.month
        if not 1 <= month <= 12 or not 1970 <= year <= 2200:
            year, month = now.year, now.month

        weeks = viewer.month_weeks(year, month)
        window_start = datetime.combine(weeks[0][0], time.min, tzinfo=tz)
        window_end = datetime.combine(weeks[-1][-1] + timedelta(days=1), time.min, tzinfo=tz)

        backend = request.app.state.backend
        calendar_cache = request.app.state.calendar_cache
        # One ctag read covers both windows below.
        ctag = await fetch_ctag(backend, settings)

        documents = await fetch_window(
            backend, settings, calendar_cache, ctag, window_start, window_end
        )
        events = viewer.expand_events(documents, window_start, window_end, tz)

        # Countdowns are relative to today, so they only make sense on the
        # month the user is actually living in.
        countdowns: list[viewer.Countdown] = []
        if settings.countdown_days > 0 and (year, month) == (now.year, now.month):
            # The horizon starts where the grid ends, so the two windows do not
            # overlap and the key stays put for the whole month - anchoring on
            # `now` would mint a new key daily and churn the cache with
            # year-wide fetches.
            horizon_end = window_end + timedelta(days=settings.countdown_days)
            horizon = await _expand_horizon(
                backend, calendar_cache, request.app.state.expansion_cache, ctag, window_end, horizon_end
            )
            countdowns = viewer.countdowns(
                horizon,
                now.date(),
                weeks[-1][-1],
                min_span=settings.countdown_min_span,
                limit=settings.countdown_limit,
            )

        prev_year, prev_month = viewer.shift_month(year, month, -1)
        next_year, next_month = viewer.shift_month(year, month, 1)

        return render(
            request,
            "calendar.html",
            year=year,
            month=month,
            month_title=datetime(year, month, 1, tzinfo=tz).strftime("%B %Y"),
            weekday_labels=viewer.WEEKDAY_LABELS,
            weeks=weeks,
            events_by_date=viewer.group_by_date(events),
            upcoming=viewer.upcoming(events, now),
            countdowns=countdowns,
            today=now.date(),
            prev_link=f"/calendar?year={prev_year}&month={prev_month}",
            next_link=f"/calendar?year={next_year}&month={next_month}",
        )

    @router.get("/passwords", include_in_schema=False)
    async def passwords_page(request: Request):
        if current_user(request) is None:
            return RedirectResponse("/", status_code=303)
        user = require_user(request)
        store = request.app.state.passwords
        passwords = store.list_for_user(user.username)
        return render(
            request,
            "passwords.html",
            passwords=passwords,
            new_secret=request.session.pop(FLASH_SECRET_KEY, None),
            error=request.session.pop(FLASH_ERROR_KEY, None),
            at_limit=len(passwords) >= store.max_passwords,
            max_passwords=store.max_passwords,
            caldav_url=settings.caldav_url,
        )

    @router.post("/passwords", include_in_schema=False)
    async def create_password(
        request: Request,
        label: str = Form(default=""),
        csrf: str = Form(default=""),
    ):
        user = require_user(request)
        verify_csrf(request, csrf)
        store = request.app.state.passwords
        try:
            # argon2 is deliberately slow; hashing on the event loop would stall
            # every other request for the duration, DAV traffic included.
            _, secret = await anyio.to_thread.run_sync(
                store.create, user.username, label or "CalDAV client"
            )
        except PasswordLimitReached as exc:
            request.session[FLASH_ERROR_KEY] = (
                f"You already have {exc.limit} app passwords, the maximum. "
                "Revoke one you no longer use before generating another."
            )
            log.info("app password limit reached for %s", user.username)
            return RedirectResponse("/passwords", status_code=303)
        request.session[FLASH_SECRET_KEY] = secret
        log.info("created app password for %s", user.username)
        return RedirectResponse("/passwords", status_code=303)

    @router.post("/passwords/{password_id}/revoke", include_in_schema=False)
    async def revoke_password(request: Request, password_id: int, csrf: str = Form(default="")):
        user = require_user(request)
        verify_csrf(request, csrf)
        store = request.app.state.passwords
        if store.revoke(user.username, password_id):
            log.info("revoked app password %s for %s", password_id, user.username)
        return RedirectResponse("/passwords", status_code=303)

    @router.get("/feeds", include_in_schema=False)
    async def feeds_page(request: Request):
        if current_user(request) is None:
            return RedirectResponse("/", status_code=303)
        user = require_user(request)
        store = request.app.state.feeds
        feeds = store.list_for_user(user.username)
        return render(
            request,
            "feeds.html",
            feeds=[(feed, feed_url(settings, feed)) for feed in feeds],
            error=request.session.pop(FLASH_FEED_ERROR_KEY, None),
            at_limit=len(feeds) >= store.max_feeds,
            max_feeds=store.max_feeds,
        )

    @router.post("/feeds", include_in_schema=False)
    async def create_feed(
        request: Request,
        label: str = Form(default=""),
        csrf: str = Form(default=""),
    ):
        user = require_user(request)
        verify_csrf(request, csrf)
        store = request.app.state.feeds
        try:
            feed = store.create(user.username, label or "Calendar feed")
        except FeedLimitReached as exc:
            request.session[FLASH_FEED_ERROR_KEY] = (
                f"You already have {exc.limit} feed links, the maximum. "
                "Revoke one you no longer use before creating another."
            )
            log.info("ics feed limit reached for %s", user.username)
            return RedirectResponse("/feeds", status_code=303)
        # id only, never the token: it is the credential, and this lands in logs.
        log.info("created ics feed %s for %s", feed.id, user.username)
        return RedirectResponse("/feeds", status_code=303)

    @router.post("/feeds/{feed_id}/revoke", include_in_schema=False)
    async def revoke_feed(request: Request, feed_id: int, csrf: str = Form(default="")):
        user = require_user(request)
        verify_csrf(request, csrf)
        if request.app.state.feeds.revoke(user.username, feed_id):
            log.info("revoked ics feed %s for %s", feed_id, user.username)
        return RedirectResponse("/feeds", status_code=303)

    @router.get("/feeds/{token}.ics", include_in_schema=False)
    async def ics_feed(request: Request, token: str) -> Response:
        """Serve the shared calendar to whoever holds the token in the URL.

        No session and no Basic auth: hosted services like Google Calendar and
        Outlook.com fetch this anonymously, so the token is the whole credential.
        """
        store = request.app.state.feeds
        limiter = request.app.state.feed_limiter
        client_ip = request.client.host if request.client else "-"
        if limiter.is_blocked(client_ip):
            log.warning("rate limited ics feed attempts from %s", client_ip)
            return Response(status_code=429, headers={"Retry-After": str(settings.auth_rate_window)})

        feed = store.lookup(token)
        if feed is None:
            # Identical for an unknown token and a revoked one: the difference
            # would confirm that a token had once been valid.
            limiter.record_failure(client_ip)
            log.info("unknown ics feed token from %s", client_ip)
            return Response(status_code=404, content=b"not found")
        limiter.reset(client_ip)

        backend = request.app.state.backend
        ctag = await fetch_ctag(backend, settings)
        headers = {
            "Cache-Control": FEED_CACHE_CONTROL,
            # The token sits in the path, so keep it out of anywhere this body
            # might link onward from, and out of search indexes.
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex",
        }
        etag = f'"{ctag}"' if ctag else None
        if etag:
            headers["ETag"] = etag
            if _matches_etag(request.headers.get("if-none-match"), etag):
                # The common case by far: subscribers poll far more often than
                # the calendar changes, and this answers without a body.
                store.touch(feed.id)
                return Response(status_code=304, headers=headers)

        cache = request.app.state.ics_cache
        body = cache.get(ctag)
        if body is None:
            body = await fetch_collection_ics(backend, settings)
            if body is None:
                # 503, not 502: subscribers should treat this as retry-later and
                # keep the events they already have.
                return Response(status_code=503, content=b"calendar backend unavailable")
            cache.put(ctag, body)

        store.touch(feed.id)
        headers["Content-Disposition"] = 'inline; filename="calendar.ics"'
        return Response(
            content=body,
            media_type="text/calendar; charset=utf-8",
            headers=headers,
        )

    @router.get("/.well-known/caldav", include_in_schema=False)
    async def well_known_caldav():
        # Clients follow this to discover the DAV root, then look up the principal.
        return RedirectResponse(settings.dav_prefix + "/", status_code=301)

    @router.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> Response:
        try:
            probe = await request.app.state.backend.request(
                "OPTIONS",
                settings.radicale_url.rstrip("/") + "/",
                headers={"X-Remote-User": settings.shared_principal},
            )
            backend_ok = probe.status_code < 500
        except Exception:  # noqa: BLE001 - health checks must never raise
            backend_ok = False
        return Response(
            content=b'{"status":"ok"}' if backend_ok else b'{"status":"degraded"}',
            status_code=200 if backend_ok else 503,
            media_type="application/json",
        )

    return router
