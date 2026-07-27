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
from .caldav import ensure_collection
from .config import Settings, get_settings
from .dav_proxy import create_dav_router
from .db import Database
from .passwords import PasswordStore, RateLimiter
from .web import create_web_router

BASE_DIR = Path(__file__).resolve().parent

log = logging.getLogger(__name__)

REQUIRED_SETTINGS = ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "session_secret")


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
        )
        app.state.auth_limiter = RateLimiter(settings.auth_rate_limit, settings.auth_rate_window)
        if bootstrap:
            try:
                await ensure_collection(app.state.backend, settings)
            except httpx.HTTPError as exc:
                log.error("could not reach calendar backend during startup: %s", exc)
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
