"""Valkey (or in-process) storage for platform mode.

- :func:`parse_storage_url` validates ``MCP_OAUTH_STORAGE_URL`` and maps the
  ``valkey(s)://`` aliases to ``redis(s)://``, keeping the query string
  (``?ssl_cert_reqs=none``) that ``RedisStore(url=...)`` would silently drop.
- :func:`build_oauth_storage` wraps a ``RedisStore`` in a
  ``FernetEncryptionWrapper`` for FastMCP's OAuth state, keyed like FastMCP's
  own file store so a rotated secret invalidates both alike.
- :class:`KeyValue` is the small async surface the token cache, the re-check
  limiter and the confirmation store use, with a Valkey and an in-process
  implementation.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse, urlunparse

from cryptography.fernet import Fernet, InvalidToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key

if TYPE_CHECKING:
    from key_value.aio.protocols.key_value import AsyncKeyValue
    from redis.asyncio import Redis

OAUTH_STATE_SALT = "fastmcp-storage-encryption-key"
CONFIRMATION_SALT = "zammad-mcp-confirmations"
SCHEME_ALIASES = {"valkey": "redis", "valkeys": "rediss"}
ACCEPTED_SCHEMES = frozenset({"redis", "rediss", "valkey", "valkeys"})

Clock = Callable[[], float]


@dataclass(frozen=True)
class RedisConfig:
    """``url`` goes to ``Redis.from_url`` verbatim; ``host``, ``port`` and ``db`` are for logs."""

    url: str
    host: str
    port: int
    db: int


def parse_storage_url(raw: str) -> RedisConfig:
    parsed = urlparse(raw)
    if parsed.scheme not in ACCEPTED_SCHEMES:
        raise ValueError(
            f"MCP_OAUTH_STORAGE_URL scheme must be redis://, rediss://, valkey:// or valkeys://, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ValueError("MCP_OAUTH_STORAGE_URL is missing a hostname")
    path = parsed.path.lstrip("/")
    if path and not (path.isascii() and path.isdigit()):
        raise ValueError(f"MCP_OAUTH_STORAGE_URL path must be a numeric DB index, got {parsed.path!r}")
    scheme = SCHEME_ALIASES.get(parsed.scheme, parsed.scheme)
    return RedisConfig(
        url=urlunparse(parsed._replace(scheme=scheme)),
        host=parsed.hostname,
        port=parsed.port or 6379,
        db=int(path) if path else 0,
    )


def fernet_for(key_material: str, salt: str) -> Fernet:
    return Fernet(key=derive_jwt_key(high_entropy_material=key_material, salt=salt))


def build_redis(config: RedisConfig) -> Redis:
    from redis.asyncio import Redis

    return Redis.from_url(config.url, decode_responses=True)


def build_oauth_storage(client: Redis, key_material: str) -> AsyncKeyValue:
    from key_value.aio.stores.redis import RedisStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    return FernetEncryptionWrapper(
        key_value=RedisStore(client=client),
        fernet=fernet_for(key_material, OAUTH_STATE_SALT),
        raise_on_decryption_error=False,
    )


class KeyValue(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def set_if_absent(self, key: str, value: str, ttl_seconds: int) -> bool:
        """``SET key value NX EX ttl``: true when this call stored the value."""
        ...

    async def delete(self, key: str) -> None: ...

    async def take(self, key: str) -> str | None:
        """``GETDEL``: return and delete atomically."""
        ...


class RedisKeyValue:
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._client.set(key, value, ex=ttl_seconds)

    async def set_if_absent(self, key: str, value: str, ttl_seconds: int) -> bool:
        return bool(await self._client.set(key, value, ex=ttl_seconds, nx=True))

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def take(self, key: str) -> str | None:
        return await self._client.getdel(key)


class MemoryKeyValue:
    """Process-local fallback when ``MCP_OAUTH_STORAGE_URL`` is unset: lost on restart, never shared."""

    def __init__(self, clock: Clock = time.time) -> None:
        self._clock = clock
        self._values: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def _live(self) -> dict[str, tuple[str, float]]:
        now = self._clock()
        return {key: entry for key, entry in self._values.items() if entry[1] > now}

    async def get(self, key: str) -> str | None:
        entry = self._live().get(key)
        return entry[0] if entry else None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        async with self._lock:
            self._values = {**self._live(), key: (value, self._clock() + ttl_seconds)}

    async def set_if_absent(self, key: str, value: str, ttl_seconds: int) -> bool:
        async with self._lock:
            live = self._live()
            if key in live:
                return False
            self._values = {**live, key: (value, self._clock() + ttl_seconds)}
            return True

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values = {k: entry for k, entry in self._live().items() if k != key}

    async def take(self, key: str) -> str | None:
        async with self._lock:
            entry = self._live().get(key)
            self._values = {k: v for k, v in self._live().items() if k != key}
        return entry[0] if entry else None


class KeyValueConfirmationBackend:
    """The :class:`zammad_mcp.confirmations.ConfirmationBackend` protocol over a :class:`KeyValue`.

    Records can hold a whole email (body and base64 attachments, ~14 MB), so
    they are Fernet-encrypted at rest. A record that does not decrypt (a
    rotated key) reads as absent: the caller prepares the action again.
    """

    def __init__(self, store: KeyValue, fernet: Fernet) -> None:
        self._store = store
        self._fernet = fernet

    async def put(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._store.set(key, self._fernet.encrypt(value.encode()).decode(), ttl_seconds)

    async def take(self, key: str) -> str | None:
        sealed = await self._store.take(key)
        if sealed is None:
            return None
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken:
            return None
