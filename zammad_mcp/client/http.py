"""Async Zammad REST client.

- One shared ``httpx.AsyncClient`` per process, created lazily so it binds to
  the running event loop, and dropped again by :meth:`ZammadClient.aclose`.
- The API token travels per call as ``Authorization: Token token=<token>``;
  the shared client carries no credential of its own.
- Cookies are refused outright, so no session can leak between users. The
  refusing ``CookieJar`` is handed to httpx as is: wrapping it in
  ``httpx.Cookies()`` copies the cookies into a fresh jar with the default
  policy and silently drops the refusal.
- A request hook makes it impossible to send any ``X-Auth-Request-*`` header,
  which an SSO-fronted Zammad would accept as proof of identity.
- Retries (see :class:`RetryPolicy`): GET, PUT and DELETE retry on connect
  errors, read and write failures, protocol errors and 502/503/504. POST
  retries only when the request provably never left the process
  (``ConnectError``, ``ConnectTimeout``, ``PoolTimeout``), so an article or
  email is never posted twice. A 429 is retried once for
  every method, after ``Retry-After``, because Zammad did not process it.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookiejar import CookieJar, CookiePolicy
from typing import Any

import httpx

from zammad_mcp.client.errors import (
    ContentTooLargeError,
    IdentityHeaderError,
    InvalidTokenError,
    MissingTokenError,
    ZammadAPIError,
    ZammadTransportError,
    error_detail,
    error_for_status,
)
from zammad_mcp.client.text import is_ascii_digits

IDENTITY_HEADER_PREFIX = "x-auth-request-"
USER_AGENT = "zammad-mcp-server"
RETRY_SAFE_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})
RETRYABLE_STATUSES = frozenset({502, 503, 504})
PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
IN_FLIGHT_ERRORS = (httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.RemoteProtocolError)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay_seconds: float = 0.25
    rate_limit_cap_seconds: float = 10.0
    rate_limit_default_seconds: float = 1.0

    def backoff(self, attempt: int, rng: random.Random) -> float:
        delay = self.base_delay_seconds * 2**attempt
        return delay + rng.uniform(0, delay)

    def rate_limit_wait(self, response: httpx.Response) -> float:
        raw = response.headers.get("retry-after", "").strip()
        seconds = float(raw) if is_ascii_digits(raw) else self.rate_limit_default_seconds
        return min(seconds, self.rate_limit_cap_seconds)


@dataclass(frozen=True)
class Download:
    content: bytes
    content_type: str


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


def is_identity_header(name: str) -> bool:
    """``X-Auth-Request-*`` in any case, with ``_`` treated as ``-`` (some proxies map one to the other)."""
    return name.lower().replace("_", "-").startswith(IDENTITY_HEADER_PREFIX)


def is_valid_token(token: str) -> bool:
    return token.isascii() and token.isprintable() and " " not in token


def _identity_headers(headers: httpx.Headers) -> list[str]:
    return [name for name in headers if is_identity_header(name)]


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


def _retryable_transport_error(error: httpx.TransportError, retry_safe: bool) -> bool:
    if isinstance(error, PRE_SEND_ERRORS):
        return True
    return retry_safe and isinstance(error, IN_FLIGHT_ERRORS)


def _declared_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length", "")
    return int(raw) if is_ascii_digits(raw) else None


class ZammadClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        connect_timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Sleep | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        self._transport = transport
        self._retry = retry or RetryPolicy()
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
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
        retry_safe: bool | None = None,
    ) -> Any:
        """Send one request and return its decoded JSON body (``None`` when empty).

        ``retry_safe`` overrides the method default, for read-only POSTs such
        as ``/tickets/search``.
        """
        response = await self._send(
            method, path, token=token, params=params, json=json, headers=headers, retry_safe=retry_safe
        )
        return _json_body(response)

    async def get(self, path: str, *, token: str | None, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, token=token, params=params)

    async def download(self, path: str, *, token: str | None, max_bytes: int) -> Download:
        """Fetch a binary body, aborting as soon as it exceeds ``max_bytes``."""
        response = await self._send("GET", path, token=token, stream=True)
        try:
            declared = _declared_length(response)
            if declared is not None and declared > max_bytes:
                raise ContentTooLargeError(f"the file is {declared} bytes, over the {max_bytes}-byte limit")
            content = await self._read_capped(response, max_bytes)
        finally:
            await response.aclose()
        return Download(content=content, content_type=response.headers.get("content-type", "application/octet-stream"))

    @staticmethod
    async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > max_bytes:
                raise ContentTooLargeError(f"the file is over the {max_bytes}-byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        retry_safe: bool | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        if not token:
            raise MissingTokenError("no Zammad API token is configured for this request")
        if not is_valid_token(token):
            raise InvalidTokenError("the Zammad API token must be printable ASCII without spaces")
        method = method.upper()
        safe = method in RETRY_SAFE_METHODS if retry_safe is None else retry_safe
        client = self._client()
        attempt = 0
        rate_limit_retried = False
        while True:
            request = client.build_request(
                method, path, params=params, json=json, headers={**(headers or {}), **token_header(token)}
            )
            try:
                response = await client.send(request, stream=stream)
            except httpx.TransportError as exc:
                if attempt < self._retry.max_retries and _retryable_transport_error(exc, safe):
                    await self._sleep(self._retry.backoff(attempt, self._rng))
                    attempt += 1
                    continue
                raise ZammadTransportError(f"could not reach Zammad ({type(exc).__name__})") from exc

            if response.status_code == 429 and not rate_limit_retried:
                rate_limit_retried = True
                await response.aclose()
                await self._sleep(self._retry.rate_limit_wait(response))
                continue
            if response.status_code in RETRYABLE_STATUSES and safe and attempt < self._retry.max_retries:
                await response.aclose()
                await self._sleep(self._retry.backoff(attempt, self._rng))
                attempt += 1
                continue
            if not response.is_success:
                await response.aread()
                await response.aclose()
                raise error_for_status(response.status_code, error_detail(response))
            return response
