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
from zammad_mcp.confirmations import MAX_OUTSTANDING_PER_IDENTITY, Confirmations, MemoryConfirmationBackend
from zammad_mcp.credentials.base import token_identity
from zammad_mcp.tools import email as email_tools
from zammad_mcp.tools.email import (
    CONFIRMATION_STORE_DOWN,
    MAX_ATTACHMENT_DATA_CHARS,
    MAX_EMAIL_BODY_CHARS,
    MAX_RECIPIENTS,
    SENT_UNREADABLE,
    decoded_size,
    normalize_recipients,
    parse_addresses,
)

EMAIL_TOOLS = {"prepare_email_reply", "send_email_reply"}
TICKET = {"id": 4, "number": "20004", "title": "Printer on fire", "group_id": 1, "group": "Users", "customer_id": 9}
GROUP = {"id": 1, "name": "Users", "email_address_id": 3}
SENDER = {"id": 3, "email": "Support@Example.com", "active": True, "channel_id": 5}
OTHER_GROUP_SENDER = {"id": 4, "email": "Billing@Example.com", "active": True, "channel_id": 6}
CUSTOMER_ARTICLE = {
    "id": 1,
    "ticket_id": 4,
    "sender": "Customer",
    "type": "email",
    "internal": False,
    "created_by_id": 9,
    "from": "Cara <cara@example.com>",
    "to": "support@example.com",
    "reply_to": "cara-replies@example.com",
    "cc": "Colleague <colleague@example.com>",
}
AGENT_ARTICLE = {
    "id": 2,
    "ticket_id": 4,
    "sender": "Agent",
    "type": "email",
    "internal": False,
    "created_by_id": 7,
    "from": "Support <support@example.com>",
    "to": "cara@example.com",
    "cc": "Boss <boss@example.com>",
}
INTERNAL_NOTE = {
    "id": 3,
    "ticket_id": 4,
    "sender": "Agent",
    "type": "note",
    "internal": True,
    "created_by_id": 7,
    "to": "secret-partner@example.com",
}
OUTSIDER_FOLLOW_UP = {
    "id": 5,
    "ticket_id": 4,
    "sender": "Customer",
    "type": "email",
    "internal": False,
    "created_by_id": 66,
    "from": "Mallory <mallory@evil.test>",
    "to": "support@example.com",
    "reply_to": "drop@evil.test",
    "cc": "accomplice@evil.test",
}
SYSTEM_NOTICE = {
    "id": 6,
    "ticket_id": 4,
    "sender": "System",
    "type": "email",
    "internal": False,
    "to": "notify-list@example.com",
}
ARTICLES = [CUSTOMER_ARTICLE, AGENT_ARTICLE, INTERNAL_NOTE, OUTSIDER_FOLLOW_UP, SYSTEM_NOTICE]
CREATED = {"id": 50, "ticket_id": 4, "type": "email", "sender": "Agent", "internal": False}
PREPARED = {"ticket_id": 4, "subject": "Re: printer", "body": "We are on it. SECRET-BODY-TEXT"}
STRAY_SEND = {"confirmation_token": "t", "to": ["a@b.test"], "subject": "s"}
NOT_A_PARTICIPANT = (
    "is not a participant of this ticket; replies can only go to the ticket's customer, addresses on the "
    "customer's own messages, or addresses agents already wrote to on this ticket"
)


def mailbox(fake: FakeZammad, *, group: dict | None = None, sender: dict | None = None) -> FakeZammad:
    fake.on("GET", "/tickets/4", json=TICKET)
    fake.on("GET", "/groups/1", json=group or GROUP)
    fake.on("GET", "/email_addresses/3", json=sender or SENDER)
    fake.on("GET", "/email_addresses", json=[SENDER, OTHER_GROUP_SENDER, "junk"])
    fake.on("GET", "/ticket_articles/by_ticket/4", json=ARTICLES)
    fake.on("GET", "/users/9", json={"id": 9, "email": "Cara@Example.com"})
    fake.on("POST", "/ticket_articles", status=201, json=CREATED)
    return fake


