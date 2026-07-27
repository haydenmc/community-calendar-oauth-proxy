# calendar-proxy

A shared community calendar that authenticates against your existing OpenID Connect
identity provider.

- **Web viewer** — sign in with SSO, see the month at a glance.
- **CalDAV** — works with Thunderbird, DAVx⁵, Apple Calendar, and anything else that speaks CalDAV.
- **App passwords** — because CalDAV clients can't do an OAuth handshake, users generate
  HTTP Basic credentials in the web UI and paste those into their client.

Storage is handled by [Radicale](https://radicale.org/); this app is the authentication
and presentation layer in front of it.

## How it works

```
                          ┌────────────────────────────────────────┐
 Browser ── OIDC ───────► │  calendar-proxy (FastAPI)              │
 (viewer, app passwords)  │  • OIDC login against your IdP         │
                          │  • month view + upcoming agenda        │
                          │  • app password CRUD (argon2, SQLite)  │ ──► Radicale
 CalDAV client ─ Basic ─► │  • /dav/* → verify Basic auth, forward │     (internal only)
 (Thunderbird, DAVx⁵…)    │    WebDAV with X-Remote-User           │
                          └────────────────────────────────────────┘
```

Storage is the stock [`tomsquest/docker-radicale`](https://github.com/tomsquest/docker-radicale)
image, configured entirely through `RADICALE_CONFIG_*` environment variables in
`docker-compose.yml` — there is no Radicale config file to mount or maintain in production.

Radicale runs with `auth type = http_x_remote_user`, meaning it takes the authenticated
identity from a header the fronting proxy sets. **Radicale must therefore never be
reachable from outside** — the compose file puts it on an `internal: true` network with no
published ports, so only calendar-proxy can talk to it. If that header mechanism ever fails
to apply, Radicale falls back to denying anonymous access rather than allowing it.

Every authenticated user is mapped onto the *same* Radicale principal (`community` by
default), so everyone reads and writes one shared collection at `/community/shared/`.
Authorization is simply "can you authenticate" — there is no group gating.

The proxy sends `X-Script-Name: /dav` so Radicale emits hrefs under the public prefix
(`/dav/community/shared/`) rather than its own root, which is what makes client
auto-discovery work through the proxy.

## Identity provider

Nothing here is specific to one vendor. Any provider will do as long as it:

- publishes a discovery document at `<issuer>/.well-known/openid-configuration`,
- supports the authorization code flow with PKCE (`S256`), and
- returns a claim usable as a username.

That covers kanidm, Authentik, Keycloak, Zitadel, Authelia, Dex, Okta, Entra ID, Google,
and most others. Configure it with four settings:

| Variable | Meaning |
| --- | --- |
| `OIDC_ISSUER` | Base URL serving the discovery document |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Confidential client credentials |
| `OIDC_SCOPES` | Defaults to `openid profile email` |
| `OIDC_USERNAME_CLAIM` | Defaults to `preferred_username`; use `email` or `sub` if your provider doesn't send it |
| `OIDC_PROVIDER_NAME` | Label on the sign-in button, e.g. `kanidm` |

The redirect URI to register is `<PUBLIC_BASE_URL>/auth/callback`.

The username claim matters more than it looks: it's the Basic-auth username for CalDAV and
the owner of each app password. Pick something stable — if a user's claim value changes,
their existing app passwords stop matching.

### Example: kanidm

```sh
kanidm system oauth2 create calendar "Community Calendar" https://cal.example.org
kanidm system oauth2 add-redirect-url calendar https://cal.example.org/auth/callback
kanidm system oauth2 update-scope-map calendar <your-users-group> openid profile email
kanidm system oauth2 show-basic-secret calendar
```

The issuer is `https://<your-kanidm>/oauth2/openid/calendar`.

Any account that can complete this flow gets calendar access. If you later want to
restrict it, narrow the scope map to a dedicated group instead of `<your-users-group>`.

## Trying it locally

You don't need a real identity provider to run this. `dev/mock_oidc.py` is a throwaway
OIDC provider that lets you sign in as anybody by typing a name:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
./dev/run-local.sh
```

That starts Radicale, the mock provider, and the proxy on loopback and prints
<http://127.0.0.1:8000>. Sign in as any username, generate an app password, and point a
real CalDAV client at `http://127.0.0.1:8000/dav/community/shared/`. State lives in
`.local/` and can be deleted at any time; Ctrl-C stops everything.

The mock exercises the genuine login path — discovery, PKCE, code exchange, signed
`id_token` validation, userinfo — so if it works there, a real provider only differs by
configuration. It is not wired into the application package and never lands in the
container image. **Don't run it anywhere real**: it issues valid tokens for any name asked
of it.

## Deploying

```sh
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'   # SESSION_SECRET
$EDITOR .env
docker compose up -d
```

| Variable | Purpose |
| --- | --- |
| `PUBLIC_BASE_URL` | Public origin, used for the redirect URI and the CalDAV URL shown to users |
| `SESSION_SECRET` | Signs the session cookie |
| `COOKIE_SECURE` | Leave `true` in production; `false` only for plain-HTTP local testing |
| `DISPLAY_TIMEZONE` | IANA zone the web viewer renders times in |
| `SITE_TITLE` | Name in the header, browser tab and sign-in page |
| `SITE_TAGLINE` | Blurb under the sign-in heading; empty to omit |
| `SHARED_DISPLAY_NAME` | Calendar name CalDAV clients will show |

plus the OIDC settings above.

`SITE_TITLE` and `SHARED_DISPLAY_NAME` are deliberately separate: the first brands the web
UI, the second is what appears in someone's calendar app next to their other calendars.
Set them the same if you want. Changing `SHARED_DISPLAY_NAME` later renames the existing
collection on the next start, and clients pick the new name up on their next sync.

The shared calendar collection is created automatically on first start; the proxy waits for
Radicale's healthcheck and retries the bootstrap, so ordering takes care of itself. The
proxy listens on `127.0.0.1:8000`; put a reverse proxy in front of it and terminate TLS
there — see [Reverse proxy](#reverse-proxy) below, the `X-Forwarded-For` details matter.

The compose file runs the published image pinned by version; check the tags on
`ghcr.io/haydenmc/community-calendar-oauth-proxy` when upgrading. To build from source
instead, swap `image:` for `build: .` and use `docker compose up -d --build` — without
`--build`, compose reuses the existing image and your changes won't take effect.

### About the Radicale image

`docker-compose.yml` pins `tomsquest/docker-radicale` by version. Tags are
`<radicale-version>.<image-build>`, so the trailing component is required — `3.7.6.0`, not
`3.7.6`. Check [the tag list](https://hub.docker.com/r/tomsquest/docker-radicale/tags)
before bumping.

Only three settings differ from the image's defaults, and they are set as environment
variables in the compose file:

| Variable | Value | Why |
| --- | --- | --- |
| `RADICALE_CONFIG_AUTH_TYPE` | `http_x_remote_user` | Trust the identity calendar-proxy asserts |
| `RADICALE_CONFIG_RIGHTS_TYPE` | `owner_only` | Everything lives under one principal |
| `RADICALE_CONFIG_WEB_TYPE` | `none` | This app provides the web UI |

The image already defaults to `0.0.0.0:5232` and `/data/collections`, and its entrypoint
chowns `/data` on start, so both named volumes and bind mounts work without any manual
`chown`.

Two consequences of the env-var approach worth knowing:

- It needs `/config` to be writable, so don't add `read_only: true` to that service. The
  entrypoint detects a read-only root filesystem and silently skips applying the variables,
  which would leave Radicale on its default `auth type = none`. If you want a read-only
  container, mount a config file instead — `dev/radicale.conf` has the same settings.
- The config is rewritten from the environment on every start, so the compose file always
  wins over whatever is in the `radicale-config` volume.

### Reverse proxy

> **Why `X-Forwarded-For` handling matters here:** failed CalDAV logins are
> rate limited per client IP, and that IP comes from the `X-Forwarded-For`
> header. If the header can be forged — because the proxy *appends* to the
> client-supplied value instead of replacing it, or because the app trusts the
> header from addresses other than the proxy — anyone can spoof a fresh IP per
> request and brute-force app passwords without ever being throttled. So two
> rules: the proxy must overwrite the header, and the app must honour it only
> from the proxy's address. The image honours it only from
> `FORWARDED_ALLOW_IPS` (never set this to `*`), and every setup below has the
> proxy overwrite the header.

**Traefik, or any containerized reverse proxy (recommended).** Attach the
proxy to the `edge` network and delete the `ports:` mapping from
`calendar-proxy` — then nothing but the proxy can reach the app at all, and
`FORWARDED_ALLOW_IPS` (already set to the `edge` subnet in the compose file)
covers it. If your Traefik lives in another compose project, declare its
shared network as external on this side and list it alongside `internal`
instead of `edge`, updating `FORWARDED_ALLOW_IPS` to that network's subnet:

```yaml
  calendar-proxy:
    # no ports: - Traefik reaches it over the shared network
    labels:
      - traefik.enable=true
      - traefik.http.routers.calendar.rule=Host(`cal.example.org`)
      - traefik.http.services.calendar.loadbalancer.server.port=8000
```

Traefik discards client-supplied `X-Forwarded-*` headers by default; keep it
that way by not setting `forwardedHeaders.trustedIPs` on the entrypoint unless
you front Traefik with a CDN whose ranges you trust. One honest caveat:
trusting a network's subnet trusts *every* container on that network, not just
Traefik. On a busy shared `proxy` network that is usually an acceptable risk
(a forged header only weakens rate limiting), but a dedicated network for the
pair removes it entirely.

**Host reverse proxy on loopback.** Keep the compose file's `127.0.0.1:8000`
binding and point the proxy at it. Those connections reach the container from
the `edge` network's gateway, which the pinned subnet already covers.

Caddy overwrites `X-Forwarded-For` by default (since 2.5), so the minimal
config is already correct:

```
cal.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

nginx must be told to overwrite it — use `$remote_addr`, **not**
`$proxy_add_x_forwarded_for`, which appends to whatever the client sent — and
make sure WebDAV verbs and request bodies pass through untouched:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $remote_addr;
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
| Username | your username from the identity provider |
| Password | the generated app password (shown once) |

`/.well-known/caldav` redirects to the DAV root, so clients that auto-discover only need
`https://cal.example.org`.

Secrets are stored as argon2 hashes and are never recoverable — revoke and regenerate if
one is lost. Revocation takes effect immediately.

The web viewer is read-only; create and edit events from a CalDAV client.

## Development

```sh
.venv/bin/python -m pytest
```

Most tests run standalone. The login-flow tests spin up the mock provider automatically.
The Radicale integration tests are skipped unless you point them at a running instance:

```sh
.venv/bin/pip install "radicale>=3.3,<4"
mkdir -p /tmp/radicale-test
sed 's|/data/collections|/tmp/radicale-test|; s|0.0.0.0:5232|127.0.0.1:5232|' \
    dev/radicale.conf > /tmp/radicale-test.conf
.venv/bin/radicale --config /tmp/radicale-test.conf &

RADICALE_TEST_URL=http://127.0.0.1:5232 .venv/bin/python -m pytest
```

### Container image

The proxy image is Alpine-based and about 110 MB. Every dependency publishes musllinux
wheels for amd64 and arm64, so it needs no compiler or Rust toolchain — the Dockerfile
passes `--only-binary=:all:` so the build fails loudly if that ever changes rather than
silently attempting a source build. It runs as an unprivileged user (uid 10001) and keeps
its SQLite database in `/data`.

### Continuous integration

| Workflow | Trigger | Does |
| --- | --- | --- |
| `.github/workflows/ci.yml` | push to `main`, pull requests | Lint, run the tests (including the Radicale integration suite), and build the image |
| `.github/workflows/release.yml` | tags matching `v*` | Build amd64 + arm64 and publish to `ghcr.io/haydenmc/community-calendar-oauth-proxy` |

Releases are tagged `v<major>.<minor>.<patch>`; the registry gets that version plus
`<major>.<minor>`, `<major>` and `latest`.

### Layout

| Path | Contents |
| --- | --- |
| `app/config.py` | Environment-driven settings |
| `app/auth.py` | OIDC login, session and CSRF helpers |
| `app/passwords.py` | App password store, Basic-auth parsing, rate limiting |
| `app/dav_proxy.py` | The `/dav/*` CalDAV reverse proxy |
| `app/caldav.py` | Server-side CalDAV client (bootstrap + viewer fetches) |
| `app/viewer.py` | Recurrence expansion and month-grid construction |
| `app/web.py` | Web routes and templates glue |
| `dev/` | Mock identity provider, local Radicale config and run script (never deployed) |

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
