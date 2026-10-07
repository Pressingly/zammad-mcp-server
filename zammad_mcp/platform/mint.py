"""Per-user Zammad tokens, minted from the user's SSO identity and bounded by their role.

Bootstrap (cache miss, re-check, or a rejected token), always on a fresh
httpx client of its own and the internal Zammad URL:

1. ``GET /user_access_token`` with the SSO email header (plus the upstream
   access token, for the corporate-ID gate, when there is one). Zammad's SSO
   middleware opens a session for that user and answers with a CSRF token
   and the user's permissions.
2. The ceiling: mintable leaves the role holds (:mod:`.ceiling`). Empty
   fails closed.
3. ``POST /user_access_token`` with that explicit list and
   ``expires_at = today + 9 days``. The session cookie is ``Secure`` and the
   internal URL is plain http, so httpx would never send it back: it is
   copied by hand into a ``Cookie`` header.
4. Best effort: delete this user's own *expired* ``zammad-mcp`` tokens.
   A live token is never revoked, since another replica may be using it.
5. Always, even when a step failed: ``DELETE /signout`` with the session
   cookie. The middleware made a persistent web session with the user's full
   role; signing out deletes its row instead of leaving it for Zammad's
   four-week session reaper.

Every bootstrap request sends the same ``X-Browser-Fingerprint`` and
``User-Agent`` (the steady-state client's), so Zammad records one device
per user and sends at most one new-device email.

The result is cached Fernet-encrypted under the deployment namespace and
``sha256(identity)`` for six days and treated as stale within a day of
expiry. It is re-checked every four hours, and after a permission-gated 403:
the 403 only sets a "due" flag behind a ``SET NX EX`` limiter (at most once
per five minutes per identity) and never rewrites the cached token, so it
cannot race a concurrent re-mint; the next re-check or mint clears the flag.
A changed ceiling re-mints. A 401 drops it and re-mints. Every mint, re-check and denial writes one ``zammad_mcp.audit``
line, without the token or the email.

Synthetic identities are refused before any request, because the first
request of any kind would create that user in Zammad. Every other failure
fails closed: there is no fallback to another token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from zammad_mcp.client.errors import (
    AuthError,
    PermissionDenied,
    ZammadError,
    ZammadTransportError,
    error_detail,
    error_for_status,
)
from zammad_mcp.client.http import USER_AGENT, _cookieless_jar, is_valid_token
from zammad_mcp.platform.ceiling import (
    CeilingError,
    CeilingNarrower,
    ceiling_fingerprint,
    ceiling_for,
    narrow_ceiling,
    validate_permissions,
)
from zammad_mcp.platform.identity import Identity
from zammad_mcp.platform.storage import KeyValue
from zammad_mcp.tiers import Tier, tier_for

logger = logging.getLogger(__name__)
audit = logging.getLogger("zammad_mcp.audit")

DEVICE_FINGERPRINT = "zammad-mcp-server-device-v1"
TOKEN_NAME_PREFIX = "zammad-mcp"
TOKEN_LIFETIME_DAYS = 9
CACHE_TTL_SECONDS = 6 * 24 * 3600
STALE_MARGIN = timedelta(days=1)
RECHECK_INTERVAL_SECONDS = 4 * 3600
FORBIDDEN_RECHECK_INTERVAL_SECONDS = 5 * 60
CACHE_KEY_PREFIX = "zammad-mcp:token:v1:"
RECHECK_LIMIT_KEY_PREFIX = "zammad-mcp:recheck-limit:v1:"
RECHECK_DUE_KEY_PREFIX = "zammad-mcp:recheck-due:v1:"
CACHE_SALT = "zammad-mcp-token-cache"
SESSION_COOKIE_PREFIX = "_zammad_session_"
SIGNOUT_PATH = "/signout"
DEFAULT_NAMESPACE = "local"
CUSTOMER_TOKEN_ACCESS_DENIED = "user authorization failed."
DEFAULT_EMAIL_HEADER = "X-Auth-Request-Email"
DEFAULT_ACCESS_TOKEN_HEADER = "X-Auth-Request-Access-Token"

Clock = Callable[[], float]


class SyntheticIdentityError(ZammadError):
    def __init__(self) -> None:
        super().__init__(
            "your account has no verified email address yet; verify your email in the portal first, "
            "then reconnect this MCP server"
        )


class CustomerTokenAccessError(ZammadError):
    def __init__(self) -> None:
        super().__init__(
            "Customer token access is not enabled on this Zammad server (HTTP 403: User authorization failed.); "
            "a Zammad admin must grant user_preferences.access_token to the Customer role"
        )


class EmptyCeilingError(ZammadError):
    def __init__(self) -> None:
        super().__init__(
            "your Zammad role holds none of the permissions this server uses "
            "(ticket.agent, ticket.customer, knowledge_base.reader, knowledge_base.editor)"
        )


class MintError(ZammadError):
    """Zammad did not hand out a usable token."""


class TokenCacheError(ZammadError):
    """The token cache could not be read or written; the request fails closed."""


@dataclass(frozen=True)
class MintedToken:
    token: str = field(repr=False)
    permissions: frozenset[str]
    tier: Tier
    ceiling_fingerprint: str
    expires_at: date
    checked_at: float
    name: str = ""

    @property
    def stale_at(self) -> float:
        expiry = datetime.combine(self.expires_at, datetime.min.time(), tzinfo=UTC)
        return (expiry - STALE_MARGIN).timestamp()

    def stale(self, now: float) -> bool:
        return now >= self.stale_at

    def due_for_recheck(self, now: float) -> bool:
        return now - self.checked_at >= RECHECK_INTERVAL_SECONDS

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "permissions": sorted(self.permissions),
                "tier": self.tier.name.lower(),
                "fingerprint": self.ceiling_fingerprint,
                "expires_at": self.expires_at.isoformat(),
                "checked_at": self.checked_at,
                "name": self.name,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> MintedToken:
        data = json.loads(raw)
        return cls(
            token=str(data["token"]),
            permissions=frozenset(data["permissions"]),
            tier=Tier[str(data["tier"]).upper()],
            ceiling_fingerprint=str(data["fingerprint"]),
            expires_at=date.fromisoformat(data["expires_at"]),
            checked_at=float(data["checked_at"]),
            name=str(data.get("name", "")),
        )


def cache_ttl_seconds(minted: MintedToken, now: float) -> int:
    """Six days, cut short so the entry never outlives its stale point."""
    return max(1, min(CACHE_TTL_SECONDS, int(minted.stale_at - now)))


def today_utc(now: float) -> date:
    return datetime.fromtimestamp(now, UTC).date()


def token_expiry(now: float) -> date:
    return today_utc(now) + timedelta(days=TOKEN_LIFETIME_DAYS)


@dataclass(frozen=True)
class CacheEntry:
    minted: MintedToken | None
    recheck_flagged: bool = False

    def due_for_recheck(self, now: float) -> bool:
        """Four hours since the last check, or a 403 flagged a re-check that has not run yet."""
        if self.minted is None:
            return False
        return self.recheck_flagged or self.minted.due_for_recheck(now)


class TokenCache:
    """Minted tokens and their re-check flags, under one deployment's namespace.

    A 403 sets two keys: a limiter (``SET NX EX 300``) so it fires at most once
    per five minutes, and a "due" flag that stays until a re-check or mint
    clears it. Neither carries a timestamp, so replicas with skewed clocks
    agree.
    """

    def __init__(self, store: KeyValue, fernet: Fernet, *, namespace: str = DEFAULT_NAMESPACE) -> None:
        self._store = store
        self._fernet = fernet
        self._namespace = namespace

    def token_key(self, identity_key: str) -> str:
        return f"{CACHE_KEY_PREFIX}{self._namespace}:{identity_key}"

    def limit_key(self, identity_key: str) -> str:
        return f"{RECHECK_LIMIT_KEY_PREFIX}{self._namespace}:{identity_key}"

    def due_key(self, identity_key: str) -> str:
        return f"{RECHECK_DUE_KEY_PREFIX}{self._namespace}:{identity_key}"

    async def load(self, identity_key: str) -> CacheEntry:
        try:
            raw, due = await self._store.get_many([self.token_key(identity_key), self.due_key(identity_key)])
        except Exception as exc:
            raise TokenCacheError("the Zammad token cache is unavailable; try again shortly") from exc
        return CacheEntry(self._decode(raw), recheck_flagged=due is not None)

    def _decode(self, raw: str | None) -> MintedToken | None:
        if not raw:
            return None
        try:
            return MintedToken.from_json(self._fernet.decrypt(raw.encode()).decode())
        except (InvalidToken, ValueError, KeyError, TypeError):
            logger.warning("discarding an unreadable token cache entry; minting a new token")
            return None

    async def put(self, identity_key: str, minted: MintedToken, now: float) -> None:
        value = self._fernet.encrypt(minted.to_json().encode()).decode()
        try:
            await self._store.set(self.token_key(identity_key), value, cache_ttl_seconds(minted, now))
        except Exception as exc:
            raise TokenCacheError("the Zammad token cache is unavailable; try again shortly") from exc

    async def drop(self, identity_key: str) -> None:
        try:
            await self._store.delete(self.token_key(identity_key))
        except Exception:
            logger.warning("could not drop a token cache entry", exc_info=True)

    async def request_recheck(self, identity_key: str) -> bool:
        """Flag the token for a re-check; false while the limiter from an earlier 403 is still running."""
        try:
            if not await self._store.set_if_absent(
                self.limit_key(identity_key), "1", FORBIDDEN_RECHECK_INTERVAL_SECONDS
            ):
                return False
            await self._store.set(self.due_key(identity_key), "1", CACHE_TTL_SECONDS)
        except Exception as exc:
            raise TokenCacheError("the Zammad token cache is unavailable; try again shortly") from exc
        return True

    async def clear_recheck(self, identity_key: str) -> None:
        """Best effort: a flag left behind only costs one more re-check."""
        try:
            await self._store.delete(self.due_key(identity_key))
        except Exception:
            logger.warning("could not clear a re-check flag", exc_info=True)


@dataclass(frozen=True)
class SessionGrant:
    """What the header session's ``GET /user_access_token`` hands back."""

    cookie: str = field(repr=False)
    csrf_token: str = field(repr=False)
    permissions: tuple[str, ...]
    tokens: tuple[Mapping[str, Any], ...]