def with_email(settings: Settings, *, allow_any: bool = False) -> Settings:
    return dataclasses.replace(
        settings,
        enabled_modules=settings.enabled_modules | {"email_replies"},
        email_allow_any_recipient=allow_any,
    )


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


async def prepare(settings: Settings, fake: FakeZammad, **changes: Any) -> Any:
    (result,) = await session(settings, fake, ("prepare_email_reply", {**PREPARED, **changes}))
    return result


def send_args(preview: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "confirmation_token": preview["confirmation_token"],
        "to": preview["to"],
        "subject": preview["subject"],
        **changes,
    }


async def prepare_then_send(settings: Settings, fake: FakeZammad, prepared: dict[str, Any], **send_changes: Any):
    async with Client(server.build_server(settings, transport=fake)) as client:
        preview = (await client.call_tool("prepare_email_reply", prepared)).data
        sent = (await client.call_tool("send_email_reply", send_args(preview, **send_changes))).data
    return preview, sent


def audit_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "zammad_mcp.audit"]


# --- registration ---


async def test_flag_off_by_default_means_the_tools_are_absent(settings, zammad):
    assert not set(await tools(settings, zammad)) & EMAIL_TOOLS
    assert not Settings.from_env({"ZAMMAD_URL": "https://z.test"}).module_enabled("email_replies")


async def test_flag_on_registers_both_tools_with_their_annotations(email_settings, zammad):
    listed = await tools(email_settings, zammad)
    prepare_tool, send_tool = listed["prepare_email_reply"], listed["send_email_reply"]
    hints = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    assert tuple(getattr(prepare_tool.annotations, hint) for hint in hints) == (True, False, False, True)
    assert tuple(getattr(send_tool.annotations, hint) for hint in hints) == (False, True, False, True)
    for tool in (prepare_tool, send_tool):
        assert {"tier:agent", "module:email_replies"} <= set(tool.meta["fastmcp"]["tags"])


async def test_body_and_attachment_data_are_bounded_in_the_schema(email_settings, zammad):
    schema = (await tools(email_settings, zammad))["prepare_email_reply"].inputSchema
    assert schema["properties"]["body"]["maxLength"] == MAX_EMAIL_BODY_CHARS == 1_000_000
    attachment = schema["properties"]["attachments"]["anyOf"][0]["items"]
    assert attachment["properties"]["data"]["maxLength"] == MAX_ATTACHMENT_DATA_CHARS
    assert MAX_ATTACHMENT_DATA_CHARS == 4 * email_tools.MAX_ATTACHMENT_BYTES // 3 + 4


async def test_read_only_mode_drops_the_email_tools(email_settings, zammad):
    assert not set(await tools(dataclasses.replace(email_settings, read_only=True), zammad)) & EMAIL_TOOLS


# --- happy path ---


async def test_prepare_then_send_posts_one_email_article_to_the_customer(email_settings, zammad, caplog):
    caplog.set_level(logging.INFO, logger="zammad_mcp.audit")
    preview, sent = await prepare_then_send(email_settings, zammad, PREPARED)
    assert preview["to"] == ["cara@example.com"]
    assert preview["cc"] == []
    assert preview["recipient_warnings"] == []
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
        "to": "cara@example.com",
        "subject": "Re: printer",
        "body": "We are on it. SECRET-BODY-TEXT",
        "content_type": "text/plain",
    }
    (line,) = audit_lines(caplog)
    assert f"identity={token_identity('secret-token')}" in line
    assert "ticket_id=4 article_id=50 recipients=1 attachments=0" in line
    assert "SECRET-BODY-TEXT" not in line
    assert "secret-token" not in line


async def test_prepare_never_writes_to_zammad(email_settings, zammad):
    await prepare(email_settings, zammad)
    assert zammad.calls("POST", "/ticket_articles") == []
    assert {request.method for request in zammad.requests} == {"GET"}


