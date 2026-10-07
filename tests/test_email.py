from __future__ import annotations

import base64
import dataclasses
import json
import logging
from functools import partial
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp import Client
from starlette.testclient import TestClient

from tests.conftest import FakeZammad, admin_only_fake, unknown_role_fake
from tests.helpers import rpc, tool_text, tools
from zammad_mcp import server
from zammad_mcp.config import Settings
from zammad_mcp.confirmations import Confirmations, MemoryConfirmationBackend
from zammad_mcp.credentials.base import token_identity
from zammad_mcp.tools import email as email_tools
from zammad_mcp.tools.email import (
    CONFIRMATION_STORE_DOWN,
    MAX_RECIPIENTS,
    normalize_recipients,
    parse_addresses,
)

EMAIL_TOOLS = {"prepare_email_reply", "send_email_reply"}
TICKET = {"id": 4, "number": "20004", "title": "Printer on fire", "group_id": 1, "group": "Users", "customer_id": 9}
GROUP = {"id": 1, "name": "Users", "email_address_id": 3}
SENDER = {"id": 3, "email": "Support@Example.com", "active": True, "channel_id": 5}
ARTICLES = [
    {"id": 1, "ticket_id": 4, "sender": "Customer", "type": "email", "from": "Cara <cara@example.com>"},
    {
        "id": 2,
        "ticket_id": 4,
        "sender": "Agent",
        "type": "email",
        "from": "Support <support@example.com>",
        "to": "cara@example.com",
        "cc": "Boss <boss@example.com>",
    },
    {
        "id": 3,
        "ticket_id": 4,
        "sender": "Customer",
        "type": "email",
        "from": "Cara <cara@example.com>",
        "reply_to": "cara-replies@example.com",
        "cc": "Colleague <colleague@example.com>, support@example.com",
    },
    {"id": 0, "ticket_id": 4, "sender": "Customer", "type": "web", "from": "Old Cara <old-cara@example.com>"},
]
CREATED = {"id": 50, "ticket_id": 4, "type": "email", "sender": "Agent", "internal": False}
PREPARED = {"ticket_id": 4, "subject": "Re: printer", "body": "We are on it. SECRET-BODY-TEXT"}


def mailbox(fake: FakeZammad, *, group: dict | None = None, sender: dict | None = None) -> FakeZammad:
    fake.on("GET", "/tickets/4", json=TICKET)
    fake.on("GET", "/groups/1", json=group or GROUP)
    fake.on("GET", "/email_addresses/3", json=sender or SENDER)
    fake.on("GET", "/ticket_articles/by_ticket/4", json=ARTICLES)
    fake.on("GET", "/users/9", json={"id": 9, "email": "Cara@Example.com"})
    fake.on("POST", "/ticket_articles", status=201, json=CREATED)
    return fake


STRAY_SEND = {"confirmation_token": "t", "to": ["a@b.test"], "subject": "s"}


def with_email(settings: Settings) -> Settings:
    return dataclasses.replace(settings, enabled_modules=settings.enabled_modules | {"email_replies"})


@pytest.fixture
def email_settings(settings) -> Settings:
    return with_email(settings)


@pytest.fixture
def zammad(fake) -> FakeZammad:
    return mailbox(fake)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(monkeypatch) -> SimpleNamespace:
    """Give the server a confirmation store whose clock and records the test can reach."""
    clock = Clock()
    backends: list[MemoryConfirmationBackend] = []

    def backend() -> MemoryConfirmationBackend:
        backends.append(MemoryConfirmationBackend(clock=clock))
        return backends[-1]

    monkeypatch.setattr(server, "MemoryConfirmationBackend", backend)
    monkeypatch.setattr(server, "Confirmations", partial(Confirmations, clock=clock))
    return SimpleNamespace(clock=clock, backends=backends)


async def session(settings: Settings, fake: httpx.AsyncBaseTransport, *calls: tuple[str, dict[str, Any]]) -> list:
    """Run several tool calls against one server, so they share its confirmation store."""
    async with Client(server.build_server(settings, transport=fake)) as client:
        return [(await client.call_tool(name, arguments)).data for name, arguments in calls]


