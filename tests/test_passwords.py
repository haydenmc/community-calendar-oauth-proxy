from __future__ import annotations

from app.passwords import RateLimiter, parse_basic_auth


def test_created_password_verifies(store):
    record, secret = store.create("alice", "Phone")
    assert record.label == "Phone"
    assert store.verify("alice", secret)


def test_secret_is_not_stored_in_plaintext(store):
    _, secret = store.create("alice", "Phone")
    with store._db.cursor() as cur:
        cur.execute("SELECT secret_hash FROM app_passwords")
        stored = cur.fetchone()["secret_hash"]
    assert secret not in stored
    assert stored.startswith("$argon2")


def test_wrong_secret_and_wrong_user_are_rejected(store):
    _, secret = store.create("alice", "Phone")
    assert not store.verify("alice", secret + "x")
    assert not store.verify("bob", secret)
    assert not store.verify("alice", "")


def test_multiple_passwords_are_independent(store):
    _, first = store.create("alice", "Phone")
    _, second = store.create("alice", "Laptop")
    assert store.verify("alice", first)
    assert store.verify("alice", second)


def test_revoke_invalidates_credential_and_cache(store):
    record, secret = store.create("alice", "Phone")
    assert store.verify("alice", secret)  # populates the verification cache

    assert store.revoke("alice", record.id)
    assert not store.verify("alice", secret)
    assert record.id not in {p.id for p in store.list_for_user("alice")}


def test_revoke_only_affects_own_passwords(store):
    record, secret = store.create("alice", "Phone")
    assert not store.revoke("bob", record.id)
    assert store.verify("alice", secret)


def test_last_used_is_recorded_on_first_use(store):
    record, secret = store.create("alice", "Phone")
    assert store.get("alice", record.id).last_used_at is None
    store.verify("alice", secret)
    assert store.get("alice", record.id).last_used_at is not None


def test_parse_basic_auth():
    assert parse_basic_auth("Basic YWxpY2U6c2VjcmV0") == ("alice", "secret")
    assert parse_basic_auth("basic YWxpY2U6c2VjcmV0") == ("alice", "secret")
    # A colon in the secret must stay in the secret.
    assert parse_basic_auth("Basic YWxpY2U6c2U6Y3JldA==") == ("alice", "se:cret")


def test_parse_basic_auth_rejects_bad_input():
    assert parse_basic_auth(None) is None
    assert parse_basic_auth("") is None
    assert parse_basic_auth("Bearer token") is None
    assert parse_basic_auth("Basic") is None
    assert parse_basic_auth("Basic !!!not-base64!!!") is None
    # No colon at all is not a valid credential pair.
    assert parse_basic_auth("Basic YWxpY2U=") is None


def test_rate_limiter_blocks_after_limit():
    limiter = RateLimiter(limit=3, window=60)
    key = "1.2.3.4|alice"
    for _ in range(2):
        limiter.record_failure(key)
    assert not limiter.is_blocked(key)
    limiter.record_failure(key)
    assert limiter.is_blocked(key)


def test_rate_limiter_reset_clears_history():
    limiter = RateLimiter(limit=1, window=60)
    limiter.record_failure("key")
    assert limiter.is_blocked("key")
    limiter.reset("key")
    assert not limiter.is_blocked("key")
