"""Tolerant views of Zammad objects.

Every model allows extra fields and coerces numbers to strings, so a Zammad
upgrade that adds or retypes an attribute never breaks a tool. Only the
fields the shapers read are declared; with ``expand=true`` Zammad sends
association names (``state``, ``group``, ``owner``) next to the ``*_id``s.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from zammad_mcp.client.errors import UnexpectedResponseError


class ZammadModel(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True, populate_by_name=True)

    @classmethod
    def parse(cls, data: Any) -> Self:
        """Validate a Zammad response, raising :class:`UnexpectedResponseError` instead of pydantic's error."""
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise UnexpectedResponseError(f"Zammad returned an unexpected {cls.__name__} shape") from exc


class Attachment(ZammadModel):
    id: int
    filename: str | None = None
    size: int | None = None
    preferences: dict[str, Any] = Field(default_factory=dict)

    @field_validator("size", mode="before")
    @classmethod
    def _size_as_int(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def content_type(self) -> str:
        for key in ("Content-Type", "Mime-Type", "content_type"):
            if self.preferences.get(key):
                return str(self.preferences[key]).split(";")[0].strip().lower()
        return "application/octet-stream"


class Ticket(ZammadModel):
    id: int
    number: str | None = None
    title: str | None = None
    state: str | None = None
    priority: str | None = None
    group: str | None = None
    owner: str | None = None
    customer: str | None = None
    organization: str | None = None
    customer_id: int | None = None
    owner_id: int | None = None
    organization_id: int | None = None
    group_id: int | None = None
    article_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    pending_time: str | None = None
    close_at: str | None = None


class Article(ZammadModel):
    id: int
    ticket_id: int | None = None
    type: str | None = None
    sender: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    cc: str | None = None
    reply_to: str | None = None
    subject: str | None = None
    body: str | None = None
    content_type: str | None = None
    internal: bool | None = None
    created_by: str | None = None
    created_by_id: int | None = None
    origin_by_id: int | None = None
    created_at: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class User(ZammadModel):
    id: int
    login: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    email: str | None = None
    phone: str | None = None
    organization: str | None = None
    organization_id: int | None = None
    roles: list[str] = Field(default_factory=list)
    role_ids: list[int] = Field(default_factory=list)
    active: bool | None = None
    vip: bool | None = None


class Organization(ZammadModel):
    id: int
    name: str | None = None
    active: bool | None = None
    shared: bool | None = None
    domain: str | None = None
    vip: bool | None = None
    member_ids: list[int] = Field(default_factory=list)


class Role(ZammadModel):
    id: int
    name: str | None = None
    permissions: list[str] = Field(default_factory=list)


class KnowledgeBaseHit(ZammadModel):
    """One ``details`` row of ``POST /knowledge_bases/search``; ``id`` is the answer translation's id."""

    id: int
    type: str | None = None
    url: str | None = None
    title: str | None = None
    body: str | None = None
    date: str | None = None


class KnowledgeBaseAnswer(ZammadModel):
    id: int
    category_id: int | None = None
    translation_ids: list[int] = Field(default_factory=list)
    published_at: str | None = None
    internal_at: str | None = None
    archived_at: str | None = None
    updated_at: str | None = None


class KnowledgeBaseAnswerTranslation(ZammadModel):
    id: int
    answer_id: int | None = None
    title: str | None = None
    content_id: int | None = None
    kb_locale_id: int | None = None


class KnowledgeBaseAnswerContent(ZammadModel):
    id: int
    body: str | None = None


class Group(ZammadModel):
    id: int
    name: str | None = None
    email_address_id: int | None = None


class EmailAddress(ZammadModel):
    id: int
    email: str | None = None
    active: bool | None = None
    channel_id: int | None = None


class Macro(ZammadModel):
    id: int
    name: str | None = None
    active: bool | None = None
    note: str | None = None
    group_ids: list[int] = Field(default_factory=list)
    perform: dict[str, Any] = Field(default_factory=dict)