def send_args(preview: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "confirmation_token": preview["confirmation_token"],
        "to": preview["to"],
        "subject": preview["subject"],
        **changes,
    }


async def prepare_then_send(settings: Settings, fake: FakeZammad, prepare: dict[str, Any], **send_changes: Any):
    async with Client(server.build_server(settings, transport=fake)) as client:
        preview = (await client.call_tool("prepare_email_reply", prepare)).data
        sent = (await client.call_tool("send_email_reply", send_args(preview, **send_changes))).data
    return preview, sent


# --- registration ---


async def test_flag_off_by_default_means_the_tools_are_absent(settings, zammad):
    assert not set(await tools(settings, zammad)) & EMAIL_TOOLS
    assert not Settings.from_env({"ZAMMAD_URL": "https://z.test"}).module_enabled("email_replies")


async def test_flag_on_registers_both_tools_with_their_annotations(email_settings, zammad):
    listed = await tools(email_settings, zammad)
    prepare, send = listed["prepare_email_reply"], listed["send_email_reply"]
    hints = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    assert tuple(getattr(prepare.annotations, hint) for hint in hints) == (True, False, False, True)
    assert tuple(getattr(send.annotations, hint) for hint in hints) == (False, True, False, True)
    for tool in (prepare, send):
        assert {"tier:agent", "module:email_replies"} <= set(tool.meta["fastmcp"]["tags"])


async def test_read_only_mode_drops_the_email_tools(email_settings, zammad):
    assert not set(await tools(dataclasses.replace(email_settings, read_only=True), zammad)) & EMAIL_TOOLS


# --- happy path ---


async def test_prepare_then_send_posts_one_email_article(email_settings, zammad, caplog):
    caplog.set_level(logging.INFO, logger="zammad_mcp.audit")
    preview, sent = await prepare_then_send(email_settings, zammad, PREPARED)
    assert preview["to"] == ["cara-replies@example.com"]
    assert preview["cc"] == []
    assert preview["from"] == "support@example.com"
    assert preview["subject"] == "Re: printer"
    assert preview["expires_in_seconds"] == 600
    assert preview["url"] == "https://zammad.test/#ticket/zoom/4"
    assert "send_email_reply" in preview["next_step"]
    assert sent == {
        "article_id": 50,
        "ticket_id": 4,
        "status": "queued",
        "note": "Zammad sends the email in the background; a delivery failure shows up on the ticket.",
        "url": "https://zammad.test/#ticket/zoom/4/50",
    }
    assert zammad.last_json("POST", "/ticket_articles") == {
        "ticket_id": 4,
        "type": "email",
        "sender": "Agent",
        "internal": False,
        "to": "cara-replies@example.com",
        "subject": "Re: printer",
        "body": "We are on it. SECRET-BODY-TEXT",
        "content_type": "text/plain",
    }
    (record,) = [record for record in caplog.records if record.name == "zammad_mcp.audit"]
    line = record.getMessage()
    assert f"identity={token_identity('secret-token')}" in line
    assert "ticket_id=4 article_id=50 recipients=1 attachments=0" in line
    assert "SECRET-BODY-TEXT" not in line
    assert "secret-token" not in line


async def test_prepare_never_writes_to_zammad(email_settings, zammad):
    await session(email_settings, zammad, ("prepare_email_reply", PREPARED))
    assert zammad.calls("POST", "/ticket_articles") == []
    assert {request.method for request in zammad.requests} == {"GET"}


async def test_explicit_recipients_are_normalized_and_cc_is_deduplicated(email_settings, zammad):
    prepare = {**PREPARED, "to": ["Cara <CARA@example.com>"], "cc": ["boss@example.com", "cara@example.com"]}
    preview, sent = await prepare_then_send(email_settings, zammad, prepare)
    assert (preview["to"], preview["cc"]) == (["cara@example.com"], ["boss@example.com"])
    body = zammad.last_json("POST", "/ticket_articles")
    assert (body["to"], body["cc"]) == ("cara@example.com", "boss@example.com")
    assert sent["status"] == "queued"


