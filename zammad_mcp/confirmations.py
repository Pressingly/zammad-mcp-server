"""Two-step confirmation for actions that must not run on a model's whim.

``issue`` stores a record bound to the caller's identity, the action name and
the sha256 of the exact payload, and returns a random token. ``consume`` takes
the record out atomically (single use, even when the check then fails), and
succeeds only if it has not expired and identity, action and payload all
match. Records are stored under ``sha256(token)``, never the token itself.

``issue(..., keep_payload=True)`` also stores the payload, for actions whose
confirming call cannot repeat it (an email body). ``redeem`` then takes the
record out the same way, checks identity and action, and returns the payload.
It also recomputes the payload's digest. That detects a corrupted record, not
a forged one: anyone who can write the store can rewrite payload and digest
together. A shared store gets tamper protection from the platform Valkey
backend (FOSS-513), which encrypts records with Fernet (authenticated).

Bounds: one identity may hold at most ``max_outstanding_per_identity`` live
confirmations, and the memory backend refuses a record that would take it
over ``max_bytes``. Both refusals are :class:`ConfirmationLimitError`.

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
MAX_OUTSTANDING_PER_IDENTITY = 20
MAX_MEMORY_BYTES = 64 * 1024 * 1024

Clock = Callable[[], float]


class ConfirmationError(Exception):
    """The confirmation token is unknown, used, expired or does not match."""


class ConfirmationLimitError(ConfirmationError):
    """A new confirmation would exceed the caller's or the store's limit."""


class ConfirmationBackend(Protocol):
    async def put(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def take(self, key: str) -> str | None:
        """Atomically return and delete the value, or ``None`` if absent or expired."""
        ...


class MemoryConfirmationBackend:
    def __init__(self, clock: Clock = time.time, *, max_bytes: int = MAX_MEMORY_BYTES) -> None:
        self._clock = clock
        self._max_bytes = max_bytes
        self._values: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, value: str, ttl_seconds: int) -> None:
        async with self._lock:
            now = self._clock()
            live = {k: entry for k, entry in self._values.items() if entry[1] > now and k != key}
            stored = sum(len(entry[0]) for entry in live.values())
            if stored + len(value) > self._max_bytes:
                raise ConfirmationLimitError(
                    "too many large actions are waiting for confirmation on this server; "
                    "confirm or abandon some and try again in a few minutes"
                )
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
    payload: Any = None


class Confirmations:
    def __init__(
        self,
        backend: ConfirmationBackend,
        *,
        ttl_seconds: int = DEFAULT_CONFIRM_TTL_SECONDS,
        clock: Clock = time.time,
        max_outstanding_per_identity: int = MAX_OUTSTANDING_PER_IDENTITY,
    ) -> None:
        self._backend = backend
        self._ttl = ttl_seconds
        self._clock = clock
        self._max_outstanding = max_outstanding_per_identity
        self._outstanding: dict[str, dict[str, float]] = {}

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    async def issue(self, *, identity: str, action: str, payload: Any, keep_payload: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        key = _storage_key(token)
        expires_at = self._clock() + self._ttl
        record = _Record(identity, action, payload_digest(payload), expires_at, payload if keep_payload else None)
        self._reserve(identity, key, expires_at)
        try:
            await self._backend.put(key, json.dumps(asdict(record)), self._ttl)
        except BaseException:
            self._release(identity, key)
            raise
        return token

    async def consume(self, token: str, *, identity: str, action: str, payload: Any) -> None:
        """Spend ``token``; raise :class:`ConfirmationError` unless it matches this exact request."""
        record = await self._take(token)
        expected = (identity, action, payload_digest(payload))
        actual = (record.identity, record.action, record.payload_sha256)
        if not _all_equal(expected, actual):
            raise ConfirmationError("confirmation token does not match this request; prepare the action again")

    async def redeem(self, token: str, *, identity: str, action: str) -> Any:
        """Spend ``token`` and return the payload ``issue`` kept; raise :class:`ConfirmationError` on any mismatch."""
        record = await self._take(token)
        if not _all_equal((identity, action), (record.identity, record.action)):
            raise ConfirmationError("confirmation token does not match this request; prepare the action again")
        if record.payload is None or not _all_equal((payload_digest(record.payload),), (record.payload_sha256,)):
            raise ConfirmationError("confirmation record is corrupted; prepare the action again")
        return record.payload

    def _live(self, identity: str) -> dict[str, float]:
        now = self._clock()
        return {key: expires for key, expires in self._outstanding.get(identity, {}).items() if expires > now}

    def _reserve(self, identity: str, key: str, expires_at: float) -> None:
        live = self._live(identity)
        if len(live) >= self._max_outstanding:
            raise ConfirmationLimitError(
                f"you already have {len(live)} actions waiting for confirmation; confirm them or let them expire "
                f"(each lasts {self._ttl} seconds) before preparing another"
            )
        now = self._clock()
        others = {
            other: keys
            for other, keys in self._outstanding.items()
            if other != identity and any(expires > now for expires in keys.values())
        }
        self._outstanding = {**others, identity: {**live, key: expires_at}}

    def _release(self, identity: str, key: str) -> None:
        remaining = {other: expires for other, expires in self._live(identity).items() if other != key}
        others = {other: keys for other, keys in self._outstanding.items() if other != identity}
        self._outstanding = {**others, identity: remaining} if remaining else others

    async def _take(self, token: str) -> _Record:
        key = _storage_key(token)
        raw = await self._backend.take(key) if token else None
        if raw is None:
            raise ConfirmationError("confirmation token is unknown, already used or expired; prepare the action again")
        record = _Record(**json.loads(raw))
        self._release(record.identity, key)
        if record.expires_at <= self._clock():
            raise ConfirmationError("confirmation token expired; prepare the action again")
        return record


def _all_equal(expected: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    return all(hmac.compare_digest(a.encode(), b.encode()) for a, b in zip(expected, actual, strict=True))
