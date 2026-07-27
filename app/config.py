"""Environment-driven configuration for calendar-proxy."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Public origin this app is served from, used to build CalDAV URLs shown to users.
    public_base_url: str = "http://localhost:8000"

    # Name shown in the header, page titles and on the sign-in page. This is the
    # branding for the site itself; `shared_display_name` below is the separate
    # name CalDAV clients show for the calendar collection.
    site_title: str = "Community Calendar"
    # Tagline under the heading on the sign-in page. Set empty to omit it.
    site_tagline: str = (
        "A shared calendar for the community, readable in your browser and "
        "subscribable from any CalDAV client."
    )

    # --- OpenID Connect ------------------------------------------------------
    # Any provider publishing a discovery document works. The issuer is the URL
    # that serves /.well-known/openid-configuration, e.g. for kanidm
    # https://idm.example.org/oauth2/openid/calendar
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid profile email"
    # Claim to use as the local username. Most providers, kanidm included, put
    # the account name in `preferred_username` when the profile scope is granted;
    # set this to `email` or `sub` for providers that do not.
    oidc_username_claim: str = "preferred_username"
    # Shown on the sign-in button, e.g. "kanidm", "Authentik", "our SSO".
    oidc_provider_name: str = "SSO"

    # --- Radicale backend ----------------------------------------------------
    radicale_url: str = "http://radicale:5232"
    # Every authenticated user is mapped onto this single Radicale principal, so
    # everyone reads and writes the same collection.
    shared_principal: str = "community"
    shared_collection: str = "shared"
    shared_display_name: str = "Community Calendar"

    # --- sessions / cookies --------------------------------------------------
    session_secret: str = ""
    session_max_age: int = 60 * 60 * 24 * 7
    cookie_secure: bool = True

    # --- storage -------------------------------------------------------------
    database_path: str = "/data/calendar-proxy.db"

    # --- behaviour -----------------------------------------------------------
    dav_prefix: str = "/dav"
    # How long a verified app password stays in the in-memory cache, so argon2 is
    # not re-run on every PROPFIND a client fires off.
    auth_cache_ttl: int = 300
    # Do not touch last_used_at more often than this (seconds) per credential.
    last_used_throttle: int = 60
    # Failed Basic-auth attempts allowed per (IP, username) inside the window.
    auth_rate_limit: int = 10
    auth_rate_window: int = 300
    # Active app passwords one user may hold. Verification tries each of a
    # user's active hashes in turn, so this also caps the argon2 work a failed
    # Basic-auth attempt against that username can cost.
    max_app_passwords: int = 20
    # Largest request body the DAV proxy will accept, in bytes. Bodies are
    # buffered in memory before being forwarded, so this bounds what a single
    # authenticated client can make the process allocate. Calendar events are
    # kilobytes; the default leaves room for large attachments.
    max_body_bytes: int = 20 * 1024 * 1024
    # Largest calendar-query response the viewer will buffer, in bytes. The
    # month window keeps this small in practice; the cap is what stops a huge
    # collection from being pulled into memory whole.
    max_calendar_bytes: int = 20 * 1024 * 1024
    display_timezone: str = "UTC"

    @property
    def shared_path(self) -> str:
        """Backend path of the shared collection, e.g. /community/shared/."""
        return f"/{self.shared_principal}/{self.shared_collection}/"

    @property
    def caldav_url(self) -> str:
        """Public CalDAV URL handed to users for their clients."""
        return f"{self.public_base_url.rstrip('/')}{self.dav_prefix}{self.shared_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
