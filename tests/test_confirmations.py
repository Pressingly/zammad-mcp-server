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


# --- redeem: the confirming call gets the kept payload back ---


async def issue_kept(confirmations: Confirmations) -> str:
    return await confirmations.issue(identity="id-1", action="email_reply", payload=PAYLOAD, keep_payload=True)


def rewrite_record(backend: MemoryConfirmationBackend, **changes) -> None:
    ((key, (raw, expires_at)),) = backend._values.items()
    backend._values = {key: (json.dumps({**json.loads(raw), **changes}), expires_at)}


async def test_redeem_returns_the_kept_payload_once(confirmations):
    token = await issue_kept(confirmations)
    assert await confirmations.redeem(token, identity="id-1", action="email_reply") == PAYLOAD
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.redeem(token, identity="id-1", action="email_reply")


async def test_payload_is_only_kept_on_request(confirmations, backend):
    await issue(confirmations)
    ((raw, _),) = backend._values.values()
    assert json.loads(raw)["payload"] is None


async def test_redeem_without_a_kept_payload_is_refused(confirmations):
    token = await confirmations.issue(identity="id-1", action="email_reply", payload=PAYLOAD)
    with pytest.raises(ConfirmationError, match="integrity"):
        await confirmations.redeem(token, identity="id-1", action="email_reply")


@pytest.mark.parametrize(("identity", "action"), [("id-2", "email_reply"), ("id-1", "apply_macro")])
async def test_redeem_mismatch_is_refused_and_burns_the_token(confirmations, identity, action):
    token = await issue_kept(confirmations)
    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.redeem(token, identity=identity, action=action)
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.redeem(token, identity="id-1", action="email_reply")


async def test_redeem_refuses_a_tampered_payload(confirmations, backend):
    token = await issue_kept(confirmations)
    rewrite_record(backend, payload={**PAYLOAD, "to": ["attacker@example.com"]})
    with pytest.raises(ConfirmationError, match="integrity"):
        await confirmations.redeem(token, identity="id-1", action="email_reply")


async def test_redeem_refuses_an_expired_token(confirmations, clock):
    token = await issue_kept(confirmations)
    clock.now += 601
    with pytest.raises(ConfirmationError, match="unknown, already used or expired"):
        await confirmations.redeem(token, identity="id-1", action="email_reply")


async def test_non_ascii_identities_compare_safely(confirmations):
    token = await confirmations.issue(identity="ü-1", action="email_reply", payload=PAYLOAD, keep_payload=True)
    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.redeem(token, identity="ü-2", action="email_reply")
