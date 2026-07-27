"""Application assembly."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import create_auth_router, create_oauth
from .caldav import CalendarCache, CollectionIcsCache, ensure_collection_with_retry
from .config import Settings, get_settings
from .dav_proxy import create_dav_router
from .db import Database
from .feeds import FeedStore
from .passwords import PasswordStore, RateLimiter
from .viewer import ExpansionCache
from .web import create_web_router

BASE_DIR = Path(__file__).resolve().parent

log = logging.getLogger(__name__)

REQUIRED_SETTINGS = ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "session_secret")

# The placeholder shipped in .env.example, in the spellings someone might retype.
PLACEHOLDER_SECRETS = frozenset({"change-me", "changeme", "change_me"})
MIN_SESSION_SECRET_LENGTH = 32

GENERATE_HINT = "generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def check_settings(settings: Settings) -> None:
    missing = [name for name in REQUIRED_SETTINGS if not getattr(settings, name)]
    if missing:
        raise RuntimeError(
            "missing required configuration: " + ", ".join(sorted(missing))
        )

    # Session cookies are signed, not encrypted: anyone who knows the secret can
    # mint a session for any username and take the calendar over. Copying
    # .env.example and missing this one line is the likeliest way that happens,
    # so refuse to start rather than serve forgeable sessions.
    secret = settings.session_secret.strip()
    if secret.lower() in PLACEHOLDER_SECRETS:
        raise RuntimeError(f"SESSION_SECRET is still the .env.example placeholder - {GENERATE_HINT}")
    if len(secret) < MIN_SESSION_SECRET_LENGTH:
        raise RuntimeError(
            f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters, "
            f"got {len(secret)} - {GENERATE_HINT}"
        )
    if settings.oidc_client_secret.strip().lower() in PLACEHOLDER_SECRETS:
        raise RuntimeError(
            "OIDC_CLIENT_SECRET is still the .env.example placeholder - set the client "
            "secret issued by your identity provider"
        )


def create_app(
    settings: Settings | None = None,
    *,
    backend: httpx.AsyncClient | None = None,
    bootstrap: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    check_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.backend = backend or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), follow_redirects=False
        )
        app.state.db = Database(settings.database_path)
        app.state.passwords = PasswordStore(
            app.state.db,
            cache_ttl=settings.auth_cache_ttl,
            last_used_throttle=settings.last_used_throttle,
            max_passwords=settings.max_app_passwords,
        )
        app.state.feeds = FeedStore(
            app.state.db,
            last_used_throttle=settings.last_used_throttle,
            max_feeds=settings.max_ics_feeds,
        )
        app.state.auth_limiter = RateLimiter(settings.auth_rate_limit, settings.auth_rate_window)
        # Its own limiter: the feed endpoint is unauthenticated and keyed by IP
        # alone, so sharing state with Basic-auth failures would let either kind
        # of probing lock out the other.
        app.state.feed_limiter = RateLimiter(settings.auth_rate_limit, settings.auth_rate_window)
        app.state.calendar_cache = CalendarCache(max_windows=settings.calendar_cache_windows)
        app.state.ics_cache = CollectionIcsCache()
        app.state.expansion_cache = ExpansionCache()
        if bootstrap:
            await ensure_collection_with_retry(app.state.backend, settings)
        try:
            yield
        finally:
            app.state.db.close()
            if backend is None:
                await app.state.backend.aclose()

    app = FastAPI(title=settings.site_title, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        max_age=settings.session_max_age,
        https_only=settings.cookie_secure,
        same_site="lax",
    )

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    # The DAV proxy is registered first so its catch-all never shadows web routes.
    app.include_router(create_dav_router(settings))
    app.include_router(create_auth_router(settings, create_oauth(settings)))
    app.include_router(create_web_router(settings, templates))
    return app


def build() -> FastAPI:
    configure_logging()
    return create_app()