async def test_explicit_recipients_are_normalized_deduplicated_and_flagged(email_settings, zammad):
    prepared = {**PREPARED, "to": ["Cara <CARA@example.com>"], "cc": ["boss@example.com", "cara@example.com"]}
    preview, sent = await prepare_then_send(email_settings, zammad, prepared)
    assert (preview["to"], preview["cc"]) == (["cara@example.com"], ["boss@example.com"])
    assert preview["recipient_warnings"] == [
        "boss@example.com is not the ticket's customer; make sure the user means to send to it"
    ]
    body = zammad.last_json("POST", "/ticket_articles")
    assert (body["to"], body["cc"]) == ("cara@example.com", "boss@example.com")
    assert sent["status"] == "queued"


async def test_echoed_to_may_differ_in_case_and_order(email_settings, zammad):
    prepared = {**PREPARED, "to": ["cara@example.com", "colleague@example.com"]}
    _, sent = await prepare_then_send(
        email_settings, zammad, prepared, to=["COLLEAGUE@example.com", "Cara <cara@example.com>"]
    )
    assert sent["status"] == "queued"


async def test_attachments_are_previewed_and_posted(email_settings, zammad):
    data = base64.b64encode(b"hello world").decode()
    attachment = {"filename": "note.txt", "data": f"{data[:4]}\n{data[4:]}", "mime_type": "text/plain"}
    preview, _ = await prepare_then_send(email_settings, zammad, {**PREPARED, "attachments": [attachment]})
    assert preview["attachments"] == [{"filename": "note.txt", "mime_type": "text/plain", "size": 11}]
    posted = zammad.last_json("POST", "/ticket_articles")["attachments"]
    assert posted == [{"filename": "note.txt", "data": data, "mime-type": "text/plain"}]


# --- who counts as a participant ---


@pytest.mark.parametrize(
    "address",
    ["cara-replies@example.com", "colleague@example.com", "boss@example.com"],
    ids=["customer-reply-to", "customer-cc", "agent-cc"],
)
async def test_customer_and_agent_articles_add_participants(email_settings, zammad, address):
    preview = await prepare(email_settings, zammad, to=[address])
    assert preview["to"] == [address]
    assert preview["recipient_warnings"] == [
        f"{address} is not the ticket's customer; make sure the user means to send to it"
    ]


@pytest.mark.parametrize(
    "address",
    [
        "mallory@evil.test",
        "drop@evil.test",
        "accomplice@evil.test",
        "secret-partner@example.com",
        "notify-list@example.com",
    ],
    ids=["outsider-from", "outsider-reply-to", "outsider-cc", "internal-note", "system-article"],
)
async def test_outsider_and_internal_articles_add_no_participants(email_settings, zammad, address):
    shaped = await prepare(email_settings, zammad, to=[address])
    assert shaped == f"Error: {address} {NOT_A_PARTICIPANT}"


async def test_outsider_follow_up_does_not_become_the_default_recipient(email_settings, zammad):
    zammad.on("GET", "/ticket_articles/by_ticket/4", json=[AGENT_ARTICLE, OUTSIDER_FOLLOW_UP])
    preview = await prepare(email_settings, zammad)
    assert preview["to"] == ["cara@example.com"]
    assert preview["recipient_warnings"] == []


async def test_outsider_cc_cannot_ride_along(email_settings, zammad):
    shaped = await prepare(email_settings, zammad, cc=["accomplice@evil.test", "drop@evil.test"])
    assert shaped == f"Error: accomplice@evil.test, drop@evil.test {NOT_A_PARTICIPANT.replace('is not', 'are not')}"


@pytest.mark.parametrize(
    "author",
    [{"created_by_id": None, "origin_by_id": 9}, {"created_by_id": None, "from": "cara@example.com"}],
    ids=["origin-by", "from-address"],
)
async def test_customer_article_is_recognised_without_the_author_id(email_settings, zammad, author):
    article = {**CUSTOMER_ARTICLE, "created_by_id": 12, **author}
    zammad.on("GET", "/ticket_articles/by_ticket/4", json=[article])
    assert (await prepare(email_settings, zammad, to=["cara-replies@example.com"]))["to"] == [
        "cara-replies@example.com"
    ]


