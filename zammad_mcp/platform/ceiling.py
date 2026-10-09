"""The permission ceiling of a minted token.

A minted token carries an explicit list: the leaf permissions this server
uses, intersected with what the user's Zammad role holds today. Zammad
intersects role and token again on every check, so a role downgrade applies
at once; the client-side intersection keeps the stored list honest, because
Zammad stores whatever list it is sent.

``admin.*``, ``report`` and ``user_preferences.*`` can never enter a token:
they are not in :data:`MINTABLE_PERMISSIONS`, and :func:`validate_permissions`
refuses them again right before the POST.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from typing import Any

MINTABLE_PERMISSIONS = frozenset({"ticket.agent", "ticket.customer", "knowledge_base.reader", "knowledge_base.editor"})
FORBIDDEN_PREFIXES = ("admin", "report", "user_preferences")

CeilingNarrower = Callable[[Mapping[str, Any], frozenset[str]], Iterable[str]]


class CeilingError(ValueError):
    """The permission list is empty or holds something a minted token must never carry."""


def narrow_ceiling(claims: Mapping[str, Any], ceiling: frozenset[str]) -> frozenset[str]:
    """Hook for a future mPass/RBAC claim (FOSS-493) to drop permissions; the identity today."""
    return ceiling


def _forbidden(permission: str) -> bool:
    return any(permission == prefix or permission.startswith(f"{prefix}.") for prefix in FORBIDDEN_PREFIXES)


def ceiling_for(
    user_permissions: Iterable[Any],
    claims: Mapping[str, Any],
    narrow: CeilingNarrower = narrow_ceiling,
) -> frozenset[str]:
    """Mintable leaves the user holds, after ``narrow``, which may only ever remove permissions."""
    held = frozenset(permission for permission in user_permissions if isinstance(permission, str))
    explicit = MINTABLE_PERMISSIONS & held
    return explicit & frozenset(narrow(claims, explicit))


def validate_permissions(permissions: Iterable[str]) -> list[str]:
    """The sorted list to POST; raises :class:`CeilingError` unless it is non-empty and mintable."""
    chosen = sorted(frozenset(permissions))
    if not chosen:
        raise CeilingError("the permission list is empty")
    unexpected = [
        permission for permission in chosen if permission not in MINTABLE_PERMISSIONS or _forbidden(permission)
    ]
    if unexpected:
        raise CeilingError(f"refusing to mint a token with {', '.join(unexpected)}")
    return chosen


def ceiling_fingerprint(ceiling: Iterable[str]) -> str:
    """Changes exactly when the ceiling does; a re-check that sees a new one re-mints."""
    return hashlib.sha256("\n".join(sorted(ceiling)).encode()).hexdigest()
