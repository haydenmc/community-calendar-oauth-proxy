"""A throwaway OpenID Connect provider for local development.

This exists so you can exercise the real login path - discovery, PKCE, code
exchange, id_token signature validation, userinfo - without standing up kanidm
or any other identity provider. Sign in as anybody by typing a username.

It is deliberately not part of the application package and is never copied into
the container image. Do not run it anywhere that matters: it accepts any client
credentials and issues tokens for any username asked of it.

    uvicorn dev.mock_oidc:app --port 8080

Then point the proxy at http://127.0.0.1:8080 as its OIDC_ISSUER.
"""

from __future__ import annotations

import os
import secrets
import time
from urllib.parse import urlencode

from authlib.jose import JsonWebKey, jwt
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

KEY_ID = "dev-key"
TOKEN_LIFETIME = 3600


def issuer() -> str:
    """Where this provider is reachable. Must match what the client is told."""
    return os.environ.get("MOCK_OIDC_ISSUER", "http://127.0.0.1:8080")

_key = JsonWebKey.generate_key("RSA", 2048, options={"kid": KEY_ID}, is_private=True)

# code -> pending authorization, access_token -> claims. Memory only; restarting
# the provider signs everyone out.
_codes: dict[str, dict] = {}
_tokens: dict[str, dict] = {}

LOGIN_PAGE = """<!doctype html>
<title>Mock identity provider</title>
<style>
 body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 26rem; margin: 4rem auto; padding: 0 1rem; }}
 input, button {{ font: inherit; padding: .5rem; width: 100%; box-sizing: border-box; }}
 button {{ margin-top: .75rem; cursor: pointer; }}
 .warn {{ background: #fff4e5; border: 1px solid #f0b775; padding: .75rem; border-radius: 6px; }}
</style>
<h1>Mock identity provider</h1>
<p class="warn"><b>Development only.</b> This issues a valid token for any name
you type. It is not a real identity provider.</p>
<form method="post">
  <input type="hidden" name="state" value="{state}">
  <label>Sign in as
    <input name="username" value="alice" autofocus required>
  </label>
  <button type="submit">Sign in</button>
</form>
"""


async def discovery(request):
    base = issuer()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "userinfo_endpoint": f"{base}/userinfo",
            "jwks_uri": f"{base}/jwks.json",
            "end_session_endpoint": f"{base}/logout",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "scopes_supported": ["openid", "profile", "email", "groups"],
            "claims_supported": ["sub", "preferred_username", "name", "email"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
            ],
        }
    )


async def jwks(request):
    return JSONResponse({"keys": [_key.as_dict(is_private=False, kid=KEY_ID)]})


async def authorize(request):
    """Show a username prompt, then hand back an authorization code."""
    if request.method == "GET":
        params = dict(request.query_params)
        state = secrets.token_urlsafe(16)
        _codes[state] = {
            "redirect_uri": params.get("redirect_uri", ""),
            "nonce": params.get("nonce", ""),
            "client_state": params.get("state", ""),
            "client_id": params.get("client_id", ""),
        }
        return HTMLResponse(LOGIN_PAGE.format(state=state))

    form = await request.form()
    pending = _codes.pop(form.get("state", ""), None)
    if pending is None:
        return HTMLResponse("<p>Unknown or expired login attempt.</p>", status_code=400)

    username = (form.get("username") or "alice").strip()
    code = secrets.token_urlsafe(24)
    _codes[code] = {**pending, "username": username}

    query = urlencode({"code": code, "state": pending["client_state"]})
    return RedirectResponse(f"{pending['redirect_uri']}?{query}", status_code=303)


def _claims_for(username: str, extra: dict | None = None) -> dict:
    return {
        "sub": f"mock-{username}",
        "preferred_username": username,
        "name": username.replace(".", " ").title(),
        "email": f"{username}@example.test",
        **(extra or {}),
    }


async def token(request):
    form = await request.form()
    authorization = _codes.pop(form.get("code", ""), None)
    if authorization is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    now = int(time.time())
    username = authorization["username"]
    access_token = secrets.token_urlsafe(32)
    _tokens[access_token] = _claims_for(username)

    payload = {
        "iss": issuer(),
        "aud": authorization["client_id"] or form.get("client_id", ""),
        "iat": now,
        "exp": now + TOKEN_LIFETIME,
        "auth_time": now,
        **_claims_for(username),
    }
    if authorization["nonce"]:
        payload["nonce"] = authorization["nonce"]

    id_token = jwt.encode({"alg": "RS256", "kid": KEY_ID}, payload, _key).decode()
    return JSONResponse(
        {
            "access_token": access_token,
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": TOKEN_LIFETIME,
        }
    )


async def userinfo(request):
    presented = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    claims = _tokens.get(presented)
    if claims is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    return JSONResponse(claims)


async def logout(request):
    return RedirectResponse(request.query_params.get("post_logout_redirect_uri", "/"), 303)


app = Starlette(
    routes=[
        Route("/.well-known/openid-configuration", discovery),
        Route("/jwks.json", jwks),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
        Route("/userinfo", userinfo),
        Route("/logout", logout),
    ]
)
