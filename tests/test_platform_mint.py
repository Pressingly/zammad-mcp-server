from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta

import anyio
import httpx
import pytest

from tests.platform_fakes import (
    ADMIN_PERMISSIONS,
    AGENT_PERMISSIONS,
    COOKIE_NAME,
    CUSTOMER_PERMISSIONS,
    FakeSsoZammad,
    identity,
)
from zammad_mcp.client.errors import AccountInactiveError, GatewayDeniedError, ZammadTransportError, to_tool_error
from zammad_mcp.client.http import USER_AGENT, is_identity_header
from zammad_mcp.platform.ceiling import MINTABLE_PERMISSIONS
from zammad_mcp.platform.identity import Identity
from zammad_mcp.platform.mint import (
    CACHE_KEY_PREFIX,
    CACHE_SALT,
    CACHE_TTL_SECONDS,
    DEVICE_FINGERPRINT,
    FORBIDDEN_RECHECK_INTERVAL_SECONDS,
    RECHECK_INTERVAL_SECONDS,
    CacheEntry,
    CustomerTokenAccessError,
    EmptyCeilingError,
    KeyedLocks,
    MintedToken,
    MintError,
    SyntheticIdentityError,
    TokenCache,
    TokenCacheError,
    TokenEndpoint,
    TokenMinter,
    cache_ttl_seconds,
    expired_mcp_token_ids,
    permission_names,
    session_cookie,
    token_expiry,
)
from zammad_mcp.platform.storage import MemoryKeyValue, fernet_for
from zammad_mcp.tiers import Tier

START = datetime(2026, 10, 7, 9, 30, tzinfo=UTC).timestamp()
AGENT = "ada@example.com"
CUSTOMER = "cara@example.com"
ADMIN = "root@example.com"
API = "http://zammad-nginx:8080/api/v1"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def zammad() -> FakeSsoZammad:
    fake = FakeSsoZammad()
    fake.add(AGENT, AGENT_PERMISSIONS)
    fake.add(CUSTOMER, CUSTOMER_PERMISSIONS)
    fake.add(ADMIN, ADMIN_PERMISSIONS + AGENT_PERMISSIONS)
    return fake


@pytest.fixture
def store(clock) -> MemoryKeyValue:
    return MemoryKeyValue(clock)


NAMESPACE = "test"


def cache_for(store) -> TokenCache:
    return TokenCache(store, fernet_for("client-secret", CACHE_SALT), namespace=NAMESPACE)


def make_minter(transport, store, clock, **options) -> TokenMinter:
    endpoint = TokenEndpoint(API, timeout=httpx.Timeout(5.0), transport=transport)
    return TokenMinter(endpoint, cache_for(store), default_email_domain="askii.ai", clock=clock, **options)


@pytest.fixture
def minter(zammad, store, clock) -> TokenMinter:
    return make_minter(zammad, store, clock)


async def test_handshake_copies_the_session_cookie_and_csrf_by_hand(minter, zammad):
    minted = await minter.token_for(identity(AGENT))

    (get,) = zammad.gets()
    (post,) = zammad.posts()
    assert get.headers["x-auth-request-email"] == AGENT
    assert get.headers["x-auth-request-access-token"] == "upstream-access"
    assert "cookie" not in get.headers
    assert post.headers["cookie"] == f"{COOKIE_NAME}=sid-0"
    assert post.headers["x-csrf-token"] == "csrf-0"
    assert not [name for name in post.headers if is_identity_header(name)]
    assert minted.token == "minted-1"
    assert minted.tier is Tier.AGENT


async def test_every_bootstrap_request_sends_one_device_fingerprint_and_user_agent(minter, zammad):
    zammad.users[AGENT].tokens.append({"id": 1, "name": "zammad-mcp (auto) 2020-01-01", "expires_at": "2020-01-01"})

    await minter.token_for(identity(AGENT))

    assert len(DEVICE_FINGERPRINT) <= 160
    assert {request.method for request in zammad.requests} == {"GET", "POST", "DELETE"}
    assert {request.headers["x-browser-fingerprint"] for request in zammad.requests} == {DEVICE_FINGERPRINT}
    assert {request.headers["user-agent"] for request in zammad.requests} == {USER_AGENT}


