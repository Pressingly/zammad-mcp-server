"""Ticket tools: search, read, create, update and history."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.models import Article, Ticket
from zammad_mcp.client.pagination import PageRequest, page_from_response, paginate_locally
from zammad_mcp.client.text import is_ascii_digits
from zammad_mcp.shaping import UNTRUSTED_NOTICE, shape_article, shape_ticket, untrusted_field
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import (
    CREATE,
    OVERWRITE,
    READ,
    ToolContext,
    ZammadSession,
    agent_only_refusal,
    as_dict,
    as_list,
    tier_error,
)

MODULE = "tickets"
EXPAND = {"expand": "true"}
MAX_RECENT_ARTICLES = 20
_FILTER_FIELDS = {
    "state_ids": "ticket.state_id",
    "priority_ids": "ticket.priority_id",
    "group_ids": "ticket.group_id",
    "customer_id": "ticket.customer_id",
    "owner_id": "ticket.owner_id",
    "organization_id": "ticket.organization_id",
}

PageNumber = Annotated[int, Field(ge=1, description="1-based page number")]
PerPage = Annotated[int, Field(ge=1, le=100, description="Results per page, at most 100")]


class TicketReferenceError(ValueError):
    pass


def ticket_condition(filters: dict[str, int | list[int] | None]) -> dict[str, Any]:
    """Build a Zammad selector from the fixed filter set.

    Attribute names and the operator come only from ``_FILTER_FIELDS``: an
    unknown attribute makes Zammad fail with a SQL error (422), so nothing the
    model sends is ever used as a key.
    """
    return {
        _FILTER_FIELDS[name]: {"operator": "is", "value": value if isinstance(value, list) else [value]}
        for name, value in filters.items()
        if value not in (None, [])
    }


async def find_ticket_id(session: ZammadSession, reference: str) -> int:
    """Resolve ``"123"`` (an id) or ``"#20001"`` (a ticket number) to a ticket id.

    Numbers are matched exactly through a search ``condition``: a plain query
    would be a substring match and find ``#200012`` too.
    """
    text = reference.strip()
    if is_ascii_digits(text):
        return int(text)
    number = text.removeprefix("#").strip()
    if not text.startswith("#") or not number:
        raise TicketReferenceError(f"{reference!r} is neither a ticket id nor a #number")
    body = {"condition": {"ticket.number": {"operator": "is", "value": number}}, "limit": 1}
    found = page_from_response(await session.search("/tickets/search", EXPAND, body), PageRequest.of(1, 1)).items
    if not found:
        raise TicketReferenceError(f"no ticket #{number} is visible to this user")
    return Ticket.parse(found[0]).id


def _customer_reference(customer: str) -> int | str:
    text = customer.strip()
    if is_ascii_digits(text):
        return int(text)
    if "@" in text:
        return f"guess:{text}"
    raise TicketReferenceError("customer must be a Zammad user id or an email address")


def _history_value(entry: dict[str, Any], key: str) -> str | None:
    return untrusted_field(entry.get(key), source=f"history_{key}", item_id=_history_id(entry))


def _history_id(entry: dict[str, Any]) -> int | str:
    entry_id = entry.get("id")
    return entry_id if isinstance(entry_id, int) else ""


def _user_name(assets: dict[str, Any], user_id: Any) -> str | None:
    user = (assets.get("User") or {}).get(str(user_id)) or {}
    name = " ".join(part for part in (user.get("firstname"), user.get("lastname")) if part)
    return name or user.get("login")


def shape_history(entry: dict[str, Any], assets: dict[str, Any]) -> dict[str, Any]:
    shaped = {
        "created_at": entry.get("created_at"),
        "created_by": untrusted_field(
            _user_name(assets, entry.get("created_by_id")), source="history_created_by", item_id=_history_id(entry)
        ),
        "type": entry.get("type") or entry.get("history_type"),
        "object": entry.get("object") or entry.get("history_object"),
        "attribute": entry.get("attribute") or entry.get("history_attribute"),
        "from": _history_value(entry, "value_from"),
        "to": _history_value(entry, "value_to"),
    }
    return {key: value for key, value in shaped.items() if value is not None}


def register(mcp: FastMCP, context: ToolContext) -> None:
    links = context.links

    @mcp.tool(**context.tool("Search tickets", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def search_tickets(
        query: Annotated[str, Field(description="Text to look for; empty lists every visible ticket")] = "",
        state_ids: Annotated[list[int] | None, Field(description="Only these state ids")] = None,
        priority_ids: Annotated[list[int] | None, Field(description="Only these priority ids")] = None,
        group_ids: Annotated[list[int] | None, Field(description="Only these group ids")] = None,
        customer_id: Annotated[int | None, Field(description="Only this customer's tickets")] = None,
        owner_id: Annotated[int | None, Field(description="Only tickets owned by this agent")] = None,
        organization_id: Annotated[int | None, Field(description="Only this organization's tickets")] = None,
        sort_by: Literal["updated_at", "created_at", "number"] = "updated_at",
        order_by: Literal["asc", "desc"] = "desc",
        page: PageNumber = 1,
        per_page: PerPage = 25,
    ) -> dict[str, Any] | str:
        """Search the tickets the caller can see, newest activity first by default.

        On a Zammad without Elasticsearch (the default) ``query`` is a plain,
        case-insensitive substring match over the ticket title and number and
        the article subjects, bodies, senders and recipients. There is no field
        syntax (``state:open`` is searched as literal text), no wildcards and no
        ranking, and ``AND``/``OR`` are searched as words. Narrow results with
        the structured filters instead (each is an exact match, all combined
        with AND); take ids from list_ticket_options. Customers only ever see
        their own and their organization's tickets.
        """
        filters = {
            "state_ids": state_ids,
            "priority_ids": priority_ids,
            "group_ids": group_ids,
            "customer_id": customer_id,
            "owner_id": owner_id,
            "organization_id": organization_id,
        }
        paging = PageRequest.of(page, per_page)
        body = {
            "query": query.strip(),
            "condition": ticket_condition(filters),
            "sort_by": sort_by,
            "order_by": order_by,
            **paging.params(),
        }
        try:
            session = await context.session()
            found = await session.search("/tickets/search", {**EXPAND, "with_total_count": "true"}, body)
            result = page_from_response(found, paging).map(lambda row: shape_ticket(Ticket.parse(row), links))
        except ZammadError as error:
            return to_tool_error(error)
        return {**result.to_dict("tickets"), "notice": UNTRUSTED_NOTICE}

    @mcp.tool(**context.tool("Get ticket", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def get_ticket(
        ticket: Annotated[str, Field(description='A ticket id such as "42", or a ticket number such as "#20001"')],
        recent_articles: Annotated[
            int, Field(ge=0, le=MAX_RECENT_ARTICLES, description="How many of the newest articles to include")
        ] = 3,
    ) -> dict[str, Any] | str:
        """Get one ticket with its newest articles (newest first).

        Article bodies are plain text cut to 4000 characters; use
        list_ticket_articles or get_ticket_article for the rest.
        """
        try:
            session = await context.session()
            ticket_id = await find_ticket_id(session, ticket)
            shaped = shape_ticket(Ticket.parse(await session.get(f"/tickets/{ticket_id}", EXPAND)), links)
            if recent_articles:
                rows = as_list(await session.get(f"/ticket_articles/by_ticket/{ticket_id}", EXPAND))
                newest = list(reversed(rows))[:recent_articles]
                shaped["recent_articles"] = [shape_article(Article.parse(row), links) for row in newest]
        except TicketReferenceError as error:
            return f"Error: {error}"
        except ZammadError as error:
            return to_tool_error(error)
        return {**shaped, "notice": UNTRUSTED_NOTICE}

    @mcp.tool(**context.tool("Get ticket history", READ, module=MODULE, tier=Tier.AGENT))
    async def get_ticket_history(
        ticket_id: Annotated[int, Field(ge=1)],
        page: PageNumber = 1,
        per_page: PerPage = 50,
    ) -> dict[str, Any] | str:
        """List a ticket's change history (state, owner, group, title changes...), newest first. Agents only."""
        try:
            session = await context.session()
            if session.lacks(Tier.AGENT):
                return tier_error(Tier.AGENT, session.tier)
            history = as_dict(await session.get(f"/ticket_history/{ticket_id}"))
        except ZammadError as error:
            return to_tool_error(error)
        assets = as_dict(history.get("assets"))
        entries = [entry for entry in reversed(as_list(history.get("history"))) if isinstance(entry, dict)]
        result = paginate_locally(entries, PageRequest.of(page, per_page))
        return result.map(lambda entry: shape_history(entry, assets)).to_dict("history")

    if not context.writes_enabled:
        return

    @mcp.tool(**context.tool("Create ticket", CREATE, module=MODULE, tier=Tier.CUSTOMER))
    async def create_ticket(
        title: Annotated[str, Field(min_length=1, max_length=250)],
        group: Annotated[str, Field(min_length=1, description="Group name, from list_ticket_options")],
        body: Annotated[str, Field(min_length=1, description="The first message, plain text")],
        customer: Annotated[
            str | None,
            Field(description="Agents: the customer's user id or email (an unknown email creates the user)"),
        ] = None,
        state: Annotated[str | None, Field(description="State name; Zammad's default when omitted")] = None,
        priority: Annotated[str | None, Field(description="Priority name such as '2 normal'")] = None,
    ) -> dict[str, Any] | str:
        """Create a ticket with a first public message.

        Agents must name the customer and may set state and priority.
        Customers always create tickets for themselves and pass only title,
        group and body.
        """
        try:
            session = await context.session()
            is_customer = session.tier == Tier.CUSTOMER
            if customer or state or priority:
                refusal = agent_only_refusal(session, "set the customer, state or priority")
                if refusal:
                    return refusal
            elif session.tier == Tier.AGENT:
                return "Error: agents must name the customer (a Zammad user id or email address)"
            payload: dict[str, Any] = {
                "title": title,
                "group": group,
                "article": {
                    "subject": title,
                    "body": body,
                    "type": "web" if is_customer else "note",
                    "internal": False,
                    "content_type": "text/plain",
                },
            }
            if customer:
                payload["customer_id"] = _customer_reference(customer)
            elif session.tier == Tier.AGENT:
                return "Error: agents must name the customer (a Zammad user id or email address)"
            payload.update({key: value for key, value in (("state", state), ("priority", priority)) if value})
            return shape_ticket(Ticket.parse(await session.post("/tickets", payload, EXPAND)), links)
        except TicketReferenceError as error:
            return f"Error: {error}"
        except ZammadError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("Update ticket", OVERWRITE, module=MODULE, tier=Tier.CUSTOMER))
    async def update_ticket(
        ticket_id: Annotated[int, Field(ge=1)],
        title: Annotated[str | None, Field(max_length=250)] = None,
        state: Annotated[str | None, Field(description="State name, from list_ticket_options")] = None,
        priority: Annotated[str | None, Field(description="Priority name")] = None,
        group: Annotated[str | None, Field(description="Group name")] = None,
        owner_id: Annotated[int | None, Field(description="Agent user id to assign; agents only")] = None,
        pending_time: Annotated[
            str | None, Field(description="ISO 8601 time, required by 'pending reminder' and 'pending close'")
        ] = None,
    ) -> dict[str, Any] | str:
        """Change a ticket's fields. Only the fields you pass change; to write a message use add_ticket_note."""
        changes = {
            key: value
            for key, value in (
                ("title", title),
                ("state", state),
                ("priority", priority),
                ("group", group),
                ("owner_id", owner_id),
                ("pending_time", pending_time),
            )
            if value is not None
        }
        if not changes:
            return "Error: pass at least one field to change"
        try:
            session = await context.session()
            refusal = agent_only_refusal(session, "assign an owner") if owner_id is not None else None
            if refusal:
                return refusal
            return shape_ticket(Ticket.parse(await session.put(f"/tickets/{ticket_id}", changes, EXPAND)), links)
        except ZammadError as error:
            return to_tool_error(error)
