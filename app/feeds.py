"""ICS feed links: the secret URLs hosted calendars subscribe to.

Google Calendar and Outlook.com cannot sign in with OIDC and do not speak
CalDAV; all they take is a URL they can GET. The token in that URL is therefore
the whole credential, which is why it is long and why revoking is immediate.

Unlike app passwords these are stored in the clear. The URL has to stay
re-displayable - people paste it into a second service months later - so the
plaintext must live in the database either way, and a hash column sitting beside
the value it is meant to protect would buy nothing.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import Settings
from .db import Database

TOKEN_BYTES = 24


class FeedLimitReached(Exception):
    """Raised when a user already holds the maximum number of feed links."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"at most {limit} feed links per user")
        self.limit = limit


@dataclass(frozen=True)
class IcsFeed:
    id: int
    username: str
    label: str
    token: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_feed(row) -> IcsFeed:
    return IcsFeed(
        id=row["id"],
        username=row["username"],
        label=row["label"],
        token=row["token"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def feed_path(token: str) -> str:
    """Path the feed is served from. The .ics suffix is what some importers sniff."""
    return f"/feeds/{token}.ics"


def feed_url(settings: Settings, feed: IcsFeed) -> str:
    """Absolute URL handed to the user, built from the configured public origin.

    Deriving it from the request instead would depend on forwarded headers being
    right, and a wrong host here is a URL that silently never updates.
    """
    return settings.public_base_url.rstrip("/") + feed_path(feed.token)


class FeedStore:
    def __init__(
        self,
        db: Database,
        last_used_throttle: int = 60,
        max_feeds: int = 20,
    ):
        self._db = db
        self._last_used_throttle = last_used_throttle
        self._max_feeds = max_feeds
        # feed_id -> monotonic timestamp of the last last_used_at write
        self._last_used_written: dict[int, float] = {}

    @property
    def max_feeds(self) -> int:
        return self._max_feeds

    def create(self, username: str, label: str) -> IcsFeed:
        """Create a feed link. Raises ``FeedLimitReached`` at the cap.

        The count and the insert share one transaction so two concurrent
        requests cannot both slip past the limit.
        """
        token = generate_token()
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS active FROM ics_feeds WHERE username = ? AND revoked_at IS NULL",
                (username,),
            )
            if cur.fetchone()["active"] >= self._max_feeds:
                raise FeedLimitReached(self._max_feeds)
            cur.execute(
                "INSERT INTO ics_feeds (username, label, token, created_at) VALUES (?, ?, ?, ?)",
                (username, label.strip()[:64], token, _now()),
            )
            new_id = cur.lastrowid
        return self.get(username, new_id)  # type: ignore[return-value]

    def get(self, username: str, feed_id: int) -> IcsFeed | None:
        with self._db.cursor() as cur:
            cur.execute("SELECT * FROM ics_feeds WHERE id = ? AND username = ?", (feed_id, username))
            row = cur.fetchone()
        return _row_to_feed(row) if row else None

    def list_for_user(self, username: str) -> list[IcsFeed]:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM ics_feeds WHERE username = ? AND revoked_at IS NULL"
                " ORDER BY created_at DESC, id DESC",
                (username,),
            )
            rows = cur.fetchall()
        return [_row_to_feed(r) for r in rows]

    def revoke(self, username: str, feed_id: int) -> bool:
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE ics_feeds SET revoked_at = ?"
                " WHERE id = ? AND username = ? AND revoked_at IS NULL",
                (_now(), feed_id, username),
            )
            return cur.rowcount > 0

    def lookup(self, token: str) -> IcsFeed | None:
        """Resolve a token from a feed URL, or None if unknown or revoked.

        Callers must not distinguish those two cases to the client. The token is
        192 bits from a CSPRNG, so the indexed equality match this compiles to
        offers nothing to guess against.
        """
        if not token:
            return None
        with self._db.cursor() as cur:
            cur.execute("SELECT * FROM ics_feeds WHERE token = ? AND revoked_at IS NULL", (token,))
            row = cur.fetchone()
        return _row_to_feed(row) if row else None

    def touch(self, feed_id: int) -> None:
        """Record a fetch, throttled: hosted services poll on their own schedule."""
        now = time.monotonic()
        last = self._last_used_written.get(feed_id)
        if last is not None and now - last < self._last_used_throttle:
            return
        self._last_used_written[feed_id] = now
        with self._db.cursor() as cur:
            cur.execute("UPDATE ics_feeds SET last_used_at = ? WHERE id = ?", (_now(), feed_id))
