"""Email replies: ``prepare_email_reply`` then ``send_email_reply``. Agents only, off by default.

Enabled by ``ZAMMAD_ENABLE_EMAIL_REPLIES=true``. Sending mail is the one
action here that leaves Zammad, so it takes two calls:

1. ``prepare_email_reply`` checks that the ticket's group has an outgoing
   email address with an active channel, settles the recipients and
   attachments, and stores the exact reply behind a confirmation token bound
   to the caller, the ``email_reply`` action, the ticket and the payload hash.
   It returns a preview for the user to approve. Nothing is sent.
2. ``send_email_reply`` spends the token (once, even if a check then fails),
   checks that the echoed ``to`` and ``subject`` match the preview, and posts
   one ``email`` article. Zammad delivers it in the background.

Recipients default to the latest customer article's ``Reply-To`` (else its
``From``). Unless ``ZAMMAD_EMAIL_ALLOW_ANY_RECIPIENT=true`` every recipient
must already be a participant of the ticket: its customer, or an address on
one of its articles. At most 10 recipients and 10 MB of attachments.

The send POST is never retried once it may have reached Zammad, and the
spent token cannot be reused, so a reply is never sent twice.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.errors import ServerError, ZammadTransportError
from zammad_mcp.client.http import PRE_SEND_ERRORS
from zammad_mcp.client.models import Article, EmailAddress, Group, Ticket, User
from zammad_mcp.confirmations import ConfirmationError
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import PREPARE, SEND, ToolContext, ZammadSession, as_list, tier_error

MODULE = "email_replies"
ACTION = "email_reply"
MAX_RECIPIENTS = 10
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
EXPAND = {"expand": "true"}
_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

CONFIRMATION_STORE_DOWN = "Error: the confirmation store is unavailable, so nothing was sent; try again shortly"

logger = logging.getLogger(__name__)
audit = logging.getLogger("zammad_mcp.audit")


class EmailReplyError(Exception):
    """The reply cannot be prepared or sent as asked; the message is shown to the model."""


class EmailAttachment(BaseModel):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    data: Annotated[str, Field(min_length=1, description="The file's bytes, base64-encoded")]
    mime_type: Annotated[str, Field(min_length=1, max_length=100)] = "application/octet-stream"


Recipients = Annotated[list[str], Field(max_length=MAX_RECIPIENTS, description="Email addresses")]


def parse_addresses(values: Iterable[str | None]) -> list[str]:
    """Every well-formed address in ``values`` (``From``/``To``/``Cc`` style lines), lower-cased, first seen first.

    Lines are parsed one at a time: ``getaddresses`` gives up on its whole
    input when any part of it is malformed.
    """
    found = (address.strip().lower() for value in values if value for _, address in getaddresses([value]))
    return list(dict.fromkeys(address for address in found if _ADDRESS.fullmatch(address)))


def normalize_recipients(values: list[str]) -> list[str]:
    """Bare, lower-cased, de-duplicated addresses; refuse anything that is not exactly one plain address."""
    normalized = []
    for value in values:
        parsed = parse_addresses([value])
        if len(parsed) != 1:
            raise EmailReplyError(f"{value!r} is not one plain email address (name@example.com)")
        normalized.append(parsed[0])
    return list(dict.fromkeys(normalized))


def participants(articles: list[Article], customer_email: str | None) -> frozenset[str]:
    lines = [customer_email]
    for article in articles:
        lines.extend((article.from_, article.to, article.cc, article.reply_to))
    return frozenset(parse_addresses(lines))


def default_recipients(articles: list[Article], own_address: str) -> list[str]:
    """``Reply-To`` (else ``From``) of the newest customer article, never the group's own address."""
    customer_articles = [article for article in articles if article.sender == "Customer"]
    if not customer_articles:
        return []
    latest = max(customer_articles, key=lambda article: article.id)
    return [address for address in parse_addresses([latest.reply_to or latest.from_]) if address != own_address]


def check_recipients(recipients: list[str], allowed: frozenset[str], *, allow_any: bool) -> None:
    if len(recipients) > MAX_RECIPIENTS:
        raise EmailReplyError(f"at most {MAX_RECIPIENTS} recipients (to and cc together)")
    if allow_any:
        return
    outside = [address for address in recipients if address not in allowed]
    if outside:
        raise EmailReplyError(
            f"{', '.join(outside)} {'is' if len(outside) == 1 else 'are'} not a participant of this ticket; replies "
            "can only go to the ticket's customer or an address already on one of its articles"
        )


