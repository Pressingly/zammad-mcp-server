from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest
from fastmcp import Client

from tests.conftest import CUSTOMER_ME, FakeZammad, admin_only_fake, unknown_role_fake
from tests.helpers import data, tools
from zammad_mcp import server
from zammad_mcp.client.errors import error_detail
from zammad_mcp.config import Settings
from zammad_mcp.confirmations import MAX_OUTSTANDING_PER_IDENTITY, Confirmations
from zammad_mcp.tools.macros import CONFIRMATION_STORE_DOWN, macro_payload

MACRO_TOOLS = {"list_macros", "prepare_apply_macro", "apply_macro"}
CLOSE_SPAM = {
    "id": 1,
    "name": "Close & Tag as Spam",
    "active": True,
    "note": "",
    "group_ids": [],
    "perform": {"ticket.state_id": {"value": "4"}, "ticket.tags": {"operator": "add", "value": "spam"}},
}
RETIRED = {"id": 2, "name": "Old flow", "active": False, "group_ids": [3]}
PREPARE = {"macro_id": 1, "ticket_ids": [6, 5, 6]}
STRAY_APPLY = {"confirmation_token": "t", "macro_id": 1, "ticket_ids": [5]}


def with_macros(settings: Settings) -> Settings:
    return dataclasses.replace(settings, enabled_modules=settings.enabled_modules | {"macros"})


@pytest.fixture
def macro_settings(settings) -> Settings:
    return with_macros(settings)


def macros(fake: FakeZammad) -> FakeZammad:
    fake.on("GET", "/macros", json=[CLOSE_SPAM, RETIRED, "junk"])
    fake.on("GET", "/macros/1", json=CLOSE_SPAM)
    fake.on("GET", "/macros/2", json=RETIRED)
    fake.on("POST", "/tickets/mass_macro", json={"ticket_ids": [5, 6], "assets": {}})
    return fake


@pytest.fixture
def zammad(fake) -> FakeZammad:
    return macros(fake)


async def prepare_then_apply(settings: Settings, fake: FakeZammad, prepare: dict[str, Any], **apply_changes: Any):
    async with Client(server.build_server(settings, transport=fake)) as client:
        preview = (await client.call_tool("prepare_apply_macro", prepare)).data
        if isinstance(preview, str):
            return preview, None
        apply = {
            "confirmation_token": preview["confirmation_token"],
            "macro_id": prepare["macro_id"],
            "ticket_ids": prepare["ticket_ids"],
            **apply_changes,
        }
        return preview, (await client.call_tool("apply_macro", apply)).data


# --- registration ---


async def test_macros_are_off_by_default(settings, zammad):
    assert not set(await tools(settings, zammad)) & MACRO_TOOLS
    assert not Settings.from_env({"ZAMMAD_URL": "https://z.test"}).module_enabled("macros")
    assert Settings.from_env({"ZAMMAD_URL": "https://z.test", "ZAMMAD_ENABLE_MACROS": "on"}).module_enabled("macros")


async def test_macro_tools_carry_their_annotations(macro_settings, zammad):
    listed = await tools(macro_settings, zammad)
    hints = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    expected = {
        "list_macros": (True, False, True, True),
        "prepare_apply_macro": (True, False, False, True),
        "apply_macro": (False, True, False, True),
    }
    for name, values in expected.items():
        assert tuple(getattr(listed[name].annotations, hint) for hint in hints) == values, name
        assert {"tier:agent", "module:macros"} <= set(listed[name].meta["fastmcp"]["tags"])


async def test_read_only_keeps_only_list_macros(macro_settings, zammad):
    names = set(await tools(dataclasses.replace(macro_settings, read_only=True), zammad))
    assert names & MACRO_TOOLS == {"list_macros"}


# --- list_macros ---


async def test_list_macros_returns_active_ones_only(macro_settings, zammad):
    shaped = await data(macro_settings, zammad, "list_macros")
    assert shaped == {"macros": [{"id": 1, "name": "Close & Tag as Spam"}]}


async def test_list_macros_error(macro_settings, zammad):
    zammad.on("GET", "/macros", status=403, json={"error": "Token authorization failed."})
    shaped = await data(macro_settings, zammad, "list_macros")
    assert shaped.startswith("Error: permission denied (HTTP 403: Token authorization failed.)")


# --- prepare and apply ---


