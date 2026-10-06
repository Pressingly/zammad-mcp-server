"""Errors raised by the Zammad client.

Tools never let these escape: :func:`to_tool_error` turns any of them into
the short ``Error: ...`` string a tool returns to the model.
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


class ZammadTransportError(ZammadError):
    """The request never produced a response (DNS, connect, timeout)."""


class ContentTooLargeError(ZammadError):
    """A download exceeded the size the caller allowed."""


class ZammadAPIError(ZammadError):
    """Zammad answered with a non-success HTTP status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class AuthError(ZammadAPIError):
    """HTTP 401: the token is invalid, expired or revoked."""


class PermissionDenied(ZammadAPIError):
    """HTTP 403: the user's role or the token's permissions forbid the action."""


class GatewayDeniedError(PermissionDenied):
    """HTTP 403 from an SSO edge in front of Zammad (e.g. ``{"error": "access_denied"}``), not from Zammad's roles."""


class AccountInactiveError(PermissionDenied):
    """HTTP 403: the Zammad account is disabled."""


class MaintenanceModeError(PermissionDenied):
    """HTTP 403: Zammad is in maintenance mode."""


class NotFoundError(ZammadAPIError):
    """HTTP 404: the object does not exist or is not visible to this user."""


class UnprocessableError(ZammadAPIError):
    """HTTP 422: Zammad rejected the request body."""


class RateLimitedError(ZammadAPIError):
    """HTTP 429: Zammad is throttling this client."""


class ServerError(ZammadAPIError):
    """HTTP 5xx: Zammad, or the proxy in front of it, failed."""


_ERRORS_BY_STATUS: dict[int, type[ZammadAPIError]] = {
    401: AuthError,
    404: NotFoundError,
    422: UnprocessableError,
    429: RateLimitedError,
}


_FORBIDDEN_BY_DETAIL: tuple[tuple[str, type[PermissionDenied]], ...] = (
    ("access_denied", GatewayDeniedError),
    ("not active", AccountInactiveError),
    ("maintenance mode", MaintenanceModeError),
)


def _forbidden(detail: str) -> type[PermissionDenied]:
    lowered = detail.lower()
    return next((error for marker, error in _FORBIDDEN_BY_DETAIL if marker in lowered), PermissionDenied)


def error_for_status(status_code: int, detail: str) -> ZammadAPIError:
    if status_code == 403:
        return _forbidden(detail)(status_code, detail)
    if status_code >= 500:
        return ServerError(status_code, detail)
    return _ERRORS_BY_STATUS.get(status_code, ZammadAPIError)(status_code, detail)


def error_detail(response: httpx.Response) -> str:
    fallback = response.reason_phrase or "unexpected response"
    try:
        body = response.json()
    except ValueError:
        return fallback
    if not isinstance(body, dict):
        return fallback
    return str(body.get("error_human") or body.get("error") or fallback)


def to_tool_error(error: ZammadError) -> str:
    match error:
        case GatewayDeniedError():
            return (
                "Error: access denied by the sign-in gateway in front of Zammad (HTTP 403); "
                "this account is not allowed to use this Zammad"
            )
        case AccountInactiveError():
            return "Error: this Zammad account is not active (HTTP 403); ask a Zammad admin to reactivate it"
        case MaintenanceModeError():
            return "Error: Zammad is in maintenance mode (HTTP 403); try again later"
        case AuthError():
            return "Error: Zammad rejected the token (HTTP 401); it is invalid, expired or revoked"
        case PermissionDenied():
            return (
                f"Error: permission denied (HTTP 403: {error.detail}); "
                "your Zammad role or token permissions do not allow this"
            )
        case NotFoundError():
            return "Error: not found (HTTP 404), or not visible to this user"
        case UnprocessableError():
            return f"Error: Zammad rejected the request (HTTP 422): {error.detail}"
        case RateLimitedError():
            return "Error: Zammad is rate limiting requests (HTTP 429); try again shortly"
        case ServerError():
            return f"Error: Zammad had a server error (HTTP {error.status_code}); try again later"
        case ZammadAPIError():
            return f"Error: Zammad returned HTTP {error.status_code} ({error.detail})"
    return f"Error: {error}"
