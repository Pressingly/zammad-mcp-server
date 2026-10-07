from __future__ import annotations

import asyncio

import pytest
from fakeredis import FakeAsyncRedis

from zammad_mcp.confirmations import ConfirmationError, Confirmations
from zammad_mcp.platform.storage import (
    CONFIRMATION_SALT,
    ConfirmationStoreFullError,
    MemoryKeyValue,
    RedisKeyValue,
    ValkeyConfirmationBackend,
    build_oauth_storage,
    build_redis,
    fernet_for,
    parse_storage_url,
)

FERNET = fernet_for("client-secret", CONFIRMATION_SALT)


def test_valkey_alias_keeps_db_and_query():
    config = parse_storage_url("valkeys://:pw@valkey.internal:6380/15?ssl_cert_reqs=none")

    assert config.url == "rediss://:pw@valkey.internal:6380/15?ssl_cert_reqs=none"
    assert (config.host, config.port, config.db) == ("valkey.internal", 6380, 15)


def test_plain_redis_url_defaults():
    config = parse_storage_url("redis://valkey")

    assert config.url == "redis://valkey"
    assert (config.port, config.db) == (6379, 0)
    assert parse_storage_url("valkey://valkey:6379/").db == 0


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("http://valkey:6379/15", "scheme"),
        ("redis:///15", "hostname"),
        ("redis://valkey:6379/fifteen", "numeric DB index"),
    ],
)
def test_malformed_storage_urls_are_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_storage_url(raw)


def test_build_redis_honours_tls():
    client = build_redis(parse_storage_url("valkeys://valkey:6380/15"))

    assert client.connection_pool.connection_kwargs["db"] == 15
    assert client.connection_pool.connection_class.__name__ == "SSLConnection"


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture(params=["valkey", "memory"])
def store(request, redis):
    return RedisKeyValue(redis) if request.param == "valkey" else MemoryKeyValue()


async def test_key_value_contract(store):
    await store.set("k", "v", 60)
    assert await store.get("k") == "v"

    assert await store.set_if_absent("nx", "1", 60) is True
    assert await store.set_if_absent("nx", "2", 60) is False
    assert await store.get("nx") == "1"

    assert await store.get_many(["k", "missing", "nx"]) == ["v", None, "1"]
    assert await store.take("k") == "v"
    assert await store.take("k") is None

    await store.delete("nx")
    assert await store.get("nx") is None


async def test_valkey_store_sets_an_expiry(redis):
    await RedisKeyValue(redis).set("k", "v", 90)

    assert 0 < await redis.ttl("k") <= 90


async def test_memory_store_expires_entries():
    now = [1000.0]
    store = MemoryKeyValue(lambda: now[0])
    await store.set("k", "v", 10)
    await store.set_if_absent("nx", "1", 10)

    now[0] += 10

    assert await store.get("k") is None
    assert await store.take("k") is None
    assert await store.set_if_absent("nx", "2", 10) is True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def valkey_backend(redis, clock, **options) -> ValkeyConfirmationBackend:
    return ValkeyConfirmationBackend(redis, FERNET, namespace="test", clock=clock, **options)


@pytest.fixture
def confirmations(redis, clock) -> Confirmations:
    return Confirmations(valkey_backend(redis, clock), ttl_seconds=600, clock=clock)


async def test_confirmation_is_single_use(confirmations):
    token = await confirmations.issue(identity="u", action="send_email_reply", payload={"to": "a"})

    await confirmations.consume(token, identity="u", action="send_email_reply", payload={"to": "a"})
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.consume(token, identity="u", action="send_email_reply", payload={"to": "a"})


async def test_confirmation_mismatch_still_spends_the_token(confirmations):
    token = await confirmations.issue(identity="u", action="merge", payload={"a": 1})

    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.consume(token, identity="someone-else", action="merge", payload={"a": 1})
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.consume(token, identity="u", action="merge", payload={"a": 1})


async def test_concurrent_consumes_succeed_once(confirmations):
    token = await confirmations.issue(identity="u", action="merge", payload={})

    async def attempt() -> bool:
        try:
            await confirmations.consume(token, identity="u", action="merge", payload={})
        except ConfirmationError:
            return False
        return True

    results = await asyncio.gather(*(attempt() for _ in range(20)))

    assert results.count(True) == 1


async def test_valkey_confirmations_are_stored_hashed_with_a_ttl(redis, confirmations):
    token = await confirmations.issue(identity="u", action="merge", payload={})

    (key,) = [key for key in await redis.keys("zammad-mcp:confirm:v1:*")]
    assert token not in key
    assert 0 < await redis.ttl(key) <= 600