async def test_access_token_header_is_left_out_when_there_is_none(minter, zammad):
    await minter.token_for(identity(AGENT, access_token=None))

    assert "x-auth-request-access-token" not in zammad.gets()[0].headers


async def test_a_fresh_client_per_mint_never_carries_another_users_session(minter, zammad):
    await minter.token_for(identity(AGENT))
    await minter.token_for(identity(CUSTOMER))

    first, second = zammad.posts()
    assert first.headers["cookie"] == f"{COOKIE_NAME}=sid-0"
    assert second.headers["cookie"] == f"{COOKIE_NAME}=sid-1"
    assert all("cookie" not in request.headers for request in zammad.gets())


async def test_a_hundred_concurrent_requests_mint_once(minter, zammad):
    results = await asyncio.gather(*(minter.token_for(identity(AGENT)) for _ in range(100)))

    assert len(zammad.posts()) == 1
    assert len(zammad.gets()) == 1
    assert {minted.token for minted in results} == {"minted-1"}
    assert minter.lock_count == 0


async def test_explicit_permission_list_never_carries_admin_or_parent_nodes(minter, zammad, clock):
    minted = await minter.token_for(identity(ADMIN))

    body = zammad.posted_body()
    assert body["permission"] == ["knowledge_base.editor", "knowledge_base.reader", "ticket.agent"]
    assert set(body["permission"]) <= MINTABLE_PERMISSIONS
    assert body["expires_at"] == (date(2026, 10, 7) + timedelta(days=9)).isoformat()
    assert body["name"] == "zammad-mcp (auto) 2026-10-07"
    assert minted.permissions == frozenset(body["permission"])


async def test_customer_gets_a_customer_token(minter, zammad):
    minted = await minter.token_for(identity(CUSTOMER))

    assert zammad.posted_body()["permission"] == ["ticket.customer"]
    assert minted.tier is Tier.CUSTOMER


async def test_admin_without_agent_role_gets_kb_editor_and_tier_none(minter, zammad):
    zammad.add("boss@example.com", ADMIN_PERMISSIONS)

    minted = await minter.token_for(identity("boss@example.com"))

    assert zammad.posted_body()["permission"] == ["knowledge_base.editor"]
    assert minted.tier is Tier.NONE


async def test_empty_ceiling_fails_closed_without_posting(minter, zammad):
    zammad.add("nobody@example.com", ("user_preferences", "user_preferences.access_token", "ticket"))

    with pytest.raises(EmptyCeilingError):
        await minter.token_for(identity("nobody@example.com"))

    assert zammad.posts() == []


async def test_customer_before_the_grant_gets_a_clear_message(minter, zammad):
    zammad.users[CUSTOMER].token_access = False

    with pytest.raises(CustomerTokenAccessError) as raised:
        await minter.token_for(identity(CUSTOMER))

    assert "Customer token access is not enabled" in to_tool_error(raised.value)
    assert "user_preferences.access_token" in to_tool_error(raised.value)
    assert zammad.posts() == []


async def test_corporate_gate_denial_maps_to_the_gateway_error(store, clock):
    zammad = FakeSsoZammad(corporate_gate=True)
    zammad.add(AGENT, AGENT_PERMISSIONS)
    minter = make_minter(zammad, store, clock)

    with pytest.raises(GatewayDeniedError) as raised:
        await minter.token_for(identity(AGENT, access_token=None))

    assert "sign-in gateway" in to_tool_error(raised.value)
    assert (await minter.token_for(identity(AGENT))).token == "minted-1"


async def test_inactive_account_maps_to_its_own_error(store, clock):
    def inactive(_request):
        return httpx.Response(403, json={"error": "User account is not active"})

    with pytest.raises(AccountInactiveError):
        await make_minter(httpx.MockTransport(inactive), store, clock).token_for(identity(AGENT))