async def test_customer_named_reply_to_on_another_customer_article_does_not_count(email_settings, zammad):
    impostor = {**CUSTOMER_ARTICLE, "created_by_id": 66, "from": "cara.lookalike@example.org"}
    zammad.on("GET", "/ticket_articles/by_ticket/4", json=[impostor])
    shaped = await prepare(email_settings, zammad, to=["cara-replies@example.com"])
    assert shaped == f"Error: cara-replies@example.com {NOT_A_PARTICIPANT}"


async def test_allow_any_recipient_lifts_the_participant_check(settings, zammad):
    preview, sent = await prepare_then_send(
        with_email(settings, allow_any=True), zammad, {**PREPARED, "to": ["partner@elsewhere.test"]}
    )
    assert preview["to"] == ["partner@elsewhere.test"]
    assert preview["recipient_warnings"] == [
        "partner@elsewhere.test is not the ticket's customer; make sure the user means to send to it"
    ]
    assert sent["status"] == "queued"


# --- Zammad's own addresses ---


@pytest.mark.parametrize("allow_any", [False, True], ids=["participants-only", "allow-any"])
@pytest.mark.parametrize("field", ["to", "cc"])
@pytest.mark.parametrize(
    "address", ["support@example.com", "Billing <BILLING@example.com>"], ids=["own-group", "other-group"]
)
async def test_system_addresses_are_always_refused(settings, zammad, allow_any, field, address):
    extra = {"to": [address]} if field == "to" else {"cc": [address]}
    shaped = await prepare(with_email(settings, allow_any=allow_any), zammad, **extra)
    bare = address.split("<")[-1].rstrip(">").lower()
    assert shaped == f"Error: {bare} is this Zammad's own email address; a reply cannot be sent to it"


async def test_several_system_addresses_are_named_together(settings, zammad):
    shaped = await prepare(
        with_email(settings, allow_any=True), zammad, to=["support@example.com", "billing@example.com"]
    )
    assert shaped == (
        "Error: support@example.com, billing@example.com are this Zammad's own email addresses; "
        "a reply cannot be sent to them"
    )


async def test_customer_whose_email_is_a_system_address_has_no_default(email_settings, zammad):
    zammad.on("GET", "/users/9", json={"id": 9, "email": "billing@example.com"})
    shaped = await prepare(email_settings, zammad)
    assert shaped == "Error: this ticket's customer has no email address to reply to; pass `to` explicitly"


async def test_customer_without_email_has_no_default(email_settings, zammad):
    zammad.on("GET", "/users/9", json={"id": 9, "email": None})
    assert (await prepare(email_settings, zammad)).startswith("Error: this ticket's customer has no email address")


async def test_ticket_without_customer_still_uses_agent_participants(email_settings, zammad):
    zammad.on("GET", "/tickets/4", json={**TICKET, "customer_id": None})
    preview = await prepare(email_settings, zammad, to=["boss@example.com"])
    assert preview["to"] == ["boss@example.com"]
    assert zammad.calls("GET", "/users/9") == []


# --- recipient limits and format ---


async def test_more_than_ten_recipients_in_total_are_refused(settings, zammad):
    to = [f"to{index}@example.com" for index in range(6)]
    cc = [f"cc{index}@example.com" for index in range(5)]
    shaped = await prepare(with_email(settings, allow_any=True), zammad, to=to, cc=cc)
    assert shaped == f"Error: at most {MAX_RECIPIENTS} recipients (to and cc together)"


async def test_more_than_ten_addresses_in_one_field_fail_validation(email_settings, zammad):
    to = [f"to{index}@example.com" for index in range(11)]
    async with Client(server.build_server(email_settings, transport=zammad)) as client:
        with pytest.raises(Exception, match="10"):
            await client.call_tool("prepare_email_reply", {**PREPARED, "to": to})