def session_cookie(response: httpx.Response) -> str | None:
    """``name=value`` of Zammad's session cookie, whatever its per-install suffix."""
    for header in response.headers.get_list("set-cookie"):
        pair = header.split(";", 1)[0].strip()
        if pair.startswith(SESSION_COOKIE_PREFIX) and "=" in pair:
            return pair
    return None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise MintError(
            f"Zammad answered the token request with something other than JSON (HTTP {response.status_code})"
        ) from exc
    if not isinstance(body, dict):
        raise MintError("Zammad answered the token request with an unexpected shape")
    return body


def _permission_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Mapping) and entry.get("active", True) is not False and isinstance(entry.get("name"), str):
        return entry["name"]
    return None


def permission_names(raw: Any) -> tuple[str, ...]:
    """Names of the active permissions; Zammad lists them as objects (``{"name": ..., "active": ...}``)."""
    if not isinstance(raw, list):
        return ()
    return tuple(name for name in map(_permission_name, raw) if name is not None)


def bootstrap_error(response: httpx.Response) -> ZammadError:
    """Tell a Customer without token access apart from the SSO middleware's own 403s."""
    detail = error_detail(response)
    if response.status_code == 403 and detail.strip().lower() == CUSTOMER_TOKEN_ACCESS_DENIED:
        return CustomerTokenAccessError()
    return error_for_status(response.status_code, detail)


