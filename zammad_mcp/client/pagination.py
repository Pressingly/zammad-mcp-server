"""Paging over Zammad list and search endpoints.

Zammad pages with ``page`` (1-based) and ``per_page``. Search endpoints
called with ``with_total_count=true`` answer ``{"records": [...],
"total_count": N}``; plain list endpoints answer a bare array with no count,
so ``has_more`` falls back to "this page came back full".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 25


@dataclass(frozen=True)
class PageRequest:
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @classmethod
    def of(cls, page: int | None = None, per_page: int | None = None) -> PageRequest:
        return cls(
            page=max(1, page or 1),
            per_page=min(MAX_PER_PAGE, max(1, per_page or DEFAULT_PER_PAGE)),
        )

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    def params(self) -> dict[str, int]:
        return {"page": self.page, "per_page": self.per_page}


@dataclass(frozen=True)
class Page:
    items: list[Any]
    page: int
    per_page: int
    has_more: bool
    total: int | None = None

    def to_dict(self, key: str = "items") -> dict[str, Any]:
        shaped: dict[str, Any] = {
            key: self.items,
            "page": self.page,
            "per_page": self.per_page,
            "has_more": self.has_more,
        }
        if self.total is not None:
            shaped["total"] = self.total
        return shaped

    def map(self, transform: Callable[[Any], Any]) -> Page:
        return Page([transform(item) for item in self.items], self.page, self.per_page, self.has_more, self.total)


def _has_more(request: PageRequest, returned: int, total: int | None) -> bool:
    if total is not None:
        return request.page * request.per_page < total
    return returned >= request.per_page


def page_from_response(body: Any, request: PageRequest) -> Page:
    """Build a :class:`Page` from either a ``{records, total_count}`` body or a bare list."""
    if isinstance(body, dict):
        records = list(body.get("records") or [])
        total = body.get("total_count")
        total = int(total) if isinstance(total, int | str) and str(total).isdigit() else None
    else:
        records = list(body or [])
        total = None
    return Page(records, request.page, request.per_page, _has_more(request, len(records), total), total)


def paginate_locally(items: list[Any], request: PageRequest) -> Page:
    """Page a list Zammad returns in full (articles, history) on the client side."""
    window = items[request.offset : request.offset + request.per_page]
    return Page(window, request.page, request.per_page, request.offset + len(window) < len(items), len(items))