async def test_records_are_encrypted_and_a_maximum_size_record_fits(redis, clock):
    backend = valkey_backend(redis, clock)
    record = "attachment:" + "A" * (20 * 1024 * 1024)

    await backend.put("k", record, 600)

    sealed = await redis.get("k")
    assert "attachment:" not in sealed
    assert len(sealed) > len(record)
    assert await backend.held_bytes() == len(record)
    assert await backend.take("k") == record
    assert await backend.take("k") is None
    assert await backend.held_bytes() == 0


async def test_a_full_store_refuses_cleanly_and_keeps_room_for_small_records(redis, clock):
    backend = valkey_backend(redis, clock, max_bytes=1000 * 1024, small_record_reserve=100 * 1024)
    large = "L" * (400 * 1024)
    await backend.put("a", large, 600)
    await backend.put("b", large, 600)

    with pytest.raises(ConfirmationStoreFullError, match="too many large actions"):
        await backend.put("c", "L" * (150 * 1024), 600)
    assert await redis.get("c") is None

    await backend.put("small", "s" * (60 * 1024), 600)
    await backend.put("small-2", "s" * (60 * 1024), 600)
    assert await backend.take("a") == large
    await backend.put("c", "L" * (150 * 1024), 600)
    assert await backend.held_bytes() == (400 + 60 + 60 + 150) * 1024


async def test_expired_records_free_their_bytes(redis, clock):
    backend = valkey_backend(redis, clock, max_bytes=1000 * 1024, small_record_reserve=0)
    large = "L" * (600 * 1024)
    await backend.put("a", large, 60)

    clock.now += 61

    await backend.put("b", large, 60)
    assert await redis.zcard("zammad-mcp:confirm-bytes:v1:test:expiry") == 1
    assert await redis.hkeys("zammad-mcp:confirm-bytes:v1:test:sizes") == ["b"]
    assert await backend.held_bytes() == len(large)


async def test_rewriting_a_key_counts_it_once(redis, clock):
    backend = valkey_backend(redis, clock, max_bytes=1000 * 1024, small_record_reserve=0)
    large = "L" * (600 * 1024)

    await backend.put("a", large, 60)
    await backend.put("a", large, 60)

    assert await backend.held_bytes() == len(large)


async def test_a_record_sealed_with_another_key_reads_as_absent(redis, clock):
    other = ValkeyConfirmationBackend(redis, fernet_for("old", CONFIRMATION_SALT), namespace="test", clock=clock)
    await other.put("k", "v", 60)

    backend = valkey_backend(redis, clock)
    assert await backend.take("k") is None
    assert await backend.held_bytes() == 0
    assert await redis.hlen("zammad-mcp:confirm-bytes:v1:test:sizes") == 0
    assert await redis.zcard("zammad-mcp:confirm-bytes:v1:test:expiry") == 0


async def test_taking_a_missing_record_changes_nothing(redis, clock):
    backend = valkey_backend(redis, clock)
    await backend.put("a", "v", 60)

    assert await backend.take("missing") is None
    assert await backend.held_bytes() == 1


async def test_an_expired_record_reads_as_absent_and_is_pruned(redis, clock):
    backend = valkey_backend(redis, clock)
    await backend.put("a", "v" * 10, 60)
    clock.now += 61
    await redis.delete("a")

    assert await backend.take("a") is None
    assert await backend.held_bytes() == 0


async def test_oauth_storage_round_trips_encrypted(redis):
    storage = build_oauth_storage(redis, "client-secret")

    await storage.put(key="client", value={"secret": "s3cr3t-plaintext"}, collection="clients")

    assert await storage.get(key="client", collection="clients") == {"secret": "s3cr3t-plaintext"}
    raw = [await redis.get(key) for key in await redis.keys("*")]
    assert raw
    assert all("s3cr3t-plaintext" not in str(item) for item in raw)


async def test_concurrent_puts_never_overshoot_the_cap(redis, clock):
    backend = valkey_backend(redis, clock, max_bytes=1000 * 1024, small_record_reserve=0)

    async def put(index: int) -> bool:
        try:
            await backend.put(f"k{index}", "L" * (150 * 1024), 600)
        except ConfirmationStoreFullError:
            return False
        return True

    results = await asyncio.gather(*(put(index) for index in range(12)))

    assert results.count(True) == 6
    assert await backend.held_bytes() == 6 * 150 * 1024
