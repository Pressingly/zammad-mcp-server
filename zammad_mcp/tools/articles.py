"""Article tools: list and read a ticket's messages, and add a note or reply."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.models import Article
from zammad_mcp.client.pagination import PageRequest, paginate_locally
from zammad_mcp.shaping import MAX_BODY_CHARS, UNTRUSTED_NOTICE, shape_article
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import CREATE, READ, ToolContext

MODULE = "articles"
EXPAND = {"expand": "true"}
MAX_ARTICLE_CHARS = 100_000


def register(mcp: FastMCP, context: ToolContext) -> None:
    links = context.links

    @mcp.tool(**context.tool("List ticket articles", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def list_ticket_articles(
        ticket_id: Annotated[int, Field(ge=1)],
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=50)] = 10,
        max_chars: Annotated[int, Field(ge=100, le=MAX_BODY_CHARS, description="Body length cap per article")] = (
            MAX_BODY_CHARS
        ),
    ) -> dict[str, Any] | str:
        """List a ticket's articles (messages and notes), newest first, with plain-text bodies.

        Customers do not see internal notes.
        """
        try:
            session = await context.session()
            rows = await session.get(f"/ticket_articles/by_ticket/{ticket_id}", EXPAND) or []
        except ZammadError as error:
            return to_tool_error(error)
        result = paginate_locally(list(reversed(rows)), PageRequest.of(page, per_page))
        shaped = result.map(lambda row: shape_article(Article.model_validate(row), links, max_chars=max_chars))
        return {**shaped.to_dict("articles"), "notice": UNTRUSTED_NOTICE}

    @mcp.tool(**context.tool("Get ticket article", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def get_ticket_article(
        article_id: Annotated[int, Field(ge=1)],
        max_chars: Annotated[int, Field(ge=100, le=MAX_ARTICLE_CHARS, description="Body length cap")] = (
            MAX_BODY_CHARS
        ),
    ) -> dict[str, Any] | str:
        """Get one article with its plain-text body and attachment list. Raise max_chars to read a long body."""
        try:
            session = await context.session()
            row = await session.get(f"/ticket_articles/{article_id}", EXPAND)
        except ZammadError as error:
            return to_tool_error(error)
        return {**shape_article(Article.model_validate(row), links, max_chars=max_chars), "notice": UNTRUSTED_NOTICE}

    if not context.writes_enabled:
        return

    @mcp.tool(**context.tool("Add ticket note", CREATE, module=MODULE, tier=Tier.CUSTOMER))
    async def add_ticket_note(
        ticket_id: Annotated[int, Field(ge=1)],
        body: Annotated[str, Field(min_length=1, description="Plain text")],
        internal: Annotated[
            bool | None,
            Field(description="Agents: true (the default) hides the note from the customer; false makes it public"),
        ] = None,
        subject: Annotated[str | None, Field(max_length=250)] = None,
    ) -> dict[str, Any] | str:
        """Add a note to a ticket. This never sends an email.

        Agents add an internal note unless ``internal`` is false. Customers
        always add a public reply that agents see in the ticket.
        """
        try:
            session = await context.session()
            is_customer = session.tier == Tier.CUSTOMER
            if is_customer and internal:
                return "Error: customers can only add public replies; leave internal unset"
            payload: dict[str, Any] = {
                "ticket_id": ticket_id,
                "body": body,
                "content_type": "text/plain",
                "type": "web" if is_customer else "note",
                "internal": False if is_customer else internal is not False,
            }
            if subject:
                payload["subject"] = subject
            created = await session.post("/ticket_articles", payload, EXPAND)
        except ZammadError as error:
            return to_tool_error(error)
        return shape_article(Article.model_validate(created), links)