@pytest.mark.parametrize("email", ["9990000000000001@askii.ai", "9990000000000001", "@askii.ai", "x@"])
async def test_synthetic_identity_is_refused_with_zero_http_calls(minter, zammad, email):
    with pytest.raises(SyntheticIdentityError) as raised:
        await minter.token_for(identity(email))
    with pytest.raises(SyntheticIdentityError):
        await minter.renew(identity(email), "old")

    assert "verify your email in the portal first" in to_tool_error(raised.value)
    assert zammad.requests == []
    assert zammad.created_users == []


async def test_cleanup_deletes_only_this_servers_expired_tokens(minter, zammad):
    zammad.users[AGENT].tokens.extend(
        [
            {"id": 1, "name": "zammad-mcp (auto) 2020-01-01", "expires_at": "2020-01-01T00:00:00.000Z"},
            {"id": 2, "name": "zammad-mcp (auto) 2026-10-06", "expires_at": "2026-10-15T00:00:00.000Z"},
            {"id": 3, "name": "laptop", "expires_at": "2020-01-01T00:00:00.000Z"},
            {"id": 4, "name": "zammad-mcp (auto) undated", "expires_at": None},
            {"id": 5, "name": "zammad-mcp (auto) garbled", "expires_at": "soon"},
        ]
    )

    await minter.token_for(identity(AGENT))

    assert zammad.deleted == [1]
    (delete,) = [request for request in zammad.deletes() if "/user_access_token/" in request.url.path]
    assert delete.headers["cookie"] == f"{COOKIE_NAME}=sid-0"
    assert delete.headers["x-csrf-token"] == "csrf-0"


async def test_failed_cleanup_never_fails_the_mint(minter, zammad, monkeypatch):
    zammad.users[AGENT].tokens.extend(
        [
            {"id": 1, "name": "zammad-mcp (auto) a", "expires_at": "2020-01-01"},
            {"id": 2, "name": "zammad-mcp (auto) b", "expires_at": "2020-01-02"},
        ]
    )
    original = zammad._delete

    def flaky(owner, token_id):
        if token_id == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(500, json={"error": "nope"}) if token_id == 2 else original(owner, token_id)

    monkeypatch.setattr(zammad, "_delete", flaky)

    assert (await minter.token_for(identity(AGENT))).token == "minted-1"


def test_expired_ids_ignore_malformed_entries():
    now = START
    tokens = [
        {"id": True, "name": "zammad-mcp x", "expires_at": "2020-01-01"},
        {"id": "7", "name": "zammad-mcp x", "expires_at": "2020-01-01"},
        {"id": 8, "name": None, "expires_at": "2020-01-01"},
        {"id": 9, "name": "zammad-mcp x", "expires_at": "2020-01-01"},
    ]
    assert expired_mcp_token_ids(tokens, now) == [9]


def test_ttl_arithmetic():
    minted = MintedToken("t", frozenset({"ticket.agent"}), Tier.AGENT, "f", token_expiry(START), START)
    expiry = datetime(2026, 10, 16, tzinfo=UTC).timestamp()

    assert minted.expires_at == date(2026, 10, 16)
    assert minted.stale_at == expiry - 86400
    assert minted.stale_at - START > CACHE_TTL_SECONDS
    assert cache_ttl_seconds(minted, START) == CACHE_TTL_SECONDS
    assert cache_ttl_seconds(minted, minted.stale_at - 3600) == 3600
    assert cache_ttl_seconds(minted, minted.stale_at + 10) == 1
    assert not minted.stale(START + CACHE_TTL_SECONDS)
    assert minted.stale(minted.stale_at)
    assert not minted.due_for_recheck(START + RECHECK_INTERVAL_SECONDS - 1)
    assert minted.due_for_recheck(START + RECHECK_INTERVAL_SECONDS)


async def test_cache_expires_after_six_days_and_remints(minter, zammad, clock):
    await minter.token_for(identity(AGENT))
    clock.advance(CACHE_TTL_SECONDS + 1)

    assert (await minter.token_for(identity(AGENT))).token == "minted-2"