async def test_prepare_then_apply_runs_mass_macro_once(macro_settings, zammad):
    preview, applied = await prepare_then_apply(macro_settings, zammad, PREPARE)
    assert preview["ticket_ids"] == [5, 6]
    assert preview["macro"] == {"id": 1, "name": "Close & Tag as Spam", "changes": CLOSE_SPAM["perform"]}
    assert preview["expires_in_seconds"] == 600
    assert "apply_macro" in preview["next_step"]
    assert applied == {
        "macro_id": 1,
        "ticket_ids": [5, 6],
        "status": "applied",
        "urls": ["https://zammad.test/#ticket/zoom/5", "https://zammad.test/#ticket/zoom/6"],
    }
    assert zammad.last_json("POST", "/tickets/mass_macro") == {"macro_id": 1, "ticket_ids": [5, 6]}
    assert len(zammad.calls("POST", "/tickets/mass_macro")) == 1


async def test_prepare_changes_nothing(macro_settings, zammad):
    await data(macro_settings, zammad, "prepare_apply_macro", PREPARE)
    assert zammad.calls("POST", "/tickets/mass_macro") == []
    assert all(request.method == "GET" for request in zammad.requests)


async def test_ticket_order_and_duplicates_do_not_matter_when_applying(macro_settings, zammad):
    _, applied = await prepare_then_apply(macro_settings, zammad, PREPARE, ticket_ids=[6, 5])
    assert applied["status"] == "applied"


@pytest.mark.parametrize("changes", [{"macro_id": 2}, {"ticket_ids": [5, 6, 7]}, {"ticket_ids": [5]}])
async def test_apply_must_echo_the_prepared_macro_and_tickets(macro_settings, zammad, changes):
    _, applied = await prepare_then_apply(macro_settings, zammad, PREPARE, **changes)
    assert applied == "Error: confirmation token does not match this request; prepare the action again"
    assert zammad.calls("POST", "/tickets/mass_macro") == []


async def test_apply_token_is_single_use(macro_settings, zammad):
    async with Client(server.build_server(macro_settings, transport=zammad)) as client:
        preview = (await client.call_tool("prepare_apply_macro", PREPARE)).data
        apply = {"confirmation_token": preview["confirmation_token"], **PREPARE}
        first = (await client.call_tool("apply_macro", apply)).data
        second = (await client.call_tool("apply_macro", apply)).data
    assert first["status"] == "applied"
    assert second.startswith("Error: confirmation token is unknown, already used or expired")
    assert len(zammad.calls("POST", "/tickets/mass_macro")) == 1


async def test_inactive_macro_is_refused_at_prepare(macro_settings, zammad):
    shaped = await data(macro_settings, zammad, "prepare_apply_macro", {"macro_id": 2, "ticket_ids": [5]})
    assert shaped == "Error: macro 2 (Old flow) is inactive; only active macros can be applied"


async def test_macro_deactivated_after_prepare_is_refused_at_apply(macro_settings, zammad):
    async with Client(server.build_server(macro_settings, transport=zammad)) as client:
        preview = (await client.call_tool("prepare_apply_macro", PREPARE)).data
        zammad.on("GET", "/macros/1", json={**CLOSE_SPAM, "active": False})
        apply = {"confirmation_token": preview["confirmation_token"], **PREPARE}
        applied = (await client.call_tool("apply_macro", apply)).data
    assert applied.startswith("Error: macro 1 (Close & Tag as Spam) is inactive")
    assert zammad.calls("POST", "/tickets/mass_macro") == []


async def test_unknown_macro_at_prepare(macro_settings, zammad):
    shaped = await data(macro_settings, zammad, "prepare_apply_macro", {"macro_id": 9, "ticket_ids": [5]})
    assert shaped == "Error: not found (HTTP 404), or not visible to this user"


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"error": True, "ticket_id": 6}, "(tickets: 6)"),
        ({"error": "Macro group restrictions do not cover all tickets", "blocking_tickets": [5, 6]}, "(tickets: 5, 6)"),
    ],
)
async def test_mass_macro_refusal_names_the_tickets(macro_settings, zammad, body, detail):
    zammad.on("POST", "/tickets/mass_macro", status=422, json=body)
    _, applied = await prepare_then_apply(macro_settings, zammad, PREPARE)
    assert applied.startswith("Error: Zammad refused to apply the macro and changed nothing (HTTP 422: ")
    assert detail in applied
    assert "True" not in applied


async def test_mass_macro_server_error_is_not_retried_and_may_have_applied(macro_settings, zammad):
    zammad.on("POST", "/tickets/mass_macro", status=503, json={"error": "busy"})
    _, applied = await prepare_then_apply(macro_settings, zammad, PREPARE)
    assert applied == (
        "Error: Zammad had a server error (HTTP 503); try again later; the macro may or may not have been "
        "applied. Check the tickets before preparing it again"
    )
    assert len(zammad.calls("POST", "/tickets/mass_macro")) == 1