def _parse_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def expired_mcp_token_ids(tokens: Iterable[Mapping[str, Any]], now: float) -> list[int]:
    """Ids of this server's own tokens whose expiry has passed; live or undated tokens never qualify."""
    moment = datetime.fromtimestamp(now, UTC)
    expired: list[int] = []
    for token in tokens:
        name, token_id, expiry = token.get("name"), token.get("id"), _parse_expiry(token.get("expires_at"))
        if not (isinstance(name, str) and name.startswith(TOKEN_NAME_PREFIX)):
            continue
        if isinstance(token_id, int) and not isinstance(token_id, bool) and expiry is not None and expiry <= moment:
            expired.append(token_id)
    return expired


class TokenSession:
    """One user's header session on one fresh httpx client; used for a single bootstrap."""

    def __init__(self, client: httpx.AsyncClient, *, email_header: str, access_token_header: str) -> None:
        self._client = client
        self._email_header = email_header
        self._access_token_header = access_token_header
        self._opened: tuple[str, str] | None = None

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ZammadTransportError(f"could not reach Zammad to mint a token ({type(exc).__name__})") from exc

    async def open(self, identity: Identity) -> SessionGrant:
        headers = {self._email_header: identity.email}
        if identity.access_token:
            headers[self._access_token_header] = identity.access_token
        response = await self._send("GET", "/user_access_token", headers=headers)
        cookie, csrf = session_cookie(response), response.headers.get("csrf-token", "").strip()
        if cookie:
            self._opened = (cookie, csrf)
        if not response.is_success:
            raise bootstrap_error(response)
        body = _json_object(response)
        if not cookie or not csrf:
            raise MintError("Zammad did not open a session for the token request; is its SSO middleware enabled?")
        permissions = body.get("permissions")
        tokens = body.get("tokens")
        return SessionGrant(
            cookie=cookie,
            csrf_token=csrf,
            permissions=permission_names(permissions),
            tokens=tuple(t for t in tokens if isinstance(t, Mapping)) if isinstance(tokens, list) else (),
        )

    @staticmethod
    def _session_headers(grant: SessionGrant) -> dict[str, str]:
        return {"Cookie": grant.cookie, "X-CSRF-Token": grant.csrf_token}

    async def create(self, grant: SessionGrant, *, name: str, permissions: list[str], expires_at: date) -> str:
        body = {"name": name, "permission": permissions, "expires_at": expires_at.isoformat()}
        response = await self._send("POST", "/user_access_token", json=body, headers=self._session_headers(grant))
        if not response.is_success:
            raise MintError(f"Zammad refused to create a token (HTTP {response.status_code}: {error_detail(response)})")
        token = _json_object(response).get("token")
        if not isinstance(token, str) or not token or not is_valid_token(token):
            raise MintError("Zammad created a token but did not return a usable value")
        return token

    async def sign_out(self) -> bool:
        """Best effort: end the web session the bootstrap opened, whatever happened in between."""
        if self._opened is None:
            return False
        cookie, csrf = self._opened
        headers = {"Cookie": cookie, **({"X-CSRF-Token": csrf} if csrf else {})}
        try:
            response = await self._send("DELETE", SIGNOUT_PATH, headers=headers)
        except ZammadError:
            logger.warning("could not sign the token bootstrap session out of Zammad")
            return False
        if not response.is_success:
            logger.warning("Zammad refused to sign the token bootstrap session out (HTTP %d)", response.status_code)
        return response.is_success

    async def delete_expired(self, grant: SessionGrant, now: float) -> int:
        deleted = 0
        for token_id in expired_mcp_token_ids(grant.tokens, now):
            try:
                response = await self._send(
                    "DELETE", f"/user_access_token/{token_id}", headers=self._session_headers(grant)
                )
            except ZammadError:
                logger.warning("could not delete an expired zammad-mcp token", exc_info=True)
                continue
            if response.is_success:
                deleted += 1
            else:
                logger.warning("Zammad refused to delete an expired zammad-mcp token (HTTP %d)", response.status_code)
        return deleted