async def test_stale_entry_is_reminted(minter, zammad, store, clock):
    minted = await minter.token_for(identity(AGENT))
    cache = cache_for(store)
    await cache.put(identity(AGENT).key, dataclasses.replace(minted, expires_at=date(2026, 10, 8)), clock())

    assert (await minter.token_for(identity(AGENT))).token == "minted-2"


async def test_recheck_after_four_hours_keeps_an_unchanged_token(minter, zammad, clock):
    await minter.token_for(identity(AGENT))
    clock.advance(RECHECK_INTERVAL_SECONDS - 1)
    await minter.token_for(identity(AGENT))
    assert len(zammad.gets()) == 1

    clock.advance(1)
    rechecked = await minter.token_for(identity(AGENT))

    assert len(zammad.gets()) == 2
    assert len(zammad.posts()) == 1
    assert rechecked.token == "minted-1"
    assert rechecked.checked_at == clock()
    clock.advance(60)
    await minter.token_for(identity(AGENT))
    assert len(zammad.gets()) == 2


async def test_a_promotion_found_by_the_recheck_remints(minter, zammad, clock):
    assert (await minter.token_for(identity(CUSTOMER))).tier is Tier.CUSTOMER
    zammad.users[CUSTOMER].permissions = AGENT_PERMISSIONS
    clock.advance(RECHECK_INTERVAL_SECONDS)

    promoted = await minter.token_for(identity(CUSTOMER))

    assert promoted.tier is Tier.AGENT
    assert promoted.token == "minted-2"
    assert zammad.posted_body()["permission"] == ["knowledge_base.reader", "ticket.agent"]


async def test_a_recheck_denial_drops_the_cache_and_fails_closed(minter, zammad, store, clock):
    await minter.token_for(identity(CUSTOMER))
    zammad.users[CUSTOMER].token_access = False
    clock.advance(RECHECK_INTERVAL_SECONDS)

    with pytest.raises(CustomerTokenAccessError):
        await minter.token_for(identity(CUSTOMER))

    assert await store.get(cache_for(store).token_key(identity(CUSTOMER).key)) is None


async def test_a_recheck_that_empties_the_ceiling_fails_closed(minter, zammad, clock):
    await minter.token_for(identity(CUSTOMER))
    zammad.users[CUSTOMER].permissions = ("user_preferences.access_token",)
    clock.advance(RECHECK_INTERVAL_SECONDS)

    with pytest.raises(EmptyCeilingError):
        await minter.token_for(identity(CUSTOMER))


async def test_forbidden_limiter_allows_one_recheck_per_five_minutes(minter, zammad, clock):
    await minter.token_for(identity(AGENT))

    assert await minter.note_forbidden(identity(AGENT)) is True
    assert await minter.note_forbidden(identity(AGENT)) is False
    await minter.token_for(identity(AGENT))
    await minter.token_for(identity(AGENT))
    assert len(zammad.gets()) == 2

    clock.advance(FORBIDDEN_RECHECK_INTERVAL_SECONDS)
    assert await minter.note_forbidden(identity(AGENT)) is True


async def test_forbidden_without_a_cached_token_only_arms_the_limiter(minter, zammad):
    assert await minter.note_forbidden(identity(AGENT)) is True
    assert zammad.requests == []


async def test_a_401_remints_once(minter, zammad):
    first = await minter.token_for(identity(AGENT))

    renewed = await minter.renew(identity(AGENT), first.token)
    again = await minter.renew(identity(AGENT), first.token)

    assert renewed.token == "minted-2"
    assert again.token == "minted-2"
    assert len(zammad.posts()) == 2


