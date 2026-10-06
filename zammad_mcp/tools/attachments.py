"""``get_ticket_attachment``: read a text attachment or view an image one.

Size caps: text up to 200 KB, images up to 2 MB, and nothing over 5 MB is
ever downloaded. Other file types are described, not downloaded.
"""

from __future__ import annotations

import base64
import json
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.models import Article, Attachment
from zammad_mcp.shaping import UNTRUSTED_NOTICE, frame_untrusted, shape_attachment
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext, ZammadSession

MODULE = "attachments"
MAX_TEXT_BYTES = 200 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_TEXT_APPLICATION_TYPES = frozenset(
    {"application/json", "application/xml", "application/x-yaml", "application/yaml", "application/csv"}
)


def is_text(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type in _TEXT_APPLICATION_TYPES
        or content_type.endswith(("+json", "+xml"))
    )


def size_cap(content_type: str) -> int | None:
    if content_type in IMAGE_TYPES:
        return MAX_IMAGE_BYTES
    if is_text(content_type):
        return MAX_TEXT_BYTES
    return None


def _text(payload: Any) -> TextContent:
    if isinstance(payload, str):
        return TextContent(type="text", text=payload)
    return TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))


def _error(message: str) -> ToolResult:
    return ToolResult(content=[_text(message)])


def register(mcp: FastMCP, context: ToolContext) -> None:
    links = context.links

    @mcp.tool(**context.tool("Get ticket attachment", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def get_ticket_attachment(
        ticket_id: Annotated[int, Field(ge=1)],
        article_id: Annotated[int, Field(ge=1)],
        attachment_id: Annotated[int, Field(ge=1, description="From the article's attachments list")],
    ) -> ToolResult:
        """Read one attachment of a ticket article.

        Text files (up to 200 KB) come back as text, PNG/JPEG/GIF/WebP images
        (up to 2 MB) as an image. Other types, and anything larger, are
        described with a link to open them in Zammad.
        """
        try:
            session = await context.session()
            article = Article.parse(await session.get(f"/ticket_articles/{article_id}", {"expand": "true"}))
        except ZammadError as error:
            return _error(to_tool_error(error))
        if article.ticket_id != ticket_id:
            return _error(f"Error: article {article_id} does not belong to ticket {ticket_id}")
        attachment = next((item for item in article.attachments if item.id == attachment_id), None)
        if attachment is None:
            return _error(f"Error: article {article_id} has no attachment {attachment_id}")

        described = {**shape_attachment(attachment), "url": links.article(ticket_id, article_id)}
        cap = size_cap(attachment.content_type)
        if cap is None:
            return ToolResult(content=[_text({**described, "note": "This file type cannot be shown here."})])
        if attachment.size is not None and attachment.size > min(cap, MAX_DOWNLOAD_BYTES):
            return ToolResult(content=[_text({**described, "note": f"Too large to show here (limit {cap} bytes)."})])
        return await _fetch(session, attachment, described, cap=cap, ticket_id=ticket_id, article_id=article_id)


async def _fetch(
    session: ZammadSession,
    attachment: Attachment,
    described: dict[str, Any],
    *,
    cap: int,
    ticket_id: int,
    article_id: int,
) -> ToolResult:
    path = f"/ticket_attachment/{ticket_id}/{article_id}/{attachment.id}"
    try:
        download = await session.download(path, max_bytes=min(cap, MAX_DOWNLOAD_BYTES))
    except ZammadError as error:
        return _error(to_tool_error(error))
    if attachment.content_type in IMAGE_TYPES:
        image = ImageContent(
            type="image", data=base64.b64encode(download.content).decode(), mimeType=attachment.content_type
        )
        return ToolResult(content=[_text(described), image])
    text = download.content.decode("utf-8", errors="replace")
    framed = frame_untrusted(text, source="attachment", item_id=attachment.id)
    return ToolResult(content=[_text({**described, "content": framed, "notice": UNTRUSTED_NOTICE})])
