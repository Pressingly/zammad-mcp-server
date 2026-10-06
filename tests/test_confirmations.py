from __future__ import annotations

import asyncio
import json

import pytest

from zammad_mcp.confirmations import (
    KEY_PREFIX,
    ConfirmationError,
    Confirmations,
    MemoryConfirmationBackend,
    payload_digest,
)

PAYLOAD = {"ticket_id": 4, "to": ["a@example.com"], "body": "hi"}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def backend(clock) -> MemoryConfirmationBackend:
    return MemoryConfirmationBackend(clock=clock)


@pytest.fixture
def confirmations(backend, clock) -> Confirmations:
    return Confirmations(backend, ttl_seconds=600, clock=clock)


async def issue(confirmations: Confirmations) -> str:
    return await confirmations.issue(identity="id-1", action="send_email_reply", payload=PAYLOAD)


async def test_matching_consume_succeeds_once(confirmations):
    token = await issue(confirmations)
    await confirmations.consume(token, identity="id-1", action="send_email_reply", payload=dict(PAYLOAD))
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.consume(token, identity="id-1", action="send_email_reply", payload=PAYLOAD)


async def test_expired_token_is_refused(confirmations, clock):
    token = await issue(confirmations)
    clock.now += 601
    with pytest.raises(ConfirmationError):
        await confirmations.consume(token, identity="id-1", action="send_email_reply", payload=PAYLOAD)


async def test_record_expiry_is_checked_even_if_the_backend_still_has_it(backend, clock):
    issuing = Confirmations(backend, ttl_seconds=600, clock=clock)
    token = await issue(issuing)
    late = Confirmations(backend, ttl_seconds=600, clock=lambda: clock.now + 700)
    with pytest.raises(ConfirmationError, match="expired"):
        await late.consume(token, identity="id-1", action="send_email_reply", payload=PAYLOAD)


@pytest.mark.parametrize(
    ("identity", "action", "payload"),
    [
        ("id-2", "send_email_reply", PAYLOAD),
        ("id-1", "merge_tickets", PAYLOAD),
        ("id-1", "send_email_reply", {**PAYLOAD, "to": ["attacker@example.com"]}),
    ],
)
async def test_mismatch_is_refused_and_burns_the_token(confirmations, identity, action, payload):
    token = await issue(confirmations)
    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.consume(token, identity=identity, action=action, payload=payload)
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.consume(token, identity="id-1", action="send_email_reply", payload=PAYLOAD)


@pytest.mark.parametrize("token", ["", "made-up-token"])
async def test_unknown_token_is_refused(confirmations, token):
    with pytest.raises(ConfirmationError, match="unknown"):
        await confirmations.consume(token, identity="id-1", action="send_email_reply", payload=PAYLOAD)


async def test_tampered_record_is_refused(confirmations, backend):
    token = await issue(confirmations)
    ((key, (raw, expires_at)),) = backend._values.items()
    record = {**json.loads(raw), "payload_sha256": payload_digest({"something": "else"})}
    backend._values = {key: (json.dumps(record), expires_at)}
    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.consume(token, identity="id-1", action="send_email_reply", payload=PAYLOAD)


async def test_records_are_keyed_by_token_hash(confirmations, backend):
    token = await issue(confirmations)
    (key,) = backend._values
    assert key.startswith(KEY_PREFIX)
    assert token not in key
    assert token not in backend._values[key][0]


async def test_concurrent_consume_succeeds_exactly_once(confirmations):
    token = await issue(confirmations)

    async def attempt() -> bool:
        try:
            await confirmations.consume(token, identity="id-1", action="send_email_reply", payload=PAYLOAD)
        except ConfirmationError:
            return False
        return True

    results = await asyncio.gather(*(attempt() for _ in range(20)))
    assert results.count(True) == 1


async def test_put_prunes_expired_records(backend, clock):
    await backend.put("a", "1", 10)
    clock.now += 11
    await backend.put("b", "2", 10)
    assert set(backend._values) == {"b"}


def test_payload_digest_is_order_independent():
    assert payload_digest({"a": 1, "b": [1, 2]}) == payload_digest({"b": [1, 2], "a": 1})
    assert payload_digest({"a": 1}) != payload_digest({"a": 2})


def test_ttl_is_exposed(confirmations):
    assert confirmations.ttl_seconds == 600