async def test_narrow_ceiling_can_only_remove(zammad, store, clock):
    widened = make_minter(zammad, store, clock, narrow=lambda claims, ceiling: {*ceiling, "admin", "ticket.customer"})
    await widened.token_for(identity(AGENT))
    assert zammad.posted_body()["permission"] == ["knowledge_base.reader", "ticket.agent"]

    narrowed = make_minter(zammad, MemoryKeyValue(clock), clock, narrow=lambda claims, ceiling: {"ticket.agent"})
    await narrowed.token_for(identity(AGENT))
    assert zammad.posted_body()["permission"] == ["ticket.agent"]

    emptied = make_minter(zammad, MemoryKeyValue(clock), clock, narrow=lambda claims, ceiling: set())
    with pytest.raises(EmptyCeilingError):
        await emptied.token_for(identity(CUSTOMER))


async def test_cache_entry_is_encrypted_under_a_hashed_key(minter, store):
    await minter.token_for(identity(AGENT))

    ((key, (value, _expires)),) = store._values.items()
    assert key == f"{CACHE_KEY_PREFIX}{NAMESPACE}:" + hashlib.sha256(AGENT.encode()).hexdigest()
    assert "minted-1" not in value
    assert AGENT not in value


class BrokenStore(MemoryKeyValue):
    def __init__(self, clock, *, fail_get: bool = False, fail_set: bool = False) -> None:
        super().__init__(clock)
        self.fail_get = fail_get
        self.fail_set = fail_set

    async def get_many(self, keys):
        if self.fail_get:
            raise ConnectionError("valkey down")
        return await super().get_many(keys)

    async def set_if_absent(self, key, value, ttl_seconds):
        if self.fail_set:
            raise ConnectionError("valkey down")
        return await super().set_if_absent(key, value, ttl_seconds)

    async def set(self, key, value, ttl_seconds):
        if self.fail_set:
            raise ConnectionError("valkey down")
        await super().set(key, value, ttl_seconds)

    async def delete(self, key):
        raise ConnectionError("valkey down")


async def test_cache_read_error_fails_closed_without_minting(zammad, clock):
    minter = make_minter(zammad, BrokenStore(clock, fail_get=True), clock)

    with pytest.raises(TokenCacheError):
        await minter.token_for(identity(AGENT))

    assert zammad.requests == []


async def test_cache_write_error_fails_closed(zammad, clock):
    with pytest.raises(TokenCacheError):
        await make_minter(zammad, BrokenStore(clock, fail_set=True), clock).token_for(identity(AGENT))


async def test_a_failed_cache_drop_is_only_logged(zammad, clock):
    minter = make_minter(zammad, BrokenStore(clock), clock)
    zammad.users[CUSTOMER].token_access = False

    with pytest.raises(CustomerTokenAccessError):
        await minter.token_for(identity(CUSTOMER))


async def test_an_unreadable_cache_entry_is_reminted(minter, store, zammad):
    await store.set(cache_for(store).token_key(identity(AGENT).key), "not-fernet", 60)

    assert (await minter.token_for(identity(AGENT))).token == "minted-1"


def response_from(status: int, **options):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(status, **options)
        return httpx.Response(200, json={"token": "t"})

    return httpx.MockTransport(handler)


def session_handler(post: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"tokens": "nope", "permissions": ["ticket.agent", 7]},
                headers={"set-cookie": f"{COOKIE_NAME}=s; secure", "csrf-token": "c"},
            )
        return post

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        (response_from(200, content=b"<html>"), "other than JSON"),
        (response_from(200, json=["list"]), "unexpected shape"),
        (response_from(200, json={"permissions": []}), "did not open a session"),
        (session_handler(httpx.Response(422, json={"error": "bad"})), "refused to create a token (HTTP 422: bad)"),
        (session_handler(httpx.Response(200, json={"token": ""})), "did not return a usable value"),
        (session_handler(httpx.Response(200, json={"token": "has space"})), "did not return a usable value"),
    ],
)
async def test_unusable_zammad_answers_fail_closed(transport, message, store, clock):
    with pytest.raises(MintError, match=message.replace("(", r"\(").replace(")", r"\)")):
        await make_minter(transport, store, clock).token_for(identity(AGENT))