async def test_mass_macro_in_flight_failure_may_have_applied(macro_settings, zammad):
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    zammad.on("POST", "/tickets/mass_macro", handler=time_out)
    _, applied = await prepare_then_apply(macro_settings, zammad, PREPARE)
    assert applied.startswith("Error: could not reach Zammad (ReadTimeout); the macro may or may not have been")
    assert len(zammad.calls("POST", "/tickets/mass_macro")) == 1


async def test_mass_macro_connect_failure_changed_nothing(macro_settings, zammad):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    zammad.on("POST", "/tickets/mass_macro", handler=refuse)
    _, applied = await prepare_then_apply(macro_settings, zammad, PREPARE)
    assert applied == "Error: could not reach Zammad (ConnectError); nothing was changed, prepare the macro again"


async def test_mass_macro_accepted_but_unreadable(macro_settings, zammad):
    zammad.on("POST", "/tickets/mass_macro", handler=lambda _request: httpx.Response(200, content=b"<html>"))
    _, applied = await prepare_then_apply(macro_settings, zammad, PREPARE)
    assert applied == (
        "Error: Zammad accepted the macro, so it was applied, but its answer could not be read; "
        "check the tickets before preparing it again"
    )


async def test_mass_macro_other_errors_pass_through(macro_settings, zammad):
    zammad.on("POST", "/tickets/mass_macro", status=403, json={"error": "Not authorized"})
    _, applied = await prepare_then_apply(macro_settings, zammad, PREPARE)
    assert applied.startswith("Error: permission denied (HTTP 403: Not authorized)")


async def test_prepare_refuses_past_the_per_user_confirmation_cap(macro_settings, zammad):
    async with Client(server.build_server(macro_settings, transport=zammad)) as client:
        results = [
            (await client.call_tool("prepare_apply_macro", PREPARE)).data
            for _ in range(MAX_OUTSTANDING_PER_IDENTITY + 1)
        ]
    assert results[-1].startswith(f"Error: you already have {MAX_OUTSTANDING_PER_IDENTITY} actions waiting")


async def test_ticket_list_is_bounded(macro_settings, zammad):
    async with Client(server.build_server(macro_settings, transport=zammad)) as client:
        with pytest.raises(Exception, match="50"):
            await client.call_tool("prepare_apply_macro", {"macro_id": 1, "ticket_ids": list(range(1, 52))})


# --- tiers and failures ---


def customer() -> FakeZammad:
    return FakeZammad(me=CUSTOMER_ME)


@pytest.mark.parametrize("make_fake", [customer, unknown_role_fake, admin_only_fake])
@pytest.mark.parametrize(
    ("tool", "args"), [("list_macros", {}), ("prepare_apply_macro", PREPARE), ("apply_macro", STRAY_APPLY)]
)
async def test_non_agents_are_refused_before_any_macro_call(macro_settings, make_fake, tool, args):
    zammad = macros(make_fake())
    shaped = await data(macro_settings, zammad, tool, args)
    assert shaped.startswith("Error: this tool needs a Zammad agent account; ")
    assert not [request for request in zammad.requests if "macro" in request.url.path]


@pytest.mark.parametrize(
    ("tool", "args"), [("list_macros", {}), ("prepare_apply_macro", PREPARE), ("apply_macro", STRAY_APPLY)]
)
async def test_macro_tools_without_a_token(macro_settings, zammad, tool, args):
    shaped = await data(dataclasses.replace(macro_settings, http_token=None), zammad, tool, args)
    assert shaped == "Error: no Zammad API token is configured for this request"


async def test_store_failures(macro_settings, zammad, monkeypatch):
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("valkey down")

    monkeypatch.setattr(Confirmations, "issue", broken)
    monkeypatch.setattr(Confirmations, "consume", broken)
    assert await data(macro_settings, zammad, "prepare_apply_macro", PREPARE) == CONFIRMATION_STORE_DOWN
    assert await data(macro_settings, zammad, "apply_macro", STRAY_APPLY) == CONFIRMATION_STORE_DOWN
    assert zammad.calls("POST", "/tickets/mass_macro") == []


def test_macro_payload_is_canonical():
    assert macro_payload(1, [6, 5, 6]) == macro_payload(1, [5, 6]) == {"macro_id": 1, "ticket_ids": [5, 6]}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": True, "ticket_id": 2}, "Unprocessable Entity (tickets: 2)"),
        ({"error": "Macro blocked", "blocking_tickets": [3, "x", True]}, "Macro blocked (tickets: 3)"),
        ({"error_human": "Readable", "error": "raw"}, "Readable"),
        ({"error": {"nested": 1}}, "{'nested': 1}"),
        ({"error": False}, "Unprocessable Entity"),
    ],
)
def test_error_detail_reads_bulk_failures(body, expected):
    response = httpx.Response(422, json=body)
    assert error_detail(response) == expected
