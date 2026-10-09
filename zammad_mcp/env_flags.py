"""Shared parsing for the server's boolean environment flags.

Every gate (``ZAMMAD_READ_ONLY``, ``ZAMMAD_ENABLE_TICKETS``,
``ZAMMAD_FILTER_TOOLS_BY_ROLE``, ...) is read through :func:`parse_flag` so
that all of them agree on what "true" means. Values are stripped before
comparison: ``ZAMMAD_READ_ONLY="true "`` with a trailing space is routine in
Docker ``.env`` files and compose ``environment:`` blocks, and must not
silently fall back to "false" and re-expose write tools.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

_TRUTHY = ("true", "1", "yes", "on")


def parse_flag(raw: str | None, default: bool = False) -> bool:
    """Interpret one raw flag value.

    Args:
        raw: The variable's value, or ``None`` when unset.
        default: Returned when the value is unset or blank.

    Returns:
        ``True`` when the stripped, lower-cased value is one of ``true``,
        ``1``, ``yes`` or ``on``; ``False`` for any other non-blank value.
    """
    value = (raw or "").strip().lower()
    if not value:
        return default
    return value in _TRUTHY


def env_flag(name: str, default: bool = False, environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the named environment variable is set to a truthy value.

    Args:
        name: Environment variable name.
        default: Returned when the variable is unset or blank.
        environ: Mapping to read instead of ``os.environ``.
    """
    env = os.environ if environ is None else environ
    return parse_flag(env.get(name), default)
