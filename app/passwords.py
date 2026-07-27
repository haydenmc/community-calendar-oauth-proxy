"""App passwords: the HTTP Basic credentials CalDAV clients use.

Secrets are generated here, shown to the user exactly once, and stored only as
argon2 hashes. Verification results are cached briefly because CalDAV clients
poll often and argon2 is deliberately slow.
"""

from __future__ import annotations

import base64
import binascii
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from .db import Database

_hasher = PasswordHasher()

SECRET_BYTES = 24


class PasswordLimitReached(Exception):
    """Raised when a user already holds the maximum number of app passwords."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"at most {limit} app passwords per user")
        self.limit = limit


@dataclass(frozen=True)
class AppPassword:
    id: int
    username: str
    label: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_password(row) -> AppPassword:
    return AppPassword(
        id=row["id"],
        username=row["username"],
        label=row["label"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


def generate_secret() -> str:
    return secrets.token_urlsafe(SECRET_BYTES)


class PasswordStore:
    def __init__(
        self,
        db: Database,
        cache_ttl: int = 300,
        last_used_throttle: int = 60,
        max_passwords: int = 20,
    ):
        self._db = db
        self._cache_ttl = cache_ttl
        self._last_used_throttle = last_used_throttle
        self._max_passwords = max_passwords
        # (username, secret) -> (expires_at, password_id)
        self._cache: dict[tuple[str, str], tuple[float, int]] = {}
        # password_id -> monotonic timestamp of the last last_used_at write
        self._last_used_written: dict[int, float] = {}

    # -- CRUD -----------------------------------------------------------------

    @property
    def max_passwords(self) -> int:
        return self._max_passwords

    def create(self, username: str, label: str) -> tuple[AppPassword, str]:
        """Create a password, returning the record and the one-time plaintext.

        Raises ``PasswordLimitReached`` if the user is already at the cap. The
        count and the insert share one transaction so two concurrent requests
        cannot both slip past it.
        """
        secret = generate_secret()
        # Hash before opening the transaction: cursor() is serialised across the
        # whole process, and argon2 is far too slow to hold that lock through.
        secret_hash = _hasher.hash(secret)
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS active FROM app_passwords"
                " WHERE username = ? AND revoked_at IS NULL",
                (username,),
            )
            if cur.fetchone()["active"] >= self._max_passwords:
                raise PasswordLimitReached(self._max_passwords)
            cur.execute(
                "INSERT INTO app_passwords (username, label, secret_hash, created_at)"
                " VALUES (?, ?, ?, ?)",
                (username, label.strip()[:64], secret_hash, _now()),
            )
            new_id = cur.lastrowid
        return self.get(username, new_id), secret  # type: ignore[return-value]

    def get(self, username: str, password_id: int) -> AppPassword | None:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_passwords WHERE id = ? AND username = ?",
                (password_id, username),
            )
            row = cur.fetchone()
        return _row_to_password(row) if row else None

    def list_for_user(self, username: str) -> list[AppPassword]:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_passwords WHERE username = ? AND revoked_at IS NULL"
                " ORDER BY created_at DESC, id DESC",
                (username,),
            )
            rows = cur.fetchall()
        return [_row_to_password(r) for r in rows]

    def revoke(self, username: str, password_id: int) -> bool:
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE app_passwords SET revoked_at = ?"
                " WHERE id = ? AND username = ? AND revoked_at IS NULL",
                (_now(), password_id, username),
            )
            changed = cur.rowcount > 0
        if changed:
            self.invalidate_cache_for(username)
        return changed

    def invalidate_cache_for(self, username: str) -> None:
        for key in [k for k in self._cache if k[0] == username]:
            self._cache.pop(key, None)

    # -- verification ---------------------------------------------------------

    def verify(self, username: str, secret: str) -> bool:
        """Check a Basic-auth credential pair. Safe to call on every request."""
        if not username or not secret:
            return False

        key = (username, secret)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and cached[0] > now:
            self._touch(cached[1])
            return True
        if cached:
            self._cache.pop(key, None)

        with self._db.cursor() as cur:
            cur.execute(
                "SELECT id, secret_hash FROM app_passwords"
                " WHERE username = ? AND revoked_at IS NULL",
                (username,),
            )
            rows = cur.fetchall()

        for row in rows:
            try:
                _hasher.verify(row["secret_hash"], secret)
            except (VerifyMismatchError, VerificationError):
                continue
            self._cache[key] = (now + self._cache_ttl, row["id"])
            self._touch(row["id"], force=True)
            return True
        return False

    def _touch(self, password_id: int, force: bool = False) -> None:
        """Record usage, throttled so chatty clients don't hammer SQLite."""
        now = time.monotonic()
        last = self._last_used_written.get(password_id)
        if not force and last is not None and now - last < self._last_used_throttle:
            return
        self._last_used_written[password_id] = now
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE app_passwords SET last_used_at = ? WHERE id = ?",
                (_now(), password_id),
            )


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    """Parse an ``Authorization: Basic ...`` header into (username, secret)."""
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, sep, secret = decoded.partition(":")
    if not sep:
        return None
    return username, secret


class RateLimiter:
    """Sliding-window limiter for failed authentication attempts."""

    def __init__(self, limit: int, window: int) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.limit

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        hits = self._prune(key, now)
        hits.append(now)
        self._hits[key] = hits

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
