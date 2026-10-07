"""Knowledge base: ``search_knowledge_base`` and ``get_kb_answer``.

Both are for customers and up. Zammad lets a customer token search and read
published answers without ``knowledge_base.reader``, so there is no in-body
tier refusal: Zammad decides what each caller sees.

- Agents search with the ``agent`` flavor and also find internal answers.
  Everyone else, including a caller whose tier is unknown, searches with
  ``public``, which only ever returns published answers.
- Reading an answer takes two calls. The answer lists its translations, each
  translation names its content id, and ``include_contents`` takes content
  ids (not answer or translation ids).
- Answer titles, snippets and bodies are written by knowledge base editors
  and returned framed as untrusted content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.errors import UnexpectedResponseError
from zammad_mcp.client.models import (
    KnowledgeBaseAnswer,
    KnowledgeBaseAnswerContent,
    KnowledgeBaseAnswerTranslation,
    KnowledgeBaseHit,
)
from zammad_mcp.shaping import (
    MAX_BODY_CHARS,
    UNTRUSTED_NOTICE,
    body_text,
    frame_untrusted,
    html_to_text,
    truncate,
    untrusted_field,
)
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext, as_dict, as_list

MODULE = "kb"
ANSWER_INDEX = "KnowledgeBase::Answer::Translation"
MAX_ANSWER_CHARS = 100_000
_ANSWER_PATH = re.compile(r"/knowledge_bases/([0-9]+)/answers/([0-9]+)(?:\?|$)")

TRANSLATION_ASSET = "KnowledgeBaseAnswerTranslation"
CONTENT_ASSET = "KnowledgeBaseAnswerTranslationContent"

AnswerAssets = dict[str, Any]


@dataclass(frozen=True)
class AnswerRef:
    knowledge_base_id: int
    answer_id: int


def search_flavor(tier: Tier | None) -> str:
    return "agent" if tier == Tier.AGENT else "public"


def answer_ref(url: str | None) -> AnswerRef | None:
    """Read the knowledge base and answer ids out of an agent-style answer URL, or ``None`` if it has another shape."""
    match = _ANSWER_PATH.search(url or "")
    if match is None:
        return None
    return AnswerRef(knowledge_base_id=int(match.group(1)), answer_id=int(match.group(2)))


def search_body(
    query: str, *, flavor: str, page: int, per_page: int, knowledge_base_id: int | None, locale: str | None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "flavor": flavor,
        "index": ANSWER_INDEX,
        "url_type": "agent",
        "page": page,
        "per_page": per_page,
    }
    if knowledge_base_id is not None:
        body["knowledge_base_id"] = knowledge_base_id
    if locale:
        body["locale"] = locale
    return body


def shape_hit(hit: KnowledgeBaseHit, ref: AnswerRef) -> dict[str, Any]:
    def field(value: str | None, name: str) -> str | None:
        return untrusted_field(html_to_text(value or ""), source=f"kb_answer_{name}", item_id=ref.answer_id)

    shaped = {
        "knowledge_base_id": ref.knowledge_base_id,
        "answer_id": ref.answer_id,
        "title": field(hit.title, "title"),
        "snippet": field(hit.body, "snippet"),
        "updated_at": hit.date,
    }
    return {key: value for key, value in shaped.items() if value is not None}


def shape_hits(found: Any) -> list[dict[str, Any]]:
    shaped = []
    rows = (row for row in as_list(as_dict(found).get("details")) if isinstance(row, dict))
    for row in rows:
        hit = KnowledgeBaseHit.parse(row)
        ref = answer_ref(hit.url)
        if hit.type == ANSWER_INDEX and ref is not None:
            shaped.append(shape_hit(hit, ref))
    return shaped


def asset_rows(assets: AnswerAssets, name: str) -> list[Any]:
    return [row for row in as_dict(assets.get(name)).values() if isinstance(row, dict)]


def find_answer(assets: AnswerAssets, answer_id: int) -> KnowledgeBaseAnswer:
    row = as_dict(assets.get("KnowledgeBaseAnswer")).get(str(answer_id))
    if not isinstance(row, dict):
        raise UnexpectedResponseError("Zammad returned the answer without its details")
    return KnowledgeBaseAnswer.parse(row)


def answer_translations(assets: AnswerAssets, answer_id: int) -> list[KnowledgeBaseAnswerTranslation]:
    translations = [KnowledgeBaseAnswerTranslation.parse(row) for row in asset_rows(assets, TRANSLATION_ASSET)]
    return sorted((item for item in translations if item.answer_id == answer_id), key=lambda item: item.id)


def answer_contents(assets: AnswerAssets) -> dict[int, KnowledgeBaseAnswerContent]:
    contents = (KnowledgeBaseAnswerContent.parse(row) for row in asset_rows(assets, CONTENT_ASSET))
    return {content.id: content for content in contents}


def shape_translation(
    translation: KnowledgeBaseAnswerTranslation,
    contents: dict[int, KnowledgeBaseAnswerContent],
    *,
    answer: AnswerRef,
    max_chars: int,
) -> dict[str, Any]:
    content = contents.get(translation.content_id) if translation.content_id is not None else None
    body = truncate(body_text(content.body if content else None, "text/html"), max_chars)
    shaped: dict[str, Any] = {
        "translation_id": translation.id,
        "kb_locale_id": translation.kb_locale_id,
        "title": untrusted_field(translation.title, source="kb_answer_title", item_id=answer.answer_id),
        "body": frame_untrusted(body.text, source="kb_answer_body", item_id=answer.answer_id),
    }
    if body.truncated:
        shaped["truncated"] = True
        shaped["truncation_hint"] = (
            f"Body cut to {max_chars} of {body.total_chars} characters; call get_kb_answer("
            f"knowledge_base_id={answer.knowledge_base_id}, answer_id={answer.answer_id}, max_chars=...) for more."
        )
    return {key: value for key, value in shaped.items() if value is not None}


def shape_answer(answer: KnowledgeBaseAnswer, translations: list[dict[str, Any]], ref: AnswerRef) -> dict[str, Any]:
    shaped = {
        "knowledge_base_id": ref.knowledge_base_id,
        "answer_id": answer.id,
        "category_id": answer.category_id,
        "published_at": answer.published_at,
        "internal_at": answer.internal_at,
        "archived_at": answer.archived_at,
        "updated_at": answer.updated_at,
        "translations": translations,
    }
    return {key: value for key, value in shaped.items() if value is not None}


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("Search knowledge base", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def search_knowledge_base(
        query: Annotated[str, Field(min_length=1, description="Words to look for in answer titles and bodies")],
        knowledge_base_id: Annotated[int | None, Field(ge=1, description="Only this knowledge base")] = None,
        locale: Annotated[str | None, Field(max_length=20, description="Only this locale, e.g. 'en-us'")] = None,
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any] | str:
        """Search knowledge base answers, newest first, with a short snippet of each.

        Customers only find published answers; agents also find internal ones.
        Read a whole answer with get_kb_answer.
        """
        text = query.strip()
        if not text:
            return "Error: the query must not be blank"
        try:
            session = await context.session()
            body = search_body(
                text,
                flavor=search_flavor(session.tier),
                page=page,
                per_page=per_page,
                knowledge_base_id=knowledge_base_id,
                locale=locale,
            )
            found = await session.search("/knowledge_bases/search", {}, body)
            answers = shape_hits(found)
        except ZammadError as error:
            return to_tool_error(error)
        return {
            "answers": answers,
            "page": page,
            "per_page": per_page,
            "has_more": len(as_list(as_dict(found).get("details"))) >= per_page,
            "notice": UNTRUSTED_NOTICE,
        }

    @mcp.tool(**context.tool("Get knowledge base answer", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def get_kb_answer(
        knowledge_base_id: Annotated[int, Field(ge=1, description="From search_knowledge_base")],
        answer_id: Annotated[int, Field(ge=1, description="From search_knowledge_base")],
        max_chars: Annotated[int, Field(ge=100, le=MAX_ANSWER_CHARS, description="Body length cap")] = MAX_BODY_CHARS,
    ) -> dict[str, Any] | str:
        """Read one knowledge base answer: every translation's title and plain-text body.

        Customers can read published answers only.
        """
        ref = AnswerRef(knowledge_base_id=knowledge_base_id, answer_id=answer_id)
        path = f"/knowledge_bases/{knowledge_base_id}/answers/{answer_id}"
        try:
            session = await context.session()
            listed = as_dict(as_dict(await session.get(path)).get("assets"))
            answer = find_answer(listed, answer_id)
            translations = answer_translations(listed, answer_id)
            content_ids = [str(item.content_id) for item in translations if item.content_id is not None]
            contents: dict[int, KnowledgeBaseAnswerContent] = {}
            if content_ids:
                full = await session.get(path, {"include_contents": ",".join(content_ids)})
                contents = answer_contents(as_dict(as_dict(full).get("assets")))
        except ZammadError as error:
            return to_tool_error(error)
        shaped = [shape_translation(item, contents, answer=ref, max_chars=max_chars) for item in translations]
        return {**shape_answer(answer, shaped, ref), "notice": UNTRUSTED_NOTICE}
