from __future__ import annotations

import pytest

from zammad_mcp.client.errors import UnexpectedResponseError
from zammad_mcp.client.models import Article, Attachment, Ticket
from zammad_mcp.client.pagination import MAX_PER_PAGE, PageRequest, page_from_response, paginate_locally


@pytest.mark.parametrize(
    ("page", "per_page", "expected"),
    [(None, None, (1, 25)), (0, 0, (1, 25)), (-3, 500, (1, MAX_PER_PAGE)), (4, 10, (4, 10))],
)
def test_page_request_clamps(page, per_page, expected):
    request = PageRequest.of(page, per_page)
    assert (request.page, request.per_page) == expected
    assert request.params() == {"page": expected[0], "per_page": expected[1]}


def test_page_with_total_count():
    page = page_from_response({"records": [{"id": 1}, {"id": 2}], "total_count": 5}, PageRequest.of(2, 2))
    assert page.to_dict("tickets") == {
        "tickets": [{"id": 1}, {"id": 2}],
        "page": 2,
        "per_page": 2,
        "has_more": True,
        "total": 5,
    }


def test_non_ascii_digit_total_is_ignored():
    assert page_from_response({"records": [], "total_count": "\u00b2"}, PageRequest.of()).total is None


@pytest.mark.parametrize("body", ["text", {"records": "x"}, 5])
def test_non_list_bodies_give_an_empty_page(body):
    assert page_from_response(body, PageRequest.of()).items == []


def test_parse_raises_unexpected_response_error():
    with pytest.raises(UnexpectedResponseError, match="unexpected Ticket shape"):
        Ticket.parse(None)


def test_last_page_with_total_has_no_more():
    page = page_from_response({"records": [{"id": 5}], "total_count": "5"}, PageRequest.of(3, 2))
    assert page.has_more is False
    assert page.total == 5


@pytest.mark.parametrize(("rows", "has_more"), [([{"id": 1}, {"id": 2}], True), ([{"id": 1}], False), (None, False)])
def test_bare_list_infers_has_more_from_a_full_page(rows, has_more):
    page = page_from_response(rows, PageRequest.of(1, 2))
    assert page.has_more is has_more
    assert page.total is None
    assert "total" not in page.to_dict()


def test_paginate_locally():
    page = paginate_locally(list(range(7)), PageRequest.of(2, 3))
    assert page.items == [3, 4, 5]
    assert page.has_more is True
    assert page.total == 7
    assert paginate_locally(list(range(7)), PageRequest.of(3, 3)).has_more is False


def test_models_tolerate_extra_and_retyped_fields():
    ticket = Ticket.model_validate({"id": 1, "number": 20001, "title": "Hi", "brand_new_field": {"x": 1}})
    assert ticket.number == "20001"
    assert ticket.model_extra == {"brand_new_field": {"x": 1}}


def test_article_reads_from_alias_and_attachment_size():
    article = Article.model_validate(
        {
            "id": 3,
            "from": "a@example.com",
            "attachments": [
                {
                    "id": 9,
                    "filename": "a.txt",
                    "size": "12",
                    "preferences": {"Content-Type": "Text/Plain; charset=UTF-8"},
                }
            ],
        }
    )
    assert article.from_ == "a@example.com"
    assert article.attachments[0].size == 12
    assert article.attachments[0].content_type == "text/plain"


@pytest.mark.parametrize("size", [None, "", "abc"])
def test_attachment_size_unknown(size):
    attachment = Attachment.model_validate({"id": 1, "size": size})
    assert attachment.size is None
    assert attachment.content_type == "application/octet-stream"
