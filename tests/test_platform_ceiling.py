from __future__ import annotations

import pytest

from zammad_mcp.platform.ceiling import (
    MINTABLE_PERMISSIONS,
    CeilingError,
    ceiling_fingerprint,
    ceiling_for,
    narrow_ceiling,
    validate_permissions,
)
from zammad_mcp.platform.mint import TokenMinter


def test_ceiling_takes_mintable_leaves_only():
    held = [
        "ticket",
        "ticket.agent",
        "admin",
        "admin.user",
        "report",
        "user_preferences.access_token",
        7,
        "knowledge_base",
    ]

    assert ceiling_for(held, {}) == frozenset({"ticket.agent"})


def test_narrowing_can_only_remove():
    held = ["ticket.agent", "knowledge_base.reader"]

    assert ceiling_for(held, {}, lambda claims, ceiling: {"admin", "ticket.agent"}) == frozenset({"ticket.agent"})
    assert ceiling_for(held, {}, lambda claims, ceiling: []) == frozenset()
    assert narrow_ceiling({}, frozenset({"ticket.agent"})) == frozenset({"ticket.agent"})


def test_default_minter_narrowing_is_the_identity_hook():
    assert TokenMinter.__init__.__kwdefaults__["narrow"] is narrow_ceiling


@pytest.mark.parametrize(
    "permissions", [[], ["admin"], ["ticket.agent", "report"], ["user_preferences.access_token"], ["ticket"]]
)
def test_validation_refuses_empty_or_unmintable_lists(permissions):
    with pytest.raises(CeilingError):
        validate_permissions(permissions)


def test_validation_sorts_and_dedupes():
    assert validate_permissions(["ticket.customer", "knowledge_base.reader", "ticket.customer"]) == [
        "knowledge_base.reader",
        "ticket.customer",
    ]
    assert MINTABLE_PERMISSIONS == {"ticket.agent", "ticket.customer", "knowledge_base.reader", "knowledge_base.editor"}


def test_fingerprint_ignores_order():
    assert ceiling_fingerprint(["a", "b"]) == ceiling_fingerprint(["b", "a"])
    assert ceiling_fingerprint(["a"]) != ceiling_fingerprint(["a", "b"])
