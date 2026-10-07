"""Platform-mode settings, read once from the environment.

Like :class:`zammad_mcp.config.Settings`, :meth:`PlatformSettings.from_env`
is pure over the mapping it is given. :func:`community_settings` derives the
core settings so that every Zammad call, minted or personal token, goes to
``ZAMMAD_INTERNAL_BASE_URL``: the public host sits behind the SSO proxy,
which rejects API tokens.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from zammad_mcp.config import ConfigError, Settings
from zammad_mcp.env_flags import env_flag
from zammad_mcp.platform.mint import DEFAULT_ACCESS_TOKEN_HEADER, DEFAULT_EMAIL_HEADER

logger = logging.getLogger(__name__)

DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 86400
REFRESH_TOKEN_FALLBACK_SECONDS = 30 * 24 * 60 * 60
DEFAULT_SCOPES = ("openid",)
REQUIRED = (
    "MCP_BASE_URL",
    "COGNITO_USER_POOL_ID",
    "COGNITO_AWS_REGION",
    "OIDC_CLIENT_ID",
    "ZAMMAD_INTERNAL_BASE_URL",
    "DEFAULT_EMAIL_DOMAIN",
)
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _read(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _split(raw: str, *, on_spaces: bool = False) -> tuple[str, ...]:
    parts = raw.replace(",", " ").split() if on_spaces else raw.split(",")
    return tuple(part.strip() for part in parts if part.strip())


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise ConfigError(f"{name}={raw!r} must be a whole number of at least 1")
    return int(raw)


@dataclass(frozen=True)
class PlatformSettings:
    base_url: str
    user_pool_id: str
    aws_region: str
    client_id: str
    internal_url: str
    default_email_domain: str
    client_secret: str = field(default="", repr=False)
    jwt_signing_key: str = field(default="", repr=False)
    storage_url: str = field(default="", repr=False)
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    allowed_redirect_uris: tuple[str, ...] = ()
    access_token_ttl_seconds: int = DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    upstream_auth_url: str = ""
    upstream_token_url: str = ""
    cors_origins: tuple[str, ...] = ("*",)
    log_level: str = "INFO"
    email_header: str = DEFAULT_EMAIL_HEADER
    access_token_header: str = DEFAULT_ACCESS_TOKEN_HEADER
    production: bool = False

    @property
    def key_material(self) -> str:
        """Entropy for the Fernet keys: the client secret when there is one (confidential client)."""
        return self.client_secret or self.jwt_signing_key

    @property
    def redirect_allowlist(self) -> list[str] | None:
        """``None`` lets dynamic client registration accept any redirect URI."""
        return list(self.allowed_redirect_uris) or None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PlatformSettings:
        env = os.environ if environ is None else environ
        missing = [name for name in REQUIRED if not _read(env, name)]
        if missing:
            raise ConfigError("platform mode is missing required settings: " + ", ".join(missing))
        if not _read(env, "OIDC_CLIENT_SECRET") and not _read(env, "MCP_JWT_SIGNING_KEY"):
            raise ConfigError(
                "platform mode needs OIDC_CLIENT_SECRET (confidential client) or MCP_JWT_SIGNING_KEY (public client)"
            )
        if _read(env, "ZAMMAD_HTTP_TOKEN") or env_flag("ZAMMAD_HTTP_SHARED_TOKEN_ROUTE", environ=env):
            raise ConfigError(
                "platform mode never uses a shared token: unset ZAMMAD_HTTP_TOKEN and ZAMMAD_HTTP_SHARED_TOKEN_ROUTE"
            )
        log_level = _read(env, "MCP_LOG_LEVEL").upper()
        return cls(
            base_url=_read(env, "MCP_BASE_URL").rstrip("/"),
            user_pool_id=_read(env, "COGNITO_USER_POOL_ID"),
            aws_region=_read(env, "COGNITO_AWS_REGION"),
            client_id=_read(env, "OIDC_CLIENT_ID"),
            internal_url=_read(env, "ZAMMAD_INTERNAL_BASE_URL").rstrip("/"),
            default_email_domain=_read(env, "DEFAULT_EMAIL_DOMAIN").lower(),
            client_secret=_read(env, "OIDC_CLIENT_SECRET"),
            jwt_signing_key=_read(env, "MCP_JWT_SIGNING_KEY"),
            storage_url=_read(env, "MCP_OAUTH_STORAGE_URL"),
            scopes=_split(_read(env, "MCP_OIDC_SCOPES"), on_spaces=True) or DEFAULT_SCOPES,
            allowed_redirect_uris=_split(_read(env, "MCP_ALLOWED_CLIENT_REDIRECT_URIS")),
            access_token_ttl_seconds=_positive_int(
                env, "MCP_ACCESS_TOKEN_TTL_SECONDS", DEFAULT_ACCESS_TOKEN_TTL_SECONDS
            ),
            upstream_auth_url=_read(env, "COGNITO_UPSTREAM_AUTH_URL"),
            upstream_token_url=_read(env, "COGNITO_UPSTREAM_TOKEN_URL"),
            cors_origins=_split(_read(env, "MCP_ALLOWED_ORIGINS")) or ("*",),
            log_level=log_level if log_level in LOG_LEVELS else "INFO",
            email_header=_read(env, "ZAMMAD_SSO_EMAIL_HEADER") or DEFAULT_EMAIL_HEADER,
            access_token_header=_read(env, "ZAMMAD_SSO_ACCESS_TOKEN_HEADER") or DEFAULT_ACCESS_TOKEN_HEADER,
            production=_read(env, "MCP_ENV").lower() == "production",
        )


def community_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Core settings pointed at the internal URL, with ``ZAMMAD_URL`` (if any) kept for links."""
    env = os.environ if environ is None else environ
    public = _read(env, "ZAMMAD_PUBLIC_URL") or _read(env, "ZAMMAD_URL")
    return Settings.from_env({**env, "ZAMMAD_URL": _read(env, "ZAMMAD_INTERNAL_BASE_URL"), "ZAMMAD_PUBLIC_URL": public})