async def test_echoed_to_may_differ_in_case_and_order(email_settings, zammad):
    prepare = {**PREPARED, "to": ["cara@example.com", "colleague@example.com"]}
    _, sent = await prepare_then_send(
        email_settings, zammad, prepare, to=["COLLEAGUE@example.com", "Cara <cara@example.com>"]
    )
    assert sent["status"] == "queued"


async def test_attachments_are_previewed_and_posted(email_settings, zammad):
    data = base64.b64encode(b"hello world").decode()
    attachment = {"filename": "note.txt", "data": f"{data[:4]}\n{data[4:]}", "mime_type": "text/plain"}
    preview, _ = await prepare_then_send(email_settings, zammad, {**PREPARED, "attachments": [attachment]})
    assert preview["attachments"] == [{"filename": "note.txt", "mime_type": "text/plain", "size": 11}]
    posted = zammad.last_json("POST", "/ticket_articles")["attachments"]
    assert posted == [{"filename": "note.txt", "data": data, "mime-type": "text/plain"}]


# --- recipient allow-list ---


@pytest.mark.parametrize(
    "to",
    [["attacker@evil.test"], ["cara@example.com", "attacker@evil.test"], ["support@example.com"]],
)
async def test_recipients_outside_the_ticket_are_refused(email_settings, zammad, store, to):
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", {**PREPARED, "to": to}))
    assert shaped.startswith("Error: ")
    assert "not a participant of this ticket" in shaped
    assert store.backends[0]._values == {}


async def test_cc_outside_the_ticket_is_refused(email_settings, zammad):
    prepare = {**PREPARED, "cc": ["attacker@evil.test"]}
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", prepare))
    assert shaped == (
        "Error: attacker@evil.test is not a participant of this ticket; replies can only go to the ticket's "
        "customer or an address already on one of its articles"
    )


async def test_allow_any_recipient_lifts_the_participant_check(email_settings, zammad):
    allow_any = dataclasses.replace(email_settings, email_allow_any_recipient=True)
    preview, sent = await prepare_then_send(allow_any, zammad, {**PREPARED, "to": ["partner@elsewhere.test"]})
    assert preview["to"] == ["partner@elsewhere.test"]
    assert sent["status"] == "queued"


async def test_more_than_ten_recipients_in_total_are_refused(email_settings, zammad):
    allow_any = dataclasses.replace(email_settings, email_allow_any_recipient=True)
    to = [f"to{index}@example.com" for index in range(6)]
    cc = [f"cc{index}@example.com" for index in range(5)]
    (shaped,) = await session(allow_any, zammad, ("prepare_email_reply", {**PREPARED, "to": to, "cc": cc}))
    assert shaped == f"Error: at most {MAX_RECIPIENTS} recipients (to and cc together)"


async def test_more_than_ten_addresses_in_one_field_fail_validation(email_settings, zammad):
    to = [f"to{index}@example.com" for index in range(11)]
    async with Client(server.build_server(email_settings, transport=zammad)) as client:
        with pytest.raises(Exception, match="10"):
            await client.call_tool("prepare_email_reply", {**PREPARED, "to": to})


@pytest.mark.parametrize("value", ["not an address", "a@b.test, c@d.test", "<script>@x.test", "üser@example.com"])
async def test_malformed_recipients_are_refused(email_settings, zammad, value):
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", {**PREPARED, "to": [value]}))
    assert shaped == f"Error: {value!r} is not one plain email address (name@example.com)"


async def test_no_customer_article_and_no_to_is_an_error(email_settings, zammad):
    zammad.on("GET", "/ticket_articles/by_ticket/4", json=[ARTICLES[1]])
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", PREPARED))
    assert shaped == "Error: this ticket has no customer email to reply to; pass `to` explicitly"


async def test_ticket_without_customer_still_uses_article_participants(email_settings, zammad):
    zammad.on("GET", "/tickets/4", json={**TICKET, "customer_id": None})
    (preview,) = await session(
        email_settings, zammad, ("prepare_email_reply", {**PREPARED, "to": ["boss@example.com"]})
    )
    assert preview["to"] == ["boss@example.com"]
    assert zammad.calls("GET", "/users/9") == []


