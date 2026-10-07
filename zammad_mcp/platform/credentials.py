"""Platform-mode credentials, token renewal and the fail-closed tier resolver.

- :class:`MintedCredentialProvider` implements
  :class:`zammad_mcp.credentials.CredentialProvider` with the caller's minted
  token. ``identity`` is ``sha256(sso identity)[:16]``, stable across
  re-mints, so a confirmation survives a token change.
- :meth:`MintedCredentialProvider.on_rejected` is the steady-state client's
  ``on_rejected`` hook: a 401 re-mints once and the client retries with the
  new token; a role-gated 403 schedules a permission re-check.
- :func:`platform_tier_resolver` decides tool visibility. Unlike the
  community resolver it fails closed: no identity, a mint error or any
  exception yields ``Tier.NONE``, which leaves only the tier-free tools
  (``get_me``) visible so the caller can still read why. Results are
  memoised per identity for 60 seconds, so a ``tools/list`` costs one
  lookup, not one per tool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastmcp.server.auth import AccessToken

from zammad_mcp.client.errors import AuthError, MissingTokenError, PermissionDenied, ZammadAPIError
from zammad_mcp.credentials.base import Profile, ProfileMemo, ZammadCredential
from zammad_mcp.platform.identity import Identity, current_identity, identity_for
from zammad_mcp.platform.mint import MintedToken, TokenMinter
from zammad_mcp.tiers import Tier, TierResolver

logger = logging.getLogger(__name__)

TIER_MEMO_SECONDS = 60.0

IdentitySource = Callable[[], Identity | None]
TokenIdentity = Callable[[AccessToken | None], Identity | None]


def _credential(identity: Identity, minted: MintedToken) -> ZammadCredential:
    return ZammadCredential(
        token=minted.token, identity=identity.short_key, tier=minted.tier, permissions=minted.permissions
    )


def is_role_denial(error: ZammadAPIError) -> bool:
    """A plain 403 from Zammad's permission checks, not the SSO gate, an inactive account or maintenance."""
    return type(error) is PermissionDenied


class MintedCredentialProvider:
    def __init__(self, minter: TokenMinter, *, identity_source: IdentitySource = current_identity) -> None:
        self._minter = minter
        self._identity_source = identity_source

    def _identity(self) -> Identity:
        identity = self._identity_source()
        if identity is None:
            raise MissingTokenError("this request carries no signed-in identity; reconnect and sign in again")
        return identity

    async def resolve(self) -> ZammadCredential:
        identity = self._identity()
        return _credential(identity, await self._minter.token_for(identity))

    async def profile_for(self, identity: Identity) -> Profile:
        minted = await self._minter.token_for(identity)
        return Profile(tier=minted.tier, permissions=minted.permissions)

    async def on_rejected(self, error: ZammadAPIError, token: str) -> str | None:
        """Return a replacement token after a 401; ``None`` means "raise the original error"."""
        identity = self._identity_source()
        if identity is None:
            return None
        if isinstance(error, AuthError):
            return (await self._minter.renew(identity, token)).token
        if is_role_denial(error):
            try:
                await self._minter.note_forbidden(identity)
            except Exception:
                logger.warning("could not schedule a permission re-check after a 403", exc_info=True)
        return None


def platform_tier_resolver(
    credentials: MintedCredentialProvider,
    *,
    memo: ProfileMemo | None = None,
    identity_of: TokenIdentity = identity_for,
) -> TierResolver:
    profiles = memo if memo is not None else ProfileMemo(ttl_seconds=TIER_MEMO_SECONDS)

    async def resolve(token: AccessToken | None) -> Tier:
        try:
            identity = identity_of(token)
            if identity is None:
                return Tier.NONE
            profile = await profiles.get(identity.key, lambda: credentials.profile_for(identity))
        except Exception:
            logger.warning("could not resolve the caller's tier; hiding tier-gated tools", exc_info=True)
            return Tier.NONE
        return profile.tier if profile.tier is not None else Tier.NONE

    return resolve