async def test_a_session_answer_with_odd_lists_still_mints(store, clock):
    minted = await make_minter(session_handler(httpx.Response(200, json={"token": "ok"})), store, clock).token_for(
        identity(AGENT)
    )

    assert minted.permissions == frozenset({"ticket.agent"})


async def test_unreachable_zammad_fails_closed(store, clock):
    def down(_request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ZammadTransportError):
        await make_minter(httpx.MockTransport(down), store, clock).token_for(identity(AGENT))


def test_session_cookie_is_found_by_prefix_whatever_the_suffix():
    response = httpx.Response(
        200, headers=[("set-cookie", "other=1; path=/"), ("set-cookie", "_zammad_session_ffff=abc; secure")]
    )
    assert session_cookie(response) == "_zammad_session_ffff=abc"
    assert session_cookie(httpx.Response(200, headers={"set-cookie": "_zammad_session_x; secure"})) is None


def test_minted_token_round_trips_through_json():
    minted = MintedToken("tok", frozenset({"ticket.customer"}), Tier.CUSTOMER, "fp", date(2026, 10, 16), 12.5)

    assert MintedToken.from_json(minted.to_json()) == minted
    assert "tok" not in repr(minted)


async def test_keyed_locks_are_released_after_use():
    locks = KeyedLocks()
    async with locks.hold("a"):
        assert len(locks) == 1
    assert len(locks) == 0


def test_permission_names_reads_zammads_permission_objects():
    raw = [
        {"id": 1, "name": "ticket.agent", "active": True},
        {"id": 2, "name": "knowledge_base.editor", "active": False},
        {"id": 3, "name": "knowledge_base.reader"},
        "ticket.customer",
        {"id": 4},
        7,
    ]

    assert permission_names(raw) == ("ticket.agent", "knowledge_base.reader", "ticket.customer")
    assert permission_names({"ticket.agent": True}) == ()


def assert_signed_out_every_session(zammad: FakeSsoZammad) -> None:
    assert zammad.sessions
    assert zammad.live_sessions() == []
    for request in zammad.signouts():
        assert request.method == "DELETE"
        assert request.headers["cookie"].startswith(f"{COOKIE_NAME}=sid-")
        assert request.headers["x-browser-fingerprint"] == DEVICE_FINGERPRINT
        assert request.headers["user-agent"] == USER_AGENT
        assert not [name for name in request.headers if is_identity_header(name)]


async def test_every_bootstrap_ends_with_a_signout(minter, zammad, clock):
    first = await minter.token_for(identity(AGENT))
    clock.advance(RECHECK_INTERVAL_SECONDS)
    await minter.token_for(identity(AGENT))
    await minter.renew(identity(AGENT), first.token)

    assert len(zammad.gets()) == 3
    assert len(zammad.signouts()) == 3
    assert zammad.signouts()[0].headers["x-csrf-token"] == "csrf-0"
    assert_signed_out_every_session(zammad)


async def test_a_failed_post_still_signs_out(minter, zammad, monkeypatch):
    monkeypatch.setattr(zammad, "_create", lambda owner, request: httpx.Response(500, json={"error": "boom"}))

    with pytest.raises(MintError):
        await minter.token_for(identity(AGENT))

    assert_signed_out_every_session(zammad)


async def test_a_refused_bootstrap_still_signs_its_session_out(minter, zammad):
    zammad.users[CUSTOMER].token_access = False

    with pytest.raises(CustomerTokenAccessError):
        await minter.token_for(identity(CUSTOMER))

    (signout,) = zammad.signouts()
    assert "x-csrf-token" not in signout.headers
    assert_signed_out_every_session(zammad)