async def test_default_recipient_falls_back_to_from(email_settings, zammad):
    zammad.on("GET", "/ticket_articles/by_ticket/4", json=ARTICLES[:2])
    (preview,) = await session(email_settings, zammad, ("prepare_email_reply", PREPARED))
    assert preview["to"] == ["cara@example.com"]


# --- preflight ---


@pytest.mark.parametrize(
    ("group", "sender", "expected"),
    [
        (
            {**GROUP, "email_address_id": None},
            None,
            "Error: the ticket's group Users has no outgoing email address; "
            "a Zammad admin must set one (Admin, Groups) before agents can reply by email",
        ),
        (
            None,
            {**SENDER, "active": False},
            "Error: the group's email address Support@Example.com has no active email channel; "
            "a Zammad admin must set up outgoing email (Admin, Channels, Email)",
        ),
        (
            None,
            {**SENDER, "channel_id": None},
            "Error: the group's email address Support@Example.com has no active email channel; "
            "a Zammad admin must set up outgoing email (Admin, Channels, Email)",
        ),
    ],
)
async def test_preflight_explains_a_missing_email_setup(email_settings, fake, group, sender, expected):
    zammad = mailbox(fake, group=group, sender=sender)
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", PREPARED))
    assert shaped == expected
    assert zammad.calls("GET", "/ticket_articles/by_ticket/4") == []


async def test_preflight_needs_a_group(email_settings, zammad):
    zammad.on("GET", "/tickets/4", json={**TICKET, "group_id": None})
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", PREPARED))
    assert shaped == "Error: ticket 4 has no group, so Zammad has no address to send from"


@pytest.mark.parametrize("path", ["/tickets/4", "/groups/1", "/email_addresses/3", "/ticket_articles/by_ticket/4"])
async def test_preflight_zammad_errors_become_error_strings(email_settings, zammad, path):
    zammad.on("GET", path, status=403, json={"error": "Not authorized"})
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", PREPARED))
    assert shaped.startswith("Error: permission denied (HTTP 403: Not authorized)")


async def test_prepare_without_a_token(email_settings, zammad):
    (shaped,) = await session(
        dataclasses.replace(email_settings, http_token=None), zammad, ("prepare_email_reply", PREPARED)
    )
    assert shaped == "Error: no Zammad API token is configured for this request"


# --- attachments ---


async def test_invalid_base64_is_refused(email_settings, zammad):
    attachment = {"filename": "x.bin", "data": "not base64!"}
    (shaped,) = await session(
        email_settings, zammad, ("prepare_email_reply", {**PREPARED, "attachments": [attachment]})
    )
    assert shaped == "Error: attachment 0 ('x.bin') is not valid base64"


async def test_attachments_over_the_limit_are_refused(email_settings, zammad, monkeypatch):
    monkeypatch.setattr(email_tools, "MAX_ATTACHMENT_BYTES", 10)
    data = base64.b64encode(b"123456").decode()
    attachments = [{"filename": "a", "data": data}, {"filename": "b", "data": data}]
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", {**PREPARED, "attachments": attachments}))
    assert shaped == "Error: attachments total 12 bytes, over the 10-byte limit"


def test_the_attachment_limit_is_ten_megabytes():
    assert email_tools.MAX_ATTACHMENT_BYTES == 10 * 1024 * 1024


# --- confirmation token ---


async def test_token_is_single_use(email_settings, zammad):
    async with Client(server.build_server(email_settings, transport=zammad)) as client:
        preview = (await client.call_tool("prepare_email_reply", PREPARED)).data
        first = (await client.call_tool("send_email_reply", send_args(preview))).data
        second = (await client.call_tool("send_email_reply", send_args(preview))).data
    assert first["status"] == "queued"
    assert second == "Error: confirmation token is unknown, already used or expired; prepare the action again"
    assert len(zammad.calls("POST", "/ticket_articles")) == 1


