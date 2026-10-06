"""The credential every tool call runs with, and how a mode supplies it.

A :class:`CredentialProvider` answers "which Zammad token, and whose, does
this request use?". Community mode has two (``static``, ``header``); platform
mode (FOSS-513) adds a minting one. Tools only ever see the protocol.

The identity of a token-based credential is ``sha256(token)[:16]``: stable
enough to key a memo on, and never the raw token.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from zammad_mcp.client import ZammadClient, ZammadError
from zammad_mcp.client.models import Role, User
from zammad_mcp.tiers import Tier, tier_for

IDENTITY_LENGTH = 16

Clock = Callable[[], float]


def token_identity(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:IDENTITY_LENGTH]


@dataclass(frozen=True)
class Profile:
    """What the token's owner may do, read from their Zammad roles."""

    tier: Tier | None = None
    permissions: frozenset[str] = frozenset()

    @property
    def known(self) -> bool:
        return self.tier is not None


UNKNOWN_PROFILE = Profile()


@dataclass(frozen=True)
class ZammadCredential:
    token: str = field(repr=False)
    identity: str
    tier: Tier | None
    permissions: frozenset[str]

    @classmethod
    def for_token(cls, token: str, profile: Profile) -> ZammadCredential:
        return cls(token=token, identity=token_identity(token), tier=profile.tier, permissions=profile.permissions)


class CredentialProvider(Protocol):
    async def resolve(self) -> ZammadCredential:
        """Return the credential for the current request, or raise a :class:`ZammadError`."""
        ...


async def fetch_profile(client: ZammadClient, token: str) -> Profile:
    """Read the token owner's permissions from ``GET /users/me`` and their roles.

    ``GET /roles/{id}`` is open to ``ticket.agent`` and ``ticket.customer``,
    and ``expand=true`` returns the role's permission names.
    """
    me = User.parse(await client.get("/users/me", token=token, params={"expand": "true"}))
    roles = [
        Role.parse(await client.get(f"/roles/{role_id}", token=token, params={"expand": "true"}))
        for role_id in me.role_ids
    ]
    permissions = frozenset(permission for role in roles for permission in role.permissions)
    return Profile(tier=tier_for(permissions), permissions=permissions)


ProfileLoader = Callable[[], Awaitable[Profile]]


class ProfileMemo:
    """In-process memo of profiles, keyed by credential identity.

    Holds at most ``max_entries`` identities and evicts the least recently
    stored first. A failed lookup is remembered as unknown for
    ``failure_ttl`` seconds so a broken role lookup costs one extra request a
    minute, not one per call. The memo is the one deliberately mutable object
    here: it is a cache, and copying it on every store would be O(n).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float | None,
        failure_ttl_seconds: float = 60.0,
        max_entries: int = 1024,
        clock: Clock = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._failure_ttl = failure_ttl_seconds
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._entries: OrderedDict[str, tuple[Profile, float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    def _fresh(self, identity: str) -> Profile | None:
        entry = self._entries.get(identity)
        if entry is None or entry[1] <= self._clock():
            return None
        return entry[0]

    def _store(self, identity: str, profile: Profile) -> None:
        ttl = self._ttl if profile.known else self._failure_ttl
        self._entries[identity] = (profile, float("inf") if ttl is None else self._clock() + ttl)
        self._entries.move_to_end(identity)
        while len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            self._release_idle_lock(evicted)

    def _release_idle_lock(self, identity: str) -> None:
        lock = self._locks.get(identity)
        if lock is not None and not lock.locked():
            del self._locks[identity]

    async def get(self, identity: str, load: ProfileLoader) -> Profile:
        cached = self._fresh(identity)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(identity, asyncio.Lock())
        try:
            async with lock:
                cached = self._fresh(identity)
                if cached is not None:
                    return cached
                profile = await _load_or_unknown(load)
                self._store(identity, profile)
                return profile
        finally:
            if identity not in self._entries:
                self._release_idle_lock(identity)


async def _load_or_unknown(load: ProfileLoader) -> Profile:
    try:
        return await load()
    except ZammadError:
        return UNKNOWN_PROFILE
