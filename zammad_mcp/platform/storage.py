"""Valkey (or in-process) storage for platform mode.

- :func:`parse_storage_url` validates ``MCP_OAUTH_STORAGE_URL`` and maps the
  ``valkey(s)://`` aliases to ``redis(s)://``, keeping the query string
  (``?ssl_cert_reqs=none``) that ``RedisStore(url=...)`` would silently drop.
- :func:`build_oauth_storage` wraps a ``RedisStore`` in a
  ``FernetEncryptionWrapper`` for FastMCP's OAuth state, keyed like FastMCP's
  own file store so a rotated secret invalidates both alike.
- :class:`KeyValue` is the small async surface the token cache and its
  re-check markers use, with a Valkey and an in-process implementation.
- :class:`ValkeyConfirmationBackend` keeps two-step confirmations in Valkey,
  encrypted and under a global byte cap.
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

from zammad_mcp.confirmations import ConfirmationError

if TYPE_CHECKING:
    from key_value.aio.protocols.key_value import AsyncKeyValue
    from redis.asyncio import Redis

OAUTH_STATE_SALT = "fastmcp-storage-encryption-key"
CONFIRMATION_SALT = "zammad-mcp-confirmations"
CONFIRMATION_LEDGER_PREFIX = "zammad-mcp:confirm-bytes:v1:"
MAX_CONFIRMATION_BYTES = 128 * 1024 * 1024
SMALL_RECORD_BYTES = 64 * 1024
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

    async def get_many(self, keys: list[str]) -> list[str | None]:
        """``MGET``: one round trip for several keys."""
        ...

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

    async def get_many(self, keys: list[str]) -> list[str | None]:
        return list(await self._client.mget(keys))

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

    async def get_many(self, keys: list[str]) -> list[str | None]:
        live = self._live()
        return [live[key][0] if key in live else None for key in keys]

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


class ConfirmationStoreFullError(ConfirmationError):
    """The shared confirmation store is at its byte cap."""


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


class ValkeyConfirmationBackend:
    """The :class:`zammad_mcp.confirmations.ConfirmationBackend` protocol on Valkey, for every replica.

    - ``put`` is ``SET key value EX ttl`` and ``take`` is ``GETDEL``, so a
      token is spent once across replicas.
    - Records can hold a whole email with base64 attachments, so they are
      Fernet-encrypted at rest (authenticated: a rewritten record fails to
      decrypt). One that does not decrypt, after a key rotation say, reads as
      absent and the caller prepares the action again.
    - A global byte cap matches the in-memory backend: ``max_bytes`` of
      plaintext UTF-8, the last ``small_record_reserve`` of it kept for
      records under ``SMALL_RECORD_BYTES``. Sizes live in a sorted-set ledger
      scored by expiry, checked and updated in one ``WATCH``/``MULTI``
      transaction, so a full store refuses the next record cleanly. Fernet
      adds about a third on top in Valkey memory; a 20 MB record fits.
    """

    def __init__(
        self,
        client: Redis,
        fernet: Fernet,
        *,
        namespace: str,
        max_bytes: int = MAX_CONFIRMATION_BYTES,
        small_record_reserve: int | None = None,
        clock: Clock = time.time,
    ) -> None:
        self._client = client
        self._fernet = fernet
        self._ledger = f"{CONFIRMATION_LEDGER_PREFIX}{namespace}"
        self._max_bytes = max_bytes
        self._reserve = max_bytes // 16 if small_record_reserve is None else small_record_reserve
        self._clock = clock

    def _limit_for(self, size: int) -> int:
        return self._max_bytes if size < SMALL_RECORD_BYTES else self._max_bytes - self._reserve

    async def held_bytes(self) -> int:
        live = await self._client.zrangebyscore(self._ledger, f"({self._clock()}", "+inf")
        return sum(_ledger_size(member) for member in live)

    async def put(self, key: str, value: str, ttl_seconds: int) -> None:
        size = _utf8_size(value)
        sealed = self._fernet.encrypt(value.encode("utf-8", "surrogatepass")).decode()
        member = f"{size}:{key}"

        async def store(pipe) -> None:
            now = self._clock()
            live = await pipe.zrangebyscore(self._ledger, f"({now}", "+inf")
            held = sum(_ledger_size(entry) for entry in live if not entry.endswith(f":{key}"))
            if held + size > self._limit_for(size):
                raise ConfirmationStoreFullError(
                    "too many large actions are waiting for confirmation on this server; "
                    "confirm or abandon some and try again in a few minutes"
                )
            pipe.multi()
            pipe.zremrangebyscore(self._ledger, "-inf", now)
            pipe.set(key, sealed, ex=ttl_seconds)
            pipe.zadd(self._ledger, {member: now + ttl_seconds})

        await self._client.transaction(store, self._ledger)

    async def take(self, key: str) -> str | None:
        sealed = await self._client.getdel(key)
        if sealed is None:
            return None
        try:
            value = self._fernet.decrypt(sealed.encode()).decode("utf-8", "surrogatepass")
        except InvalidToken:
            return None
        await self._client.zrem(self._ledger, f"{_utf8_size(value)}:{key}")
        return value


def _ledger_size(member: str) -> int:
    size, _, _ = member.partition(":")
    return int(size) if size.isdigit() else 0