async def test_expired_token_is_refused(email_settings, zammad, store):
    async with Client(server.build_server(email_settings, transport=zammad)) as client:
        preview = (await client.call_tool("prepare_email_reply", PREPARED)).data
        store.clock.now += 601
        sent = (await client.call_tool("send_email_reply", send_args(preview))).data
    assert sent == "Error: confirmation token is unknown, already used or expired; prepare the action again"
    assert zammad.calls("POST", "/ticket_articles") == []


async def test_unknown_token_is_refused(email_settings, zammad):
    args = {"confirmation_token": "made-up", "to": ["cara@example.com"], "subject": "x"}
    (sent,) = await session(email_settings, zammad, ("send_email_reply", args))
    assert sent.startswith("Error: confirmation token is unknown")


async def test_tampered_record_is_refused(email_settings, zammad, store):
    async with Client(server.build_server(email_settings, transport=zammad)) as client:
        preview = (await client.call_tool("prepare_email_reply", PREPARED)).data
        backend = store.backends[0]
        ((key, (raw, expires_at)),) = backend._values.items()
        record = json.loads(raw)
        record["payload"]["to"] = ["attacker@evil.test"]
        backend._values = {key: (json.dumps(record), expires_at)}
        sent = (await client.call_tool("send_email_reply", send_args(preview, to=["attacker@evil.test"]))).data
    assert sent == "Error: confirmation record failed its integrity check; prepare the action again"
    assert zammad.calls("POST", "/ticket_articles") == []


async def test_record_holds_the_payload_hash_bound_to_identity_and_action(email_settings, zammad, store):
    (preview,) = await session(email_settings, zammad, ("prepare_email_reply", PREPARED))
    ((key, (raw, _)),) = store.backends[0]._values.items()
    record = json.loads(raw)
    assert (record["identity"], record["action"]) == (token_identity("secret-token"), "email_reply")
    assert record["payload"]["ticket_id"] == 4
    assert preview["confirmation_token"] not in key + raw


@pytest.mark.parametrize(
    "changes",
    [{"to": ["cara@example.com"]}, {"subject": "Re: something else"}, {"to": ["not an address"]}],
)
async def test_echo_mismatch_sends_nothing_and_spends_the_token(email_settings, zammad, changes):
    async with Client(server.build_server(email_settings, transport=zammad)) as client:
        preview = (await client.call_tool("prepare_email_reply", PREPARED)).data
        mismatch = (await client.call_tool("send_email_reply", send_args(preview, **changes))).data
        retry = (await client.call_tool("send_email_reply", send_args(preview))).data
    assert mismatch == (
        "Error: to or subject differ from the prepared reply, so nothing was sent and the confirmation is spent; "
        "prepare the reply again"
    )
    assert retry.startswith("Error: confirmation token is unknown, already used")
    assert zammad.calls("POST", "/ticket_articles") == []


def api_key_call(client: TestClient, token: str, name: str, arguments: dict[str, Any]) -> str:
    result = rpc(
        client, "/http/api-key/mcp", "tools/call", {"name": name, "arguments": arguments}, {"X-Zammad-Token": token}
    )
    return tool_text(result)


async def test_token_issued_to_one_user_is_refused_for_another(api_key_settings, fake):
    zammad = mailbox(fake)
    settings = with_email(api_key_settings)
    app = server.build_http_app(server.build_server(settings, transport=zammad), settings)
    with TestClient(app) as client:
        preview = json.loads(api_key_call(client, "token-of-ada", "prepare_email_reply", PREPARED))
        stolen = api_key_call(client, "token-of-bob", "send_email_reply", send_args(preview))
        owner = api_key_call(client, "token-of-ada", "send_email_reply", send_args(preview))
    assert stolen == "Error: confirmation token does not match this request; prepare the action again"
    assert owner.startswith("Error: confirmation token is unknown, already used")
    assert zammad.calls("POST", "/ticket_articles") == []


# --- tiers ---