@pytest.mark.parametrize("value", ["not an address", "a@b.test, c@d.test", "<script>@x.test", "üser@example.com"])
async def test_malformed_recipients_are_refused(email_settings, zammad, value):
    shaped = await prepare(email_settings, zammad, to=[value])
    assert shaped == f"Error: {value!r} is not one plain email address (name@example.com)"


@pytest.mark.parametrize("subject", ["Re: printer\r\nBcc: x@evil.test", "Re:\nprinter", "Re: printer", "a\tb"])
async def test_subject_with_control_characters_is_refused(email_settings, zammad, subject):
    shaped = await prepare(email_settings, zammad, subject=subject)
    assert shaped == "Error: the subject must be one line without control characters"
    assert zammad.calls("GET", "/tickets/4") == []


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
    assert await prepare(email_settings, zammad) == expected
    assert zammad.calls("GET", "/ticket_articles/by_ticket/4") == []


async def test_preflight_needs_a_group(email_settings, zammad):
    zammad.on("GET", "/tickets/4", json={**TICKET, "group_id": None})
    assert await prepare(email_settings, zammad) == (
        "Error: ticket 4 has no group, so Zammad has no address to send from"
    )


@pytest.mark.parametrize(
    "path", ["/tickets/4", "/groups/1", "/email_addresses/3", "/email_addresses", "/ticket_articles/by_ticket/4"]
)
async def test_preflight_zammad_errors_become_error_strings(email_settings, zammad, path):
    zammad.on("GET", path, status=403, json={"error": "Not authorized"})
    assert (await prepare(email_settings, zammad)).startswith("Error: permission denied (HTTP 403: Not authorized)")


async def test_prepare_without_a_token(email_settings, zammad):
    shaped = await prepare(dataclasses.replace(email_settings, http_token=None), zammad)
    assert shaped == "Error: no Zammad API token is configured for this request"


# --- attachments ---


async def test_invalid_base64_is_refused(email_settings, zammad):
    shaped = await prepare(email_settings, zammad, attachments=[{"filename": "x.bin", "data": "not base64!"}])
    assert shaped == "Error: attachment 0 ('x.bin') is not valid base64"


async def test_attachments_over_the_limit_are_refused_before_decoding(email_settings, zammad, monkeypatch):
    monkeypatch.setattr(email_tools, "MAX_ATTACHMENT_BYTES", 10)
    decoded: list[str] = []
    real_decode = base64.b64decode

    def recording_decode(data: str, **kwargs: Any) -> bytes:
        decoded.append(data)
        return real_decode(data, **kwargs)

    monkeypatch.setattr(email_tools.base64, "b64decode", recording_decode)
    first, second = base64.b64encode(b"123456").decode(), base64.b64encode(b"abcdef").decode()
    attachments = [{"filename": "a", "data": first}, {"filename": "b", "data": second}]
    shaped = await prepare(email_settings, zammad, attachments=attachments)
    assert shaped == "Error: attachments add up to more than the 10-byte limit"
    assert decoded == [first]


@pytest.mark.parametrize("size", [0, 1, 2, 3, 10, 11])
def test_decoded_size_matches_base64(size):
    data = base64.b64encode(b"x" * size).decode()
    assert decoded_size(data) == size


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
    (sent,) = await session(email_settings, zammad, ("send_email_reply", {**STRAY_SEND, "confirmation_token": "x"}))
    assert sent.startswith("Error: confirmation token is unknown")


async def test_corrupted_record_is_detected(email_settings, zammad, store):
    async with Client(server.build_server(email_settings, transport=zammad)) as client:
        preview = (await client.call_tool("prepare_email_reply", PREPARED)).data
        backend = store.backends[0]
        ((key, (raw, expires_at)),) = backend._values.items()
        record = json.loads(raw)
        record["payload"]["to"] = ["attacker@evil.test"]
        backend._values = {key: (json.dumps(record), expires_at)}
        sent = (await client.call_tool("send_email_reply", send_args(preview, to=["attacker@evil.test"]))).data
    assert sent == "Error: confirmation record is corrupted; prepare the action again"
    assert zammad.calls("POST", "/ticket_articles") == []


