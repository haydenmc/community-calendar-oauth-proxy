"""kanidm OIDC login and session helpers.

Any account that can complete the OIDC handshake with kanidm gets access to the
shared calendar; there is deliberately no group gating.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .config import Settings

log = logging.getLogger(__name__)

PROVIDER = "kanidm"
SESSION_USER_KEY = "user"
SESSION_CSRF_KEY = "csrf"


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    email: str = ""


def create_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name=PROVIDER,
        server_metadata_url=settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={
            "scope": settings.oidc_scopes,
            "code_challenge_method": "S256",
        },
    )
    return oauth


def current_user(request: Request) -> User | None:
    data = request.session.get(SESSION_USER_KEY)
    if not isinstance(data, dict) or not data.get("username"):
        return None
    return User(
        username=data["username"],
        display_name=data.get("display_name") or data["username"],
        email=data.get("email", ""),
    )


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(24)
        request.session[SESSION_CSRF_KEY] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get(SESSION_CSRF_KEY)
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


def _username_from_claims(claims: dict, settings: Settings) -> str | None:
    for key in (settings.oidc_username_claim, "preferred_username", "name", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def create_auth_router(settings: Settings, oauth: OAuth) -> APIRouter:
    router = APIRouter()
    client = oauth.create_client(PROVIDER)

    @router.get("/login", include_in_schema=False)
    async def login(request: Request):
        redirect_uri = settings.public_base_url.rstrip("/") + "/auth/callback"
        return await client.authorize_redirect(request, redirect_uri)

    @router.get("/auth/callback", include_in_schema=False)
    async def callback(request: Request):
        try:
            token = await client.authorize_access_token(request)
        except OAuthError as exc:
            log.warning("OIDC callback failed: %s", exc)
            return RedirectResponse("/?error=login_failed", status_code=303)

        claims = token.get("userinfo") or {}
        if not claims:
            try:
                claims = (await client.userinfo(token=token)) or {}
            except OAuthError as exc:
                log.warning("userinfo lookup failed: %s", exc)

        username = _username_from_claims(dict(claims), settings)
        if not username:
            log.error("no usable username claim in OIDC response")
            return RedirectResponse("/?error=no_username", status_code=303)

        request.session.clear()
        request.session[SESSION_USER_KEY] = {
            "username": username,
            "display_name": claims.get("name") or username,
            "email": claims.get("email", ""),
        }
        log.info("signed in %s", username)
        return RedirectResponse("/calendar", status_code=303)

    @router.get("/logout", include_in_schema=False)
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    return router
