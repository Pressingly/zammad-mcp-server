"""Errors raised by the Zammad client.

Tools never let these escape: :func:`describe_error` turns any of them into
the short string a tool returns to the model.
"""

from __future__ import annotations

import httpx


class ZammadError(Exception):
    """Base class for every failure talking to Zammad."""


class IdentityHeaderError(ZammadError):
    """An ``X-Auth-Request-*`` header was about to leave the process.

    Those headers are trusted by an SSO-fronted Zammad as proof of identity.
    The client must never send one, whatever the caller supplied.
    """


class MissingTokenError(ZammadError):
    """No Zammad API token is available for this call."""


class ZammadAPIError(ZammadError):
    """Zammad answered with a non-success HTTP status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ZammadTransportError(ZammadError):
    """The request never produced a response (DNS, connect, timeout)."""


_STATUS_HINTS = {
    401: "the Zammad token is invalid or expired",
    403: "the Zammad token lacks permission for this action",
    404: "not found, or not visible to this user",
    429: "Zammad is rate limiting requests; try again shortly",
}


def describe_error(error: ZammadError) -> str:
    if isinstance(error, ZammadAPIError):
        hint = _STATUS_HINTS.get(error.status_code, error.detail)
        return f"Error: Zammad returned HTTP {error.status_code} ({hint})"
    return f"Error: {error}"


def error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "unexpected response"
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return response.reason_phrase or "unexpected response"