class TokenEndpoint:
    """Opens :class:`TokenSession` objects against ``{internal}/api/v1``, each on its own client."""

    def __init__(
        self,
        api_base_url: str,
        *,
        timeout: httpx.Timeout,
        transport: httpx.AsyncBaseTransport | None = None,
        email_header: str = DEFAULT_EMAIL_HEADER,
        access_token_header: str = DEFAULT_ACCESS_TOKEN_HEADER,
    ) -> None:
        self._base_url = api_base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._email_header = email_header
        self._access_token_header = access_token_header

    @asynccontextmanager
    async def session(self) -> AsyncIterator[TokenSession]:
        client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
            cookies=_cookieless_jar(),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "X-Browser-Fingerprint": DEVICE_FINGERPRINT,
            },
        )
        async with client:
            session = TokenSession(
                client, email_header=self._email_header, access_token_header=self._access_token_header
            )
            try:
                yield session
            finally:
                await session.sign_out()


class KeyedLocks:
    """One asyncio lock per key, dropped once nobody holds or waits on it."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key]
                del self._locks[key]


def _is_denial(error: ZammadError) -> bool:
    return isinstance(error, CustomerTokenAccessError | PermissionDenied | AuthError)


def _audit(event: str, identity: Identity, minted: MintedToken | None = None, **details: str) -> None:
    """One JSON line per token decision: the identity hash, never the email or the token."""
    entry: dict[str, Any] = {"event": f"token.{event}", "identity": identity.short_key, **details}
    if minted is not None:
        entry.update(tier=minted.tier.name.lower(), permissions=sorted(minted.permissions), token_name=minted.name)
    audit.info(json.dumps(entry, sort_keys=True))


class TokenMinter:
    def __init__(
        self,
        endpoint: TokenEndpoint,
        cache: TokenCache,
        *,
        default_email_domain: str | None,
        narrow: CeilingNarrower = narrow_ceiling,
        clock: Clock = time.time,
    ) -> None:
        self._endpoint = endpoint
        self._cache = cache
        self._default_email_domain = default_email_domain
        self._narrow = narrow
        self._clock = clock
        self._locks = KeyedLocks()

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    def _refuse_synthetic(self, identity: Identity) -> None:
        if identity.is_synthetic(self._default_email_domain):
            _audit("refused", identity, reason="synthetic_identity")
            raise SyntheticIdentityError()

    def _usable(self, cached: MintedToken | None) -> bool:
        return cached is not None and not cached.stale(self._clock())

    def _ready(self, entry: CacheEntry) -> bool:
        return self._usable(entry.minted) and not entry.due_for_recheck(self._clock())

    async def token_for(self, identity: Identity) -> MintedToken:
        """The cached token, re-checked or minted when needed."""
        self._refuse_synthetic(identity)
        entry = await self._cache.load(identity.key)
        if self._ready(entry):
            return entry.minted
        async with self._locks.hold(identity.key):
            entry = await self._cache.load(identity.key)
            if self._ready(entry):
                return entry.minted
            if self._usable(entry.minted):
                return await self._recheck(identity, entry.minted)
            return await self._mint(identity, reason="stale" if entry.minted else "first")

    async def renew(self, identity: Identity, rejected_token: str) -> MintedToken:
        """After a 401: mint a replacement, unless another request already has."""
        self._refuse_synthetic(identity)
        async with self._locks.hold(identity.key):
            cached = (await self._cache.load(identity.key)).minted
            if self._usable(cached) and cached.token != rejected_token:
                _audit("renew_reused", identity, cached)
                return cached
            await self._cache.drop(identity.key)
            return await self._mint(identity, reason="rejected")

    async def note_forbidden(self, identity: Identity) -> bool:
        """After a role-gated 403: ask for a re-check, at most once per five minutes; the token is left alone."""
        requested = await self._cache.request_recheck(identity.key)
        if requested:
            _audit("recheck_requested", identity, reason="forbidden")
        return requested

    async def _open(self, session: TokenSession, identity: Identity) -> SessionGrant:
        try:
            return await session.open(identity)
        except ZammadError as error:
            if _is_denial(error):
                await self._cache.drop(identity.key)
                _audit("denied", identity, reason=type(error).__name__)
            raise

    async def _mint(self, identity: Identity, *, reason: str) -> MintedToken:
        async with self._endpoint.session() as session:
            grant = await self._open(session, identity)
            return await self._mint_with(session, grant, identity, reason=reason)

    async def _recheck(self, identity: Identity, cached: MintedToken) -> MintedToken:
        async with self._endpoint.session() as session:
            grant = await self._open(session, identity)
            ceiling = self._ceiling(grant, identity)
            if ceiling and ceiling_fingerprint(ceiling) == cached.ceiling_fingerprint:
                refreshed = replace(cached, checked_at=self._clock())
                await self._cache.put(identity.key, refreshed, self._clock())
                await self._cache.clear_recheck(identity.key)
                _audit("recheck_unchanged", identity, refreshed)
                return refreshed
            return await self._mint_with(session, grant, identity, reason="permissions_changed")

    def _ceiling(self, grant: SessionGrant, identity: Identity) -> frozenset[str]:
        return ceiling_for(grant.permissions, identity.claims, self._narrow)

    async def _mint_with(
        self, session: TokenSession, grant: SessionGrant, identity: Identity, *, reason: str
    ) -> MintedToken:
        ceiling = self._ceiling(grant, identity)
        try:
            permissions = validate_permissions(ceiling)
        except CeilingError as exc:
            await self._cache.drop(identity.key)
            _audit("denied", identity, reason="empty_ceiling")
            raise EmptyCeilingError() from exc
        now = self._clock()
        expires_at = token_expiry(now)
        name = f"{TOKEN_NAME_PREFIX} (auto) {today_utc(now).isoformat()}"
        token = await session.create(grant, name=name, permissions=permissions, expires_at=expires_at)
        minted = MintedToken(
            token=token,
            permissions=frozenset(permissions),
            tier=tier_for(permissions),
            ceiling_fingerprint=ceiling_fingerprint(permissions),
            expires_at=expires_at,
            checked_at=now,
            name=name,
        )
        await self._cache.put(identity.key, minted, now)
        await self._cache.clear_recheck(identity.key)
        _audit("minted", identity, minted, reason=reason)
        await session.delete_expired(grant, now)
        return minted
