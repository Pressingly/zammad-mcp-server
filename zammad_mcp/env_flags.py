"""Shared parsing for the server's boolean environment flags.

Every gate (``OUTLINE_READ_ONLY``, ``OUTLINE_ENABLE_DELETE``, …) is read
through :func:`env_flag` so that all of them agree on what "true" means.
Values are stripped before comparison: ``OUTLINE_READ_ONLY="true "`` with a
trailing space is routine in Docker ``.env`` files and compose
``environment:`` blocks, and must not silently fall back to "false" and
re-expose destructive tools.
"""

from __future__ import annotations

import os

_TRUTHY = ("true", "1", "yes", "on")


def env_flag(name: str) -> bool:
    """Return whether the named environment variable is set to a truthy value.

    Args:
        name: Environment variable name.

    Returns:
        ``True`` when the variable's stripped, lower-cased value is one of
        ``true``, ``1``, ``yes`` or ``on``; ``False`` otherwise (including unset).
    """
    return os.getenv(name, "").strip().lower() in _TRUTHY
