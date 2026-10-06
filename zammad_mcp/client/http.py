"""Minimal async Zammad REST client.

- One shared ``httpx.AsyncClient`` per process, created lazily so it binds to
  the running event loop, and dropped again by :meth:`ZammadClient.aclose`.
- The API token travels per call as ``Authorization: Token token=<token>``;
  the shared client carries no credential of its own.
- Cookies are refused outright, so no session can leak between users.
- A request hook makes it impossible to send any ``X-Auth-Request-*`` header,
  which an SSO-fronted Zammad would accept as proof of identity.
"""

from __future__ import annotations

from http.cookiejar import CookieJar, CookiePolicy
from typing import Any

import httpx

from zammad_mcp.client.errors import (
    IdentityHeaderError,
    MissingTokenError,
    ZammadAPIError,
    ZammadTransportError,
    error_detail,
)

IDENTITY_HEADER_PREFIX = "x-auth-request-"
USER_AGENT = "zammad-mcp-server"


class _RefuseAllCookies(CookiePolicy):
    netscape = True
    rfc2965 = False
    hide_cookie2 = True

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False

    def domain_return_ok(self, domain, request) -> bool:
        return False

    def path_return_ok(self, path, request) -> bool:
        return False


def _cookieless_jar() -> CookieJar:
    return CookieJar(policy=_RefuseAllCookies())


def _identity_headers(headers: httpx.Headers) -> list[str]:
    return [name for name in headers if name.lower().startswith(IDENTITY_HEADER_PREFIX)]


async def reject_identity_headers(request: httpx.Request) -> None:
    leaked = _identity_headers(request.headers)
    if leaked:
        raise IdentityHeaderError(f"refusing to send identity header(s) {', '.join(sorted(leaked))} to Zammad")


def _json_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise ZammadAPIError(response.status_code, "response was not JSON") from exc


def token_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Token token={token}"}


class ZammadClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        connect_timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                cookies=_cookieless_jar(),
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                event_hooks={"request": [reject_identity_headers]},
                follow_redirects=False,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if not token:
            raise MissingTokenError("no Zammad API token is configured for this request")
        try:
            response = await self._client().request(
                method,
                path,
                params=params,
                json=json,
                headers={**(headers or {}), **token_header(token)},
            )
        except httpx.TransportError as exc:
            raise ZammadTransportError(f"could not reach Zammad ({type(exc).__name__})") from exc
        if not response.is_success:
            raise ZammadAPIError(response.status_code, error_detail(response))
        return _json_body(response)

    async def get(self, path: str, *, token: str | None, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, token=token, params=params)
