# calendar-proxy

A shared community calendar that authenticates against [kanidm](https://kanidm.com/).

- **Web viewer** — sign in with kanidm, see the month at a glance.
- **CalDAV** — works with Thunderbird, DAVx⁵, Apple Calendar, and anything else that speaks CalDAV.
- **App passwords** — because CalDAV clients can't do an OAuth handshake, users generate
  HTTP Basic credentials in the web UI and paste those into their client.

Storage is handled by [Radicale](https://radicale.org/); this app is the authentication
and presentation layer in front of it.

## How it works

```
                          ┌────────────────────────────────────────┐
 Browser ── OIDC ───────► │  calendar-proxy (FastAPI)              │
 (viewer, app passwords)  │  • OIDC login against kanidm           │
                          │  • month view + upcoming agenda        │
                          │  • app password CRUD (argon2, SQLite)  │ ──► Radicale
 CalDAV client ─ Basic ─► │  • /dav/* → verify Basic auth, forward │     (internal only)
 (Thunderbird, DAVx⁵…)    │    WebDAV with X-Remote-User           │
                          └────────────────────────────────────────┘
```

Radicale runs with `auth type = http_x_remote_user`, meaning it takes the authenticated
identity from a header the fronting proxy sets. **Radicale must therefore never be
reachable from outside** — the compose file puts it on an `internal: true` network with no
published ports, so only calendar-proxy can talk to it.

Every authenticated user is mapped onto the *same* Radicale principal (`community` by
default), so everyone reads and writes one shared collection at `/community/shared/`.
Authorization is simply "can you authenticate with kanidm" — there is no group gating.

The proxy sends `X-Script-Name: /dav` so Radicale emits hrefs under the public prefix
(`/dav/community/shared/`) rather than its own root, which is what makes client
auto-discovery work through the proxy.

## Setup

### 1. Register the OIDC client in kanidm

```sh
kanidm system oauth2 create calendar "Community Calendar" https://cal.example.org
kanidm system oauth2 add-redirect-url calendar https://cal.example.org/auth/callback
kanidm system oauth2 update-scope-map calendar <your-users-group> openid profile email
kanidm system oauth2 show-basic-secret calendar
```

Any account that can complete this flow gets calendar access. If you later want to
restrict it, narrow the scope map to a dedicated group instead of `<your-users-group>`.

The issuer URL is `https://<your-kanidm>/oauth2/openid/calendar`.

### 2. Configure

```sh
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'   # SESSION_SECRET
$EDITOR .env
```

| Variable | Purpose |
| --- | --- |
| `PUBLIC_BASE_URL` | Public origin, used for the redirect URI and the CalDAV URL shown to users |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | kanidm OAuth2 client |
| `SESSION_SECRET` | Signs the session cookie |
| `COOKIE_SECURE` | Leave `true` in production; `false` only for plain-HTTP local testing |
| `DISPLAY_TIMEZONE` | IANA zone the web viewer renders times in |
| `SHARED_DISPLAY_NAME` | Calendar name clients will show |

### 3. Run

```sh
docker compose up -d --build
```

The shared calendar collection is created automatically on first start. The proxy listens
on `127.0.0.1:8000`; point your existing reverse proxy at it and terminate TLS there.

Caddy:

```
cal.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

nginx — make sure WebDAV verbs and request bodies pass through untouched:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 20m;
}
```

## Using it

Sign in, open **CalDAV access**, and generate an app password. The page shows everything a
client needs:

| Field | Value |
| --- | --- |
| URL | `https://cal.example.org/dav/community/shared/` |
| Username | your kanidm username |
| Password | the generated app password (shown once) |

`/.well-known/caldav` redirects to the DAV root, so clients that auto-discover only need
`https://cal.example.org`.

Secrets are stored as argon2 hashes and are never recoverable — revoke and regenerate if
one is lost. Revocation takes effect immediately.

The web viewer is read-only; create and edit events from a CalDAV client.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The integration tests exercise the whole chain against a real Radicale and are skipped
unless you point them at one:

```sh
.venv/bin/pip install "radicale>=3.3,<4"
mkdir -p /tmp/radicale-test
sed 's|/data/collections|/tmp/radicale-test|; s|0.0.0.0:5232|127.0.0.1:5232|' \
    radicale/config > /tmp/radicale-test.conf
.venv/bin/radicale --config /tmp/radicale-test.conf &

RADICALE_TEST_URL=http://127.0.0.1:5232 .venv/bin/python -m pytest
```

To run the app itself outside Docker, export the same variables as `.env` (plus
`RADICALE_URL=http://127.0.0.1:5232`) and:

```sh
.venv/bin/uvicorn app.main:build --factory --reload
```

### Layout

| Path | Contents |
| --- | --- |
| `app/config.py` | Environment-driven settings |
| `app/auth.py` | kanidm OIDC login, session and CSRF helpers |
| `app/passwords.py` | App password store, Basic-auth parsing, rate limiting |
| `app/dav_proxy.py` | The `/dav/*` CalDAV reverse proxy |
| `app/caldav.py` | Server-side CalDAV client (bootstrap + viewer fetches) |
| `app/viewer.py` | Recurrence expansion and month-grid construction |
| `app/web.py` | Web routes and templates glue |
| `radicale/` | Radicale image and configuration |

## Operational notes

- **Back up** the `radicale-data` volume (the calendar itself) and the `proxy-data` volume
  (the app password database).
- Failed Basic-auth attempts are rate limited per IP and username
  (`AUTH_RATE_LIMIT` / `AUTH_RATE_WINDOW`).
- Successful verifications are cached for `AUTH_CACHE_TTL` seconds so argon2 isn't re-run
  on every poll; a revocation clears the cache immediately.
- `GET /healthz` reports whether Radicale is reachable.

## Possible extensions

- Per-user calendars alongside the shared one: pass the real username in `X-Remote-User`
  and add a Radicale rights file granting everyone read/write on the shared collection.
- An "add event" form in the web viewer — the proxy can `PUT` to Radicale itself.
- Group gating, if the community ever wants the calendar limited to a subset of accounts.
