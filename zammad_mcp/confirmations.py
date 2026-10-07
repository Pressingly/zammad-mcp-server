"""Two-step confirmation for actions that must not run on a model's whim.

``issue`` stores a record bound to the caller's identity, the action name and
the sha256 of the exact payload, and returns a random token. ``consume`` takes
the record out atomically (single use, even when the check then fails), and
succeeds only if it has not expired and identity, action and payload all
match. Records are stored under ``sha256(token)``, never the token itself.

The backend is a two-method protocol: :class:`MemoryConfirmationBackend` now,
a Valkey one later (``SET key value EX ttl`` and ``GETDEL key``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from zammad_mcp.config import DEFAULT_CONFIRM_TTL_SECONDS

KEY_PREFIX = "zammad-mcp:confirm:v1:"

Clock = Callable[[], float]


class ConfirmationError(Exception):
    """The confirmation token is unknown, used, expired or does not match."""


class ConfirmationBackend(Protocol):
    async def put(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def take(self, key: str) -> str | None:
        """Atomically return and delete the value, or ``None`` if absent or expired."""
        ...


class MemoryConfirmationBackend:
    def __init__(self, clock: Clock = time.time) -> None:
        self._clock = clock
        self._values: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, value: str, ttl_seconds: int) -> None:
        async with self._lock:
            now = self._clock()
            live = {k: entry for k, entry in self._values.items() if entry[1] > now}
            self._values = {**live, key: (value, now + ttl_seconds)}

    async def take(self, key: str) -> str | None:
        async with self._lock:
            entry = self._values.get(key)
            self._values = {k: v for k, v in self._values.items() if k != key}
        if entry is None or entry[1] <= self._clock():
            return None
        return entry[0]


def payload_digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _storage_key(token: str) -> str:
    return KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class _Record:
    identity: str
    action: str
    payload_sha256: str
    expires_at: float


class Confirmations:
    def __init__(
        self,
        backend: ConfirmationBackend,
        *,
        ttl_seconds: int = DEFAULT_CONFIRM_TTL_SECONDS,
        clock: Clock = time.time,
    ) -> None:
        self._backend = backend
        self._ttl = ttl_seconds
        self._clock = clock

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    async def issue(self, *, identity: str, action: str, payload: Any) -> str:
        token = secrets.token_urlsafe(32)
        record = _Record(identity, action, payload_digest(payload), self._clock() + self._ttl)
        await self._backend.put(_storage_key(token), json.dumps(asdict(record)), self._ttl)
        return token

    async def consume(self, token: str, *, identity: str, action: str, payload: Any) -> None:
        """Spend ``token``; raise :class:`ConfirmationError` unless it matches this exact request."""
        raw = await self._backend.take(_storage_key(token)) if token else None
        if raw is None:
            raise ConfirmationError("confirmation token is unknown, already used or expired; prepare the action again")
        record = _Record(**json.loads(raw))
        if record.expires_at <= self._clock():
            raise ConfirmationError("confirmation token expired; prepare the action again")
        expected = (identity, action, payload_digest(payload))
        actual = (record.identity, record.action, record.payload_sha256)
        if not all(hmac.compare_digest(a, b) for a, b in zip(expected, actual, strict=True)):
            raise ConfirmationError("confirmation token does not match this request; prepare the action again")