@pytest.mark.parametrize("tool", sorted(EMAIL_TOOLS))
async def test_customers_are_refused(email_settings, customer_fake, tool):
    mailbox(customer_fake)
    args = PREPARED if tool == "prepare_email_reply" else STRAY_SEND
    (shaped,) = await session(email_settings, customer_fake, (tool, args))
    assert shaped == "Error: this tool needs a Zammad agent account; your account is a customer"
    assert customer_fake.calls("POST", "/ticket_articles") == []
    assert customer_fake.calls("GET", "/tickets/4") == []


@pytest.mark.parametrize("make_fake", [unknown_role_fake, admin_only_fake])
@pytest.mark.parametrize("tool", sorted(EMAIL_TOOLS))
async def test_unknown_or_ticketless_tiers_are_refused(email_settings, make_fake, tool):
    zammad = mailbox(make_fake())
    args = PREPARED if tool == "prepare_email_reply" else STRAY_SEND
    (shaped,) = await session(email_settings, zammad, (tool, args))
    assert shaped.startswith("Error: this tool needs a Zammad agent account; ")
    assert zammad.calls("POST", "/ticket_articles") == []


async def test_send_without_a_token_is_an_error(email_settings, zammad):
    args = STRAY_SEND
    (shaped,) = await session(dataclasses.replace(email_settings, http_token=None), zammad, ("send_email_reply", args))
    assert shaped == "Error: no Zammad API token is configured for this request"


# --- the send POST ---


async def test_in_flight_failure_is_not_retried(email_settings, zammad):
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    zammad.on("POST", "/ticket_articles", handler=time_out)
    _, sent = await prepare_then_send(email_settings, zammad, PREPARED)
    assert len(zammad.calls("POST", "/ticket_articles")) == 1
    assert sent.startswith("Error: could not reach Zammad (ReadTimeout); the reply may or may not have been created")


async def test_gateway_error_on_send_is_not_retried(email_settings, zammad):
    zammad.on("POST", "/ticket_articles", status=502, json={"error": "bad gateway"})
    _, sent = await prepare_then_send(email_settings, zammad, PREPARED)
    assert len(zammad.calls("POST", "/ticket_articles")) == 1
    assert "may or may not have been created" in sent


async def test_connect_failure_says_nothing_was_sent(email_settings, zammad):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    zammad.on("POST", "/ticket_articles", handler=refuse)
    _, sent = await prepare_then_send(email_settings, zammad, PREPARED)
    assert sent == "Error: could not reach Zammad (ConnectError); the reply was not sent, prepare it again"


async def test_zammad_refusing_the_article_is_reported(email_settings, zammad):
    zammad.on(
        "POST",
        "/ticket_articles",
        status=422,
        json={"error": "This group has no email address configured for outgoing communication."},
    )
    _, sent = await prepare_then_send(email_settings, zammad, PREPARED)
    assert sent == (
        "Error: Zammad rejected the request (HTTP 422): "
        "This group has no email address configured for outgoing communication."
    )


# --- confirmation store failures ---


async def test_store_failure_on_prepare(email_settings, zammad, monkeypatch):
    async def broken(*_args: Any, **_kwargs: Any) -> str:
        raise ConnectionError("valkey down")

    monkeypatch.setattr(Confirmations, "issue", broken)
    (shaped,) = await session(email_settings, zammad, ("prepare_email_reply", PREPARED))
    assert shaped == CONFIRMATION_STORE_DOWN


async def test_store_failure_on_send(email_settings, zammad, monkeypatch):
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("valkey down")

    monkeypatch.setattr(Confirmations, "redeem", broken)
    args = STRAY_SEND
    (shaped,) = await session(email_settings, zammad, ("send_email_reply", args))
    assert shaped == CONFIRMATION_STORE_DOWN
    assert zammad.calls("POST", "/ticket_articles") == []


# --- helpers ---


def test_parse_addresses_keeps_well_formed_addresses_once():
    lines = ["Cara <CARA@example.com>, bob@example.com", None, "cara@example.com; junk", "<evil>@x.test"]
    assert parse_addresses(lines) == ["cara@example.com", "bob@example.com"]


def test_normalize_recipients_deduplicates():
    assert normalize_recipients(["a@example.com", "A <A@EXAMPLE.COM>"]) == ["a@example.com"]