async def test_a_failed_signout_never_fails_the_mint(store, clock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/signout"):
            if request.headers["cookie"].endswith("=s1"):
                raise httpx.ConnectError("gone")
            return httpx.Response(500, json={"error": "nope"})
        if request.method == "GET":
            sid = "s1" if request.headers["x-auth-request-email"] == AGENT else "s2"
            return httpx.Response(
                200,
                json={"tokens": [], "permissions": ["ticket.agent"]},
                headers={"set-cookie": f"{COOKIE_NAME}={sid}; secure", "csrf-token": "c"},
            )
        return httpx.Response(200, json={"token": "ok"})

    minter = make_minter(httpx.MockTransport(handler), store, clock)

    assert (await minter.token_for(identity(AGENT))).token == "ok"
    assert (await minter.token_for(identity(CUSTOMER))).token == "ok"


async def test_no_signout_without_a_session(store, clock):
    calls = []

    def down(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("refused")

    with pytest.raises(ZammadTransportError):
        await make_minter(httpx.MockTransport(down), store, clock).token_for(identity(AGENT))

    assert calls == ["/api/v1/user_access_token"]


async def test_a_real_address_on_the_synthetic_domain_is_minted(minter, zammad):
    zammad.add("jane@askii.ai", CUSTOMER_PERMISSIONS)

    minted = await minter.token_for(identity("jane@askii.ai", username="sid-42"))

    assert minted.tier is Tier.CUSTOMER
    assert zammad.gets()[0].headers["x-auth-request-email"] == "jane@askii.ai"


async def test_the_exact_synthetic_shape_is_refused(minter, zammad):
    with pytest.raises(SyntheticIdentityError):
        await minter.token_for(identity("sid-42@askii.ai", username="sid-42"))

    assert zammad.requests == []


async def test_a_forbidden_marker_never_overwrites_a_concurrent_remint(minter, zammad, store, clock):
    first = await minter.token_for(identity(AGENT))
    clock.advance(1)
    zammad.revoked.add(first.token)

    renewed, marked = await asyncio.gather(
        minter.renew(identity(AGENT), first.token), minter.note_forbidden(identity(AGENT))
    )

    cached = (await cache_for(store).load(identity(AGENT).key)).minted
    assert marked is True
    assert renewed.token == cached.token == "minted-2"
    assert (await minter.token_for(identity(AGENT))).token == "minted-2"


async def test_a_forbidden_marker_does_not_touch_the_cached_value(minter, store, clock):
    await minter.token_for(identity(AGENT))
    key = cache_for(store).token_key(identity(AGENT).key)
    before = await store.get(key)

    await minter.note_forbidden(identity(AGENT))

    assert await store.get(key) == before


async def test_a_broken_marker_store_fails_closed(zammad, clock):
    with pytest.raises(TokenCacheError):
        await make_minter(zammad, BrokenStore(clock, fail_set=True), clock).note_forbidden(identity(AGENT))


def audit_lines(caplog) -> list[dict]:
    return [json.loads(record.getMessage()) for record in caplog.records if record.name == "zammad_mcp.audit"]


async def test_audit_lines_record_every_token_decision(minter, zammad, clock, caplog):
    caplog.set_level(logging.INFO, logger="zammad_mcp.audit")
    customer = identity(CUSTOMER)

    first = await minter.token_for(customer)
    clock.advance(RECHECK_INTERVAL_SECONDS)
    await minter.token_for(customer)
    zammad.users[CUSTOMER].permissions = AGENT_PERMISSIONS
    clock.advance(RECHECK_INTERVAL_SECONDS)
    await minter.token_for(customer)
    await minter.renew(customer, first.token)
    await minter.note_forbidden(customer)
    zammad.users[CUSTOMER].token_access = False
    with pytest.raises(CustomerTokenAccessError):
        await minter.renew(customer, "minted-2")
    with pytest.raises(SyntheticIdentityError):
        await minter.token_for(identity("x@askii.ai"))

    lines = audit_lines(caplog)
    assert [(line["event"], line.get("reason")) for line in lines] == [
        ("token.minted", "first"),
        ("token.recheck_unchanged", None),
        ("token.minted", "permissions_changed"),
        ("token.renew_reused", None),
        ("token.recheck_requested", "forbidden"),
        ("token.denied", "CustomerTokenAccessError"),
        ("token.refused", "synthetic_identity"),
    ]
    minted = lines[0]
    assert minted["identity"] == customer.short_key
    assert minted["tier"] == "customer"
    assert minted["permissions"] == ["ticket.customer"]
    assert minted["token_name"] == "zammad-mcp (auto) 2026-10-07"
    assert lines[2]["tier"] == "agent"
    text = caplog.text + "".join(json.dumps(line) for line in lines)
    assert CUSTOMER not in text
    assert "minted-1" not in text
    assert "minted-2" not in text


def test_cache_entry_recheck_rules():
    minted = MintedToken("t", frozenset({"ticket.agent"}), Tier.AGENT, "f", date(2026, 10, 16), 100.0)

    assert CacheEntry(None, recheck_flagged=True).due_for_recheck(200.0) is False
    assert CacheEntry(minted).due_for_recheck(200.0) is False
    assert CacheEntry(minted, recheck_flagged=True).due_for_recheck(200.0) is True
    assert CacheEntry(minted).due_for_recheck(100.0 + RECHECK_INTERVAL_SECONDS) is True


async def test_the_due_flag_needs_no_clock_agreement(minter, zammad, store, clock):
    await minter.token_for(identity(AGENT))
    await minter.note_forbidden(identity(AGENT))
    clock.advance(-3600)

    await minter.token_for(identity(AGENT))
    await minter.token_for(identity(AGENT))

    assert len(zammad.gets()) == 2
    assert await store.get(cache_for(store).due_key(identity(AGENT).key)) is None


async def test_a_mint_clears_a_pending_flag(minter, zammad, store):
    await minter.note_forbidden(identity(AGENT))

    await minter.token_for(identity(AGENT))
    await minter.token_for(identity(AGENT))

    assert len(zammad.gets()) == 1
    assert await store.get(cache_for(store).due_key(identity(AGENT).key)) is None
    assert await store.get(cache_for(store).limit_key(identity(AGENT).key)) == "1"


async def test_a_flag_that_cannot_be_cleared_costs_one_more_recheck(zammad, clock):
    class StuckFlags(MemoryKeyValue):
        async def delete(self, key):
            raise ConnectionError("valkey down")

    minter = make_minter(zammad, StuckFlags(clock), clock)
    await minter.note_forbidden(identity(AGENT))

    assert (await minter.token_for(identity(AGENT))).token == "minted-1"


async def test_another_users_synthetic_address_is_refused(minter, zammad):
    victim = Identity(email="victim-sid@askii.ai", claims={"id_token": "id", "cognito:username": "attacker-sid"})

    with pytest.raises(SyntheticIdentityError):
        await minter.token_for(victim)

    assert zammad.requests == []


async def test_a_cancelled_bootstrap_still_signs_out(store, clock):
    posted = asyncio.Event()
    signouts: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/signout"):
            await asyncio.sleep(0)
            signouts.append(request)
            return httpx.Response(200, json={})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"tokens": [], "permissions": ["ticket.agent"]},
                headers={"set-cookie": f"{COOKIE_NAME}=held; secure", "csrf-token": "c"},
            )
        posted.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    minter = make_minter(httpx.MockTransport(handler), store, clock)
    async with anyio.create_task_group() as group:
        group.start_soon(minter.token_for, identity(AGENT))
        await posted.wait()
        group.cancel_scope.cancel()

    (signout,) = signouts
    assert signout.headers["cookie"] == f"{COOKIE_NAME}=held"


async def test_a_hanging_signout_gives_up(store, clock, monkeypatch):
    monkeypatch.setattr("zammad_mcp.platform.mint.SIGNOUT_TIMEOUT_SECONDS", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/signout"):
            await asyncio.Event().wait()
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"tokens": [], "permissions": ["ticket.agent"]},
                headers={"set-cookie": f"{COOKIE_NAME}=s; secure", "csrf-token": "c"},
            )
        return httpx.Response(200, json={"token": "ok"})

    minted = await asyncio.wait_for(
        make_minter(httpx.MockTransport(handler), store, clock).token_for(identity(AGENT)), 2
    )

    assert minted.token == "ok"