async def test_record_holds_the_payload_hash_bound_to_identity_and_action(email_settings, zammad, store):
    preview = await prepare(email_settings, zammad)
    ((key, (raw, _)),) = store.backends[0]._values.items()
    record = json.loads(raw)
    assert (record["identity"], record["action"]) == (token_identity("secret-token"), "email_reply")
    assert record["payload"]["ticket_id"] == 4
    assert preview["confirmation_token"] not in key + raw


async def test_outstanding_confirmations_are_capped_per_user(email_settings, zammad):
    calls = [("prepare_email_reply", PREPARED)] * (MAX_OUTSTANDING_PER_IDENTITY + 1)
    results = await session(email_settings, zammad, *calls)
    assert all(isinstance(result, dict) for result in results[:-1])
    assert results[-1].startswith(f"Error: you already have {MAX_OUTSTANDING_PER_IDENTITY} actions waiting")


@pytest.mark.parametrize(
    "changes",
    [{"to": ["colleague@example.com"]}, {"subject": "Re: something else"}, {"to": ["not an address"]}],
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
    params = {"name": name, "arguments": arguments}
    return tool_text(rpc(client, "/http/api-key/mcp", "tools/call", params, {"X-Zammad-Token": token}))


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
    no_token = dataclasses.replace(email_settings, http_token=None)
    (shaped,) = await session(no_token, zammad, ("send_email_reply", STRAY_SEND))
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


async def test_zammad_refusing_the_article_is_reported(email_settings, zammad, caplog):
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
    assert audit_lines(caplog) == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, content=b"<html>not json</html>"),
        httpx.Response(201, json=["not", "an", "article"]),
        httpx.Response(201, json={"no": "id"}),
    ],
    ids=["not-json", "list", "no-id"],
)
async def test_accepted_but_unreadable_send_is_audited_as_unknown(email_settings, zammad, caplog, response):
    caplog.set_level(logging.INFO, logger="zammad_mcp.audit")
    zammad.on("POST", "/ticket_articles", handler=lambda _request: response)
    _, sent = await prepare_then_send(email_settings, zammad, PREPARED)
    assert sent == SENT_UNREADABLE
    assert len(zammad.calls("POST", "/ticket_articles")) == 1
    (line,) = audit_lines(caplog)
    assert "ticket_id=4 article_id=unknown recipients=1" in line


# --- confirmation store failures ---


async def test_store_failure_on_prepare(email_settings, zammad, monkeypatch):
    async def broken(*_args: Any, **_kwargs: Any) -> str:
        raise ConnectionError("valkey down")

    monkeypatch.setattr(Confirmations, "issue", broken)
    assert await prepare(email_settings, zammad) == CONFIRMATION_STORE_DOWN


async def test_store_failure_on_send(email_settings, zammad, monkeypatch):
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("valkey down")

    monkeypatch.setattr(Confirmations, "redeem", broken)
    (shaped,) = await session(email_settings, zammad, ("send_email_reply", STRAY_SEND))
    assert shaped == CONFIRMATION_STORE_DOWN
    assert zammad.calls("POST", "/ticket_articles") == []


# --- helpers ---


def test_parse_addresses_keeps_well_formed_addresses_once():
    lines = ["Cara <CARA@example.com>, bob@example.com", None, "", "<evil>@x.test", "dan@example.com"]
    assert parse_addresses(lines) == ["cara@example.com", "bob@example.com", "dan@example.com"]


def test_normalize_recipients_deduplicates():
    assert normalize_recipients(["a@example.com", "A <A@EXAMPLE.COM>"]) == ["a@example.com"]
