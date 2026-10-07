from __future__ import annotations

import asyncio

import pytest
from fakeredis import FakeAsyncRedis

from zammad_mcp.confirmations import ConfirmationError, Confirmations
from zammad_mcp.platform.storage import (
    CONFIRMATION_SALT,
    KeyValueConfirmationBackend,
    MemoryKeyValue,
    RedisKeyValue,
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


@pytest.fixture
def confirmations(store) -> Confirmations:
    return Confirmations(KeyValueConfirmationBackend(store, FERNET), ttl_seconds=600)


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


async def test_valkey_confirmations_are_stored_hashed_with_a_ttl(redis):
    confirmations = Confirmations(KeyValueConfirmationBackend(RedisKeyValue(redis), FERNET), ttl_seconds=600)
    token = await confirmations.issue(identity="u", action="merge", payload={})

    (key,) = await redis.keys("*")
    assert token not in key
    assert key.startswith("zammad-mcp:confirm:v1:")
    assert 0 < await redis.ttl(key) <= 600


async def test_oauth_storage_round_trips_encrypted(redis):
    storage = build_oauth_storage(redis, "client-secret")

    await storage.put(key="client", value={"secret": "s3cr3t-plaintext"}, collection="clients")

    assert await storage.get(key="client", collection="clients") == {"secret": "s3cr3t-plaintext"}
    raw = [await redis.get(key) for key in await redis.keys("*")]
    assert raw
    assert all("s3cr3t-plaintext" not in str(item) for item in raw)


async def test_confirmation_records_are_encrypted_and_survive_large_payloads(redis):
    backend = KeyValueConfirmationBackend(RedisKeyValue(redis), FERNET)
    record = "attachment:" + "A" * (14 * 1024 * 1024)

    await backend.put("k", record, 600)

    sealed = await redis.get("k")
    assert "attachment:" not in sealed
    assert await backend.take("k") == record
    assert await backend.take("k") is None


async def test_a_record_sealed_with_another_key_reads_as_absent(redis):
    await KeyValueConfirmationBackend(RedisKeyValue(redis), fernet_for("old", CONFIRMATION_SALT)).put("k", "v", 60)

    assert await KeyValueConfirmationBackend(RedisKeyValue(redis), FERNET).take("k") is None