def encode_attachments(attachments: list[EmailAttachment]) -> list[dict[str, str]]:
    """Validate the base64 and the 10 MB total, and return Zammad's article attachment shape."""
    encoded = []
    total = 0
    for index, attachment in enumerate(attachments):
        data = "".join(attachment.data.split())
        try:
            total += len(base64.b64decode(data, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise EmailReplyError(f"attachment {index} ({attachment.filename!r}) is not valid base64") from exc
        encoded.append({"filename": attachment.filename.strip(), "data": data, "mime-type": attachment.mime_type})
    if total > MAX_ATTACHMENT_BYTES:
        raise EmailReplyError(f"attachments total {total} bytes, over the {MAX_ATTACHMENT_BYTES}-byte limit")
    return encoded


def describe_attachment(attachment: dict[str, str]) -> dict[str, Any]:
    return {
        "filename": attachment["filename"],
        "mime_type": attachment["mime-type"],
        "size": len(base64.b64decode(attachment["data"])),
    }


@dataclass(frozen=True)
class Mailbox:
    """The ticket an email reply goes out on, and the group address Zammad sends it from."""

    ticket: Ticket
    address: str


async def preflight(session: ZammadSession, ticket_id: int) -> Mailbox:
    ticket = Ticket.parse(await session.get(f"/tickets/{ticket_id}", EXPAND))
    if ticket.group_id is None:
        raise EmailReplyError(f"ticket {ticket_id} has no group, so Zammad has no address to send from")
    group = Group.parse(await session.get(f"/groups/{ticket.group_id}"))
    if group.email_address_id is None:
        raise EmailReplyError(
            f"the ticket's group {group.name or group.id!s} has no outgoing email address; "
            "a Zammad admin must set one (Admin, Groups) before agents can reply by email"
        )
    address = EmailAddress.parse(await session.get(f"/email_addresses/{group.email_address_id}"))
    if not address.active or address.channel_id is None:
        raise EmailReplyError(
            f"the group's email address {address.email or address.id!s} has no active email channel; "
            "a Zammad admin must set up outgoing email (Admin, Channels, Email)"
        )
    return Mailbox(ticket=ticket, address=(address.email or "").strip().lower())


async def ticket_articles(session: ZammadSession, ticket_id: int) -> list[Article]:
    rows = as_list(await session.get(f"/ticket_articles/by_ticket/{ticket_id}", EXPAND))
    return [Article.parse(row) for row in rows]


async def customer_email(session: ZammadSession, ticket: Ticket) -> str | None:
    if ticket.customer_id is None:
        return None
    return User.parse(await session.get(f"/users/{ticket.customer_id}")).email


def reply_article(payload: dict[str, Any]) -> dict[str, Any]:
    article: dict[str, Any] = {
        "ticket_id": payload["ticket_id"],
        "type": "email",
        "sender": "Agent",
        "internal": False,
        "to": ", ".join(payload["to"]),
        "subject": payload["subject"],
        "body": payload["body"],
        "content_type": "text/plain",
    }
    if payload["cc"]:
        article["cc"] = ", ".join(payload["cc"])
    if payload["attachments"]:
        article["attachments"] = payload["attachments"]
    return article


def echo_matches(payload: dict[str, Any], to: list[str], subject: str) -> bool:
    try:
        echoed = normalize_recipients(to)
    except EmailReplyError:
        return False
    return sorted(echoed) == sorted(payload["to"]) and subject.strip() == payload["subject"]


def send_failure(error: ZammadError) -> str:
    """Tell a reply that surely failed from one that may have reached Zammad."""
    if isinstance(error, ZammadTransportError) and isinstance(error.__cause__, PRE_SEND_ERRORS):
        return f"{to_tool_error(error)}; the reply was not sent, prepare it again"
    if isinstance(error, ZammadTransportError | ServerError):
        return (
            f"{to_tool_error(error)}; the reply may or may not have been created. Check the ticket's articles "
            "before preparing it again, so the customer does not get it twice"
        )
    return to_tool_error(error)


def register(mcp: FastMCP, context: ToolContext) -> None:
    if not context.writes_enabled:
        return
    allow_any = context.email_allow_any_recipient
    confirmations = context.confirmations

    @mcp.tool(**context.tool("Prepare email reply", PREPARE, module=MODULE, tier=Tier.AGENT))
    async def prepare_email_reply(
        ticket_id: Annotated[int, Field(ge=1)],
        subject: Annotated[str, Field(min_length=1, max_length=250)],
        body: Annotated[str, Field(min_length=1, description="Plain text")],
        to: Annotated[
            Recipients | None, Field(description="Defaults to the latest customer message's Reply-To or From")
        ] = None,
        cc: Recipients | None = None,
        attachments: Annotated[
            list[EmailAttachment] | None, Field(description="Base64 files, 10 MB in total at most")
        ] = None,
    ) -> dict[str, Any] | str:
        """Prepare an email reply on a ticket and return a preview with a confirmation token. Nothing is sent.

        Show the preview to the user. Only once they approve it, call
        send_email_reply with the token and the same ``to`` and ``subject``.
        Recipients must already be participants of the ticket.
        """
        try:
            session = await context.session()
            if session.lacks(Tier.AGENT):
                return tier_error(Tier.AGENT, session.tier)
            mailbox = await preflight(session, ticket_id)
            articles = await ticket_articles(session, ticket_id)
            allowed = participants(articles, await customer_email(session, mailbox.ticket)) - {mailbox.address}
            recipients = normalize_recipients(to) if to else default_recipients(articles, mailbox.address)
            if not recipients:
                return "Error: this ticket has no customer email to reply to; pass `to` explicitly"
            copies = [address for address in normalize_recipients(cc or []) if address not in recipients]
            check_recipients(recipients + copies, allowed, allow_any=allow_any)
            payload = {
                "ticket_id": ticket_id,
                "to": recipients,
                "cc": copies,
                "subject": subject.strip(),
                "body": body,
                "attachments": encode_attachments(attachments or []),
            }
        except EmailReplyError as error:
            return f"Error: {error}"
        except ZammadError as error:
            return to_tool_error(error)
        try:
            token = await confirmations.issue(
                identity=session.credential.identity, action=ACTION, payload=payload, keep_payload=True
            )
        except Exception:
            logger.warning("could not store an email reply confirmation", exc_info=True)
            return CONFIRMATION_STORE_DOWN
        return {
            "confirmation_token": token,
            "expires_in_seconds": confirmations.ttl_seconds,
            "ticket_id": ticket_id,
            "from": mailbox.address,
            "to": payload["to"],
            "cc": payload["cc"],
            "subject": payload["subject"],
            "body": body,
            "attachments": [describe_attachment(item) for item in payload["attachments"]],
            "url": context.links.ticket(ticket_id),
            "next_step": (
                "Show this preview to the user. Only after they approve it, call send_email_reply with "
                "confirmation_token and exactly these to and subject values."
            ),
        }

    @mcp.tool(**context.tool("Send email reply", SEND, module=MODULE, tier=Tier.AGENT))
    async def send_email_reply(
        confirmation_token: Annotated[str, Field(min_length=1, max_length=200)],
        to: Annotated[Recipients, Field(min_length=1, description="The preview's to, unchanged")],
        subject: Annotated[str, Field(min_length=1, max_length=250, description="The preview's subject, unchanged")],
    ) -> dict[str, Any] | str:
        """Send an email reply the user approved, from prepare_email_reply's preview. The token works once.

        Zammad queues the email and delivers it in the background.
        """
        try:
            session = await context.session()
        except ZammadError as error:
            return to_tool_error(error)
        if session.lacks(Tier.AGENT):
            return tier_error(Tier.AGENT, session.tier)
        identity = session.credential.identity
        try:
            payload = await confirmations.redeem(confirmation_token, identity=identity, action=ACTION)
        except ConfirmationError as error:
            return f"Error: {error}"
        except Exception:
            logger.warning("could not read an email reply confirmation", exc_info=True)
            return CONFIRMATION_STORE_DOWN
        if not echo_matches(payload, to, subject):
            return (
                "Error: to or subject differ from the prepared reply, so nothing was sent and the confirmation is "
                "spent; prepare the reply again"
            )
        try:
            created = Article.parse(await session.post("/ticket_articles", reply_article(payload)))
        except ZammadError as error:
            return send_failure(error)
        audit.warning(
            "email_reply queued identity=%s ticket_id=%s article_id=%s recipients=%d attachments=%d",
            identity,
            payload["ticket_id"],
            created.id,
            len(payload["to"]) + len(payload["cc"]),
            len(payload["attachments"]),
        )
        return {
            "article_id": created.id,
            "ticket_id": payload["ticket_id"],
            "status": "queued",
            "note": "Zammad sends the email in the background; a delivery failure shows up on the ticket.",
            "url": context.links.article(payload["ticket_id"], created.id),
        }
