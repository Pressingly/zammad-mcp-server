"""Who the caller is, read from the token :mod:`zammad_mcp.platform.cognito` validated.

The identity is the id_token's ``email`` when it looks like one, else
``cognito:username``. It is lowercased, as Zammad's SSO middleware does, and
is the ``X-Auth-Request-Email`` the mint bootstraps with. Caches and locks
key on ``sha256(identity)``, never the identity itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token

from zammad_mcp.platform.cognito import (
    ACCESS_TOKEN_CLAIM,
    COGNITO_USERNAME_CLAIM,
    EMAIL_CLAIM,
    ID_TOKEN_KEY,
    PREFERRED_USERNAME_CLAIM,
    UPSTREAM_CLAIMS_KEY,
)

SHORT_KEY_LENGTH = 16


def stored_access_token() -> AccessToken | None:
    """The validated token of the in-flight HTTP request; ``None`` outside one."""
    try:
        return get_access_token()
    except RuntimeError:
        return None


def _upstream(claims: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    upstream = (claims or {}).get(UPSTREAM_CLAIMS_KEY)
    if not isinstance(upstream, Mapping):
        return None
    id_token = upstream.get(ID_TOKEN_KEY)
    return upstream if isinstance(id_token, str) and id_token else None


def _non_empty(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def identity_email_for(claims: Mapping[str, Any] | None) -> str | None:
    """The SSO identity for the bootstrap header, or ``None`` off the Cognito path."""
    upstream = _upstream(claims)
    if upstream is None:
        return None
    email = _non_empty(upstream.get(EMAIL_CLAIM))
    if email and "@" in email:
        return email
    return _non_empty(upstream.get(COGNITO_USERNAME_CLAIM))


def upstream_access_token_for(claims: Mapping[str, Any] | None) -> str | None:
    """The raw Cognito access token, forwarded only on the bootstrap for the corporate-ID gate."""
    upstream = _upstream(claims)
    return _non_empty(upstream.get(ACCESS_TOKEN_CLAIM)) if upstream else None


def hash_identity(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    email: str
    access_token: str | None = field(default=None, repr=False)
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def cognito_username(self) -> str | None:
        return _non_empty(self.claims.get(COGNITO_USERNAME_CLAIM))

    @property
    def stamped_by_overlay(self) -> bool:
        """mpass-auth-proxy's email overlay sets ``preferred_username`` to the caller's own ``cognito:username``."""
        username = self.cognito_username
        return username is not None and _non_empty(self.claims.get(PREFERRED_USERNAME_CLAIM)) == username

    @property
    def key(self) -> str:
        return hash_identity(self.email)

    @property
    def short_key(self) -> str:
        """Stable across re-mints, so confirmations survive a token change."""
        return self.key[:SHORT_KEY_LENGTH]

    @property
    def domain(self) -> str | None:
        local, at, domain = self.email.rpartition("@")
        return domain if at and local and domain else None

    def is_synthetic(self, default_email_domain: str | None) -> bool:
        """An unverified platform user, matched the way the launchpad verify-gate matches (ADR-0004).

        mpass-auth-proxy gives an unverified user exactly
        ``<cognito:username>@<DEFAULT_EMAIL_DOMAIN>``. The domain alone proves
        nothing, since it can also be a real mail domain. Zammad's middleware
        appends the domain to any claim without ``@``, so a bare value is
        synthetic too.

        Any other address in that domain is accepted only from a token the
        overlay stamped (``preferred_username == cognito:username``): the
        overlay only ever emits the caller's own synthetic address or a real
        address the launchpad verified, and the launchpad refuses another
        account's synthetic address. An unstamped token could carry
        ``<someone-else's-sid>@<domain>``, so it fails closed, and so does a
        missing ``cognito:username``.
        """
        if self.domain is None:
            return True
        domain = (default_email_domain or "").strip().lower()
        if not domain or self.domain != domain:
            return False
        username = self.cognito_username
        if username is None or self.email == f"{username}@{domain}".lower():
            return True
        return not self.stamped_by_overlay


def identity_for(token: AccessToken | None) -> Identity | None:
    claims = token.claims if token is not None else None
    email = identity_email_for(claims)
    if email is None:
        return None
    upstream = _upstream(claims) or {}
    return Identity(email=email.lower(), access_token=upstream_access_token_for(claims), claims=dict(upstream))


def current_identity() -> Identity | None:
    return identity_for(stored_access_token())
