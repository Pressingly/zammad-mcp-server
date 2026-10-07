from __future__ import annotations

import asyncio
import json

import pytest

from zammad_mcp.confirmations import (
    KEY_PREFIX,
    MAX_BYTES_PER_IDENTITY,
    MAX_MEMORY_BYTES,
    MAX_OUTSTANDING_PER_IDENTITY,
    SMALL_RECORD_BYTES,
    ConfirmationError,
    ConfirmationLimitError,
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


def rewrite_record(backend: MemoryConfirmationBackend, **changes) -> None:
    ((key, entry),) = backend._values.items()
    backend._values = {key: entry._replace(value=json.dumps({**json.loads(entry.value), **changes}))}


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


async def test_corrupted_digest_is_refused(confirmations, backend):
    token = await issue(confirmations)
    rewrite_record(backend, payload_sha256=payload_digest({"something": "else"}))
    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.consume(token, identity="id-1", action="send_email_reply", payload=PAYLOAD)


async def test_records_are_keyed_by_token_hash(confirmations, backend):
    token = await issue(confirmations)
    (key,) = backend._values
    assert key.startswith(KEY_PREFIX)
    assert token not in key
    assert token not in backend._values[key].value


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


async def test_redeem_returns_the_kept_payload_once(confirmations):
    token = await issue_kept(confirmations)
    assert await confirmations.redeem(token, identity="id-1", action="email_reply") == PAYLOAD
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.redeem(token, identity="id-1", action="email_reply")


async def test_payload_is_only_kept_on_request(confirmations, backend):
    await issue(confirmations)
    (entry,) = backend._values.values()
    assert json.loads(entry.value)["payload"] is None


async def test_redeem_without_a_kept_payload_is_refused(confirmations):
    token = await confirmations.issue(identity="id-1", action="email_reply", payload=PAYLOAD)
    with pytest.raises(ConfirmationError, match="corrupted"):
        await confirmations.redeem(token, identity="id-1", action="email_reply")


@pytest.mark.parametrize(("identity", "action"), [("id-2", "email_reply"), ("id-1", "apply_macro")])
async def test_redeem_mismatch_is_refused_and_burns_the_token(confirmations, identity, action):
    token = await issue_kept(confirmations)
    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.redeem(token, identity=identity, action=action)
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.redeem(token, identity="id-1", action="email_reply")


async def test_redeem_detects_a_corrupted_payload(confirmations, backend):
    token = await issue_kept(confirmations)
    rewrite_record(backend, payload={**PAYLOAD, "to": ["attacker@example.com"]})
    with pytest.raises(ConfirmationError, match="corrupted"):
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


# --- bounds ---


async def test_one_identity_is_capped_at_its_outstanding_confirmations(confirmations):
    tokens = [await issue(confirmations) for _ in range(MAX_OUTSTANDING_PER_IDENTITY)]
    with pytest.raises(ConfirmationLimitError, match=f"already have {MAX_OUTSTANDING_PER_IDENTITY} actions waiting"):
        await issue(confirmations)
    await confirmations.issue(identity="id-2", action="send_email_reply", payload=PAYLOAD)
    await confirmations.consume(tokens[0], identity="id-1", action="send_email_reply", payload=PAYLOAD)
    await issue(confirmations)


async def test_expired_confirmations_free_their_slots(confirmations, clock):
    for _ in range(MAX_OUTSTANDING_PER_IDENTITY):
        await issue(confirmations)
    clock.now += 601
    await issue(confirmations)
    assert set(confirmations._outstanding) == {"id-1"}


async def test_a_failed_consume_still_frees_the_slot(backend, clock):
    confirmations = Confirmations(backend, ttl_seconds=600, clock=clock, max_outstanding_per_identity=1)
    token = await issue(confirmations)
    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.consume(token, identity="id-1", action="other", payload=PAYLOAD)
    await issue(confirmations)


async def test_idle_identities_are_dropped(confirmations, clock):
    await confirmations.issue(identity="id-2", action="a", payload=PAYLOAD)
    clock.now += 601
    await issue(confirmations)
    assert set(confirmations._outstanding) == {"id-1"}


async def test_memory_backend_refuses_past_its_byte_cap(clock):
    backend = MemoryConfirmationBackend(clock=clock, max_bytes=10)
    await backend.put("a", "12345", 60)
    await backend.put("a", "123456789", 60)
    with pytest.raises(ConfirmationLimitError, match="too many large actions"):
        await backend.put("b", "12", 60)
    clock.now += 61
    await backend.put("b", "1234567890", 60)


async def test_a_refused_put_releases_the_reservation(clock):
    confirmations = Confirmations(
        MemoryConfirmationBackend(clock=clock, max_bytes=10), clock=clock, max_outstanding_per_identity=1
    )
    with pytest.raises(ConfirmationLimitError, match="too many large actions"):
        await issue(confirmations)
    assert confirmations._outstanding == {}


# --- byte budgets ---

MACRO = {"macro_id": 1, "ticket_ids": [5, 6]}
BIG_BODY = "x" * (4 * 1024 * 1024)


async def issue_big(confirmations: Confirmations, identity: str = "id-1") -> str:
    return await confirmations.issue(
        identity=identity, action="email_reply", payload={"body": BIG_BODY}, keep_payload=True
    )


def test_defaults_leave_room_for_several_users():
    assert MAX_BYTES_PER_IDENTITY == MAX_MEMORY_BYTES // 4
    assert SMALL_RECORD_BYTES == 64 * 1024


async def test_one_identity_cannot_exhaust_the_pool(clock):
    backend = MemoryConfirmationBackend(clock=clock)
    confirmations = Confirmations(backend, ttl_seconds=600, clock=clock)
    issued = 0
    with pytest.raises(ConfirmationLimitError, match="share of the confirmation store"):
        for _ in range(14):
            await issue_big(confirmations)
            issued += 1
    assert issued == MAX_BYTES_PER_IDENTITY // (len(BIG_BODY) + 200)
    assert confirmations.held_bytes("id-1") <= MAX_BYTES_PER_IDENTITY
    stored = sum(entry.size for entry in backend._values.values())
    assert stored == confirmations.held_bytes("id-1") <= MAX_MEMORY_BYTES // 4
    await confirmations.issue(identity="id-2", action="apply_macro", payload=MACRO)
    await issue_big(confirmations, identity="id-2")


async def test_second_users_macro_succeeds_while_the_first_is_at_its_budget(backend, clock):
    confirmations = Confirmations(backend, ttl_seconds=600, clock=clock, max_bytes_per_identity=1000)
    await confirmations.issue(identity="id-1", action="email_reply", payload={"body": "x" * 800}, keep_payload=True)
    with pytest.raises(ConfirmationLimitError, match="1000-byte share"):
        await confirmations.issue(identity="id-1", action="apply_macro", payload={"body": "y" * 300}, keep_payload=True)
    token = await confirmations.issue(identity="id-2", action="apply_macro", payload=MACRO)
    await confirmations.consume(token, identity="id-2", action="apply_macro", payload=MACRO)


async def test_bytes_are_released_on_take(backend, clock):
    confirmations = Confirmations(backend, ttl_seconds=600, clock=clock, max_bytes_per_identity=1000)
    token = await confirmations.issue(identity="id-1", action="a", payload={"body": "x" * 800}, keep_payload=True)
    assert confirmations.held_bytes("id-1") > 800
    await confirmations.redeem(token, identity="id-1", action="a")
    assert confirmations.held_bytes("id-1") == 0
    await confirmations.issue(identity="id-1", action="a", payload={"body": "x" * 800}, keep_payload=True)


async def test_bytes_are_released_on_expiry(backend, clock):
    confirmations = Confirmations(backend, ttl_seconds=600, clock=clock, max_bytes_per_identity=1000)
    await confirmations.issue(identity="id-1", action="a", payload={"body": "x" * 800}, keep_payload=True)
    clock.now += 601
    assert confirmations.held_bytes("id-1") == 0
    await confirmations.issue(identity="id-1", action="a", payload={"body": "x" * 800}, keep_payload=True)


async def test_bytes_are_released_when_the_backend_refuses(clock):
    backend = MemoryConfirmationBackend(clock=clock, max_bytes=500, small_record_reserve=0)
    confirmations = Confirmations(backend, ttl_seconds=600, clock=clock)
    with pytest.raises(ConfirmationLimitError, match="too many large actions"):
        await confirmations.issue(identity="id-1", action="a", payload={"body": "x" * 800}, keep_payload=True)
    assert confirmations.held_bytes("id-1") == 0
    assert confirmations._outstanding == {}


async def test_sizes_are_utf8_bytes_of_unescaped_json(backend, clock):
    confirmations = Confirmations(backend, ttl_seconds=600, clock=clock)
    await confirmations.issue(identity="id-1", action="a", payload={"body": "\U0001f600" * 1000}, keep_payload=True)
    (entry,) = backend._values.values()
    assert "\U0001f600" in entry.value
    assert "\\ud83d" not in entry.value
    assert entry.size == len(entry.value.encode()) == confirmations.held_bytes("id-1")
    assert 4000 < entry.size < 4500


async def test_lone_surrogates_are_counted_not_rejected(backend, clock):
    confirmations = Confirmations(backend, ttl_seconds=600, clock=clock)
    token = await confirmations.issue(identity="id-1", action="a", payload={"body": "\ud800"}, keep_payload=True)
    assert await confirmations.redeem(token, identity="id-1", action="a") == {"body": "\ud800"}


async def test_small_records_keep_a_reserve_large_ones_cannot_use(clock):
    backend = MemoryConfirmationBackend(clock=clock, max_bytes=200_000, small_record_reserve=100_000)
    big = "x" * SMALL_RECORD_BYTES
    await backend.put("a", big, 60)
    with pytest.raises(ConfirmationLimitError, match="too many large actions"):
        await backend.put("b", big, 60)
    for index in range(10):
        await backend.put(f"small-{index}", "y" * 10_000, 60)
