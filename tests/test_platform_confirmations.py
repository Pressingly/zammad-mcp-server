"""Part 2's confirmations (email replies, per-identity limits) on the platform Valkey backend."""

from __future__ import annotations

import json

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastmcp import Client

from tests.conftest import FakeZammad
from tests.platform_fakes import AGENT_PERMISSIONS, FakeSsoZammad, identity
from tests.test_email import PREPARED, mailbox, send_args, with_email
from tests.test_platform_mint import AGENT, Clock, make_minter
from zammad_mcp import server
from zammad_mcp.client import ZammadClient
from zammad_mcp.config import Settings
from zammad_mcp.confirmations import ConfirmationLimitError, Confirmations
from zammad_mcp.platform.credentials import MintedCredentialProvider, platform_tier_resolver
from zammad_mcp.platform.storage import (
    CONFIRMATION_SALT,
    ConfirmationStoreFullError,
    MemoryKeyValue,
    ValkeyConfirmationBackend,
    fernet_for,
)

FERNET = fernet_for("client-secret", CONFIRMATION_SALT)
NON_ASCII_BODY = "Wir kümmern uns darum. 我们正在处理. SECRET-BODY-TEXT"


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


def valkey(redis, **options) -> ValkeyConfirmationBackend:
    return ValkeyConfirmationBackend(redis, FERNET, namespace="test", **options)


async def stored_values(redis) -> list[str]:
    return [await redis.get(key) for key in await redis.keys("zammad-mcp:confirm:v1:*")]


async def test_a_kept_email_payload_round_trips_encrypted(settings, redis):
    zammad = mailbox(FakeZammad())
    backend = valkey(redis)
    built = server.build_server(with_email(settings), transport=zammad, confirmation_backend=backend)

    async with Client(built) as client:
        preview = (await client.call_tool("prepare_email_reply", {**PREPARED, "body": NON_ASCII_BODY})).data
        (sealed,) = await stored_values(redis)
        held = await backend.held_bytes()
        sent = (await client.call_tool("send_email_reply", send_args(preview))).data

    assert "SECRET-BODY-TEXT" not in sealed
    assert "email_reply" not in sealed
    plain = FERNET.decrypt(sealed.encode()).decode()
    assert held == len(plain.encode("utf-8"))
    assert "kümmern" in plain, "records keep non-ASCII text as is (ensure_ascii=False)"
    assert json.loads(plain)["payload"]["body"] == NON_ASCII_BODY
    assert sent["status"] == "queued"
    assert zammad.last_json("POST", "/ticket_articles")["body"] == NON_ASCII_BODY
    assert await stored_values(redis) == []
    assert await backend.held_bytes() == 0


async def test_a_full_valkey_store_is_a_clean_tool_error(settings, redis):
    zammad = mailbox(FakeZammad())
    backend = valkey(redis, max_bytes=256, small_record_reserve=0)
    built = server.build_server(with_email(settings), transport=zammad, confirmation_backend=backend)

    async with Client(built) as client:
        result = (await client.call_tool("prepare_email_reply", PREPARED)).data

    assert isinstance(ConfirmationStoreFullError("x"), ConfirmationLimitError)
    assert result == (
        "Error: too many large actions are waiting for confirmation on this server; "
        "confirm or abandon some and try again in a few minutes"
    )
    assert await stored_values(redis) == []


async def test_the_per_identity_share_holds_on_valkey(redis):
    backend = valkey(redis)
    confirmations = Confirmations(backend, ttl_seconds=600, max_bytes_per_identity=4096)
    payload = {"body": "x" * 1500}

    first = await confirmations.issue(identity="ada-hash", action="email_reply", payload=payload, keep_payload=True)
    await confirmations.issue(identity="ada-hash", action="email_reply", payload=payload, keep_payload=True)
    with pytest.raises(ConfirmationLimitError, match="share of the confirmation store"):
        await confirmations.issue(identity="ada-hash", action="email_reply", payload=payload, keep_payload=True)
    await confirmations.issue(identity="cara-hash", action="email_reply", payload=payload, keep_payload=True)

    assert await backend.held_bytes() == confirmations.held_bytes("ada-hash") + confirmations.held_bytes("cara-hash")
    assert await confirmations.redeem(first, identity="ada-hash", action="email_reply") == payload
    await confirmations.issue(identity="ada-hash", action="email_reply", payload=payload, keep_payload=True)


async def test_the_slot_cap_holds_on_valkey(redis):
    confirmations = Confirmations(valkey(redis), ttl_seconds=600, max_outstanding_per_identity=2)
    for _ in range(2):
        await confirmations.issue(identity="ada-hash", action="merge", payload={})

    with pytest.raises(ConfirmationLimitError, match="already have 2 actions"):
        await confirmations.issue(identity="ada-hash", action="merge", payload={})


def split_transport(sso: FakeSsoZammad, tickets: FakeZammad) -> httpx.MockTransport:
    """Token bootstrap and signout go to the SSO fake, everything else to the ticket fake."""

    def route(request: httpx.Request) -> httpx.Response:
        bootstrap = "user_access_token" in request.url.path or request.url.path.endswith("/signout")
        return sso._dispatch(request) if bootstrap else tickets._dispatch(request)

    return httpx.MockTransport(route)


async def test_platform_confirmations_are_bound_to_the_identity_hash(redis):
    sso = FakeSsoZammad()
    sso.add(AGENT, AGENT_PERMISSIONS)
    transport = split_transport(sso, mailbox(FakeZammad()))
    clock = Clock()
    who = identity(AGENT)
    minter = make_minter(transport, MemoryKeyValue(clock), clock)
    credentials = MintedCredentialProvider(minter, identity_source=lambda: who)
    settings = with_email(Settings(zammad_url="http://zammad-nginx:8080"))
    built = server.build_server(
        settings,
        client=ZammadClient(settings.api_base_url, transport=transport, on_rejected=credentials.on_rejected),
        credentials=credentials,
        tier_resolver=platform_tier_resolver(credentials, identity_of=lambda _token: who),
        confirmation_backend=valkey(redis),
    )

    async with Client(built) as client:
        preview = (await client.call_tool("prepare_email_reply", PREPARED)).data

    assert "confirmation_token" in preview
    (sealed,) = await stored_values(redis)
    record = json.loads(FERNET.decrypt(sealed.encode()).decode())
    assert record["identity"] == who.short_key
    assert AGENT not in json.dumps(record)
