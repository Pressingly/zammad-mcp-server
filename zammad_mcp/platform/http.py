"""Platform-mode HTTP entry point.

``zammad-mcp http`` lands here when ``COGNITO_USER_POOL_ID`` is set. It serves:

- ``/mcp`` (and the OAuth routes at ``/``): FastMCP's Cognito provider is the
  only auth layer. It is a full OAuth 2.0 authorization server (dynamic
  client registration, RFC 8414/9728 discovery, ``/authorize``,
  ``/auth/callback``, ``/token``), and every tool call runs with the caller's
  minted Zammad token.
- ``/http/api-key/mcp``, only with ``ZAMMAD_HTTP_API_KEY_ROUTE=true``: the
  community route, where each caller sends their own token as
  ``X-Zammad-Token``. Off by default, because that token is replayed to
  ``ZAMMAD_INTERNAL_BASE_URL``, past the SSO proxy and the corporate-ID gate,
  so a leaver's or leaked token would keep working. Not mounting it is the
  control: a router rule in front can be bypassed with an encoded path
  (``/http%2Fapi-key/mcp``) that the server decodes before routing.

``GET /healthz`` answers ``{"status": "ok"}``. Logs are JSON lines on stderr.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route

from zammad_mcp.client import ZammadClient
from zammad_mcp.config import ConfigError, Settings
from zammad_mcp.confirmations import ConfirmationBackend
from zammad_mcp.credentials import api_key_mount
from zammad_mcp.credentials.header import API_KEY_MOUNT_PATH
from zammad_mcp.platform.cognito import ZammadCognitoProvider
from zammad_mcp.platform.credentials import MintedCredentialProvider, platform_tier_resolver
from zammad_mcp.platform.mint import CACHE_SALT, TokenCache, TokenEndpoint, TokenMinter
from zammad_mcp.platform.settings import REFRESH_TOKEN_FALLBACK_SECONDS, PlatformSettings, community_settings
from zammad_mcp.platform.storage import (
    CONFIRMATION_SALT,
    KeyValue,
    MemoryKeyValue,
    RedisKeyValue,
    ValkeyConfirmationBackend,
    build_oauth_storage,
    build_redis,
    fernet_for,
    parse_storage_url,
)
from zammad_mcp.server import MCP_PATH, build_server, healthz

if TYPE_CHECKING:
    from key_value.aio.protocols.key_value import AsyncKeyValue
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/auth/callback"
LOGGERS = ("fastmcp", "uvicorn", "uvicorn.error", "zammad_mcp")
AUDIT_LOGGER = "zammad_mcp.audit"


def build_cognito_provider(platform: PlatformSettings, oauth_storage: AsyncKeyValue | None) -> ZammadCognitoProvider:
    """The OAuth provider; ``oauth_storage=None`` keeps FastMCP's encrypted file store.

    Cognito user pools ignore RFC 8707 resource indicators, and forwarding
    ``resource`` on ``/authorize`` without echoing it on ``/token`` makes
    Cognito answer ``invalid_grant``, hence ``forward_resource=False``.
    """
    provider = ZammadCognitoProvider(
        user_pool_id=platform.user_pool_id,
        aws_region=platform.aws_region,
        client_id=platform.client_id,
        client_secret=platform.client_secret,
        base_url=platform.base_url,
        redirect_path=CALLBACK_PATH,
        required_scopes=list(platform.scopes),
        allowed_client_redirect_uris=platform.redirect_allowlist,
        forward_resource=False,
        client_storage=oauth_storage,
        jwt_signing_key=platform.jwt_signing_key or None,
        fastmcp_access_token_expiry_seconds=platform.access_token_ttl_seconds,
        fallback_refresh_token_expiry_seconds=REFRESH_TOKEN_FALLBACK_SECONDS,
    )
    if platform.upstream_auth_url:
        provider._upstream_authorization_endpoint = platform.upstream_auth_url
    if platform.upstream_token_url:
        provider._upstream_token_endpoint = platform.upstream_token_url
    return provider


def build_platform_server(
    settings: Settings,
    platform: PlatformSettings,
    *,
    store: KeyValue,
    oauth_storage: AsyncKeyValue | None,
    confirmation_backend: ConfirmationBackend | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    """The Cognito-authenticated server: minted credentials and fail-closed tiers.

    ``confirmation_backend=None`` keeps confirmations in process.
    """
    endpoint = TokenEndpoint(
        settings.api_base_url,
        timeout=httpx.Timeout(settings.timeout_seconds, connect=settings.connect_timeout_seconds),
        transport=transport,
        email_header=platform.email_header,
        access_token_header=platform.access_token_header,
    )
    minter = TokenMinter(
        endpoint,
        TokenCache(store, fernet_for(platform.key_material, CACHE_SALT), namespace=platform.namespace),
        default_email_domain=platform.default_email_domain,
    )
    credentials = MintedCredentialProvider(minter)
    client = ZammadClient(
        settings.api_base_url,
        timeout_seconds=settings.timeout_seconds,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        transport=transport,
        on_rejected=credentials.on_rejected,
    )
    mcp = build_server(
        settings,
        client=client,
        credentials=credentials,
        tier_resolver=platform_tier_resolver(credentials),
        confirmation_backend=confirmation_backend,
        auth=build_cognito_provider(platform, oauth_storage),
    )
    mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=False))
    return mcp


def build_api_key_app(settings: Settings, transport: httpx.AsyncBaseTransport | None) -> StarletteWithLifespan:
    """The community server for ``/http/api-key``: each caller's own ``X-Zammad-Token``."""
    return build_server(settings, transport=transport).http_app(path=MCP_PATH, stateless_http=True)


def served_paths(platform: PlatformSettings) -> tuple[str, ...]:
    api_key_paths = (f"{API_KEY_MOUNT_PATH}{MCP_PATH}",) if platform.http_api_key_route else ()
    return (MCP_PATH, *api_key_paths, "/healthz")


def build_platform_app(
    settings: Settings,
    platform: PlatformSettings,
    *,
    redis: Redis | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """The platform MCP app behind one Starlette app; ``redis=None`` keeps every store in process.

    ``/http/api-key`` is mounted only when ``platform.http_api_key_route`` is set.
    """
    store: KeyValue = RedisKeyValue(redis) if redis is not None else MemoryKeyValue()
    oauth_storage = build_oauth_storage(redis, platform.key_material) if redis is not None else None
    confirmations = (
        ValkeyConfirmationBackend(
            redis, fernet_for(platform.key_material, CONFIRMATION_SALT), namespace=platform.namespace
        )
        if redis is not None
        else None
    )
    platform_app = build_platform_server(
        settings,
        platform,
        store=store,
        oauth_storage=oauth_storage,
        confirmation_backend=confirmations,
        transport=transport,
    ).http_app(path=MCP_PATH, stateless_http=True)
    api_key_apps = [build_api_key_app(settings, transport)] if platform.http_api_key_route else []
    sub_apps = [platform_app, *api_key_apps]

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for sub_app in sub_apps:
                await stack.enter_async_context(sub_app.lifespan(sub_app))
            try:
                yield
            finally:
                if redis is not None:
                    await redis.aclose()

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            *(Mount(API_KEY_MOUNT_PATH, app=api_key_mount(api_key_app)) for api_key_app in api_key_apps),
            Mount("/", app=platform_app),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(platform.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["error"] = {"type": type(record.exc_info[1]).__name__, "message": str(record.exc_info[1])}
        return json.dumps(entry)


def configure_logging(level: str) -> None:
    """JSON on stderr; audit lines (``zammad_mcp.audit``) are kept whatever ``MCP_LOG_LEVEL`` says."""
    for name in LOGGERS:
        target = logging.getLogger(name)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JSONFormatter())
        target.handlers = [handler]
        target.setLevel(level)
        target.propagate = False
    logging.getLogger(AUDIT_LOGGER).setLevel(logging.INFO)


def _log_startup(settings: Settings, platform: PlatformSettings) -> None:
    logger.info(
        "platform mode: pool=%s region=%s client_id=%s base_url=%s scopes=%s redirect_allowlist=%s secret=%s "
        "storage=%s zammad_internal=%s zammad_public=%s",
        platform.user_pool_id,
        platform.aws_region,
        platform.client_id,
        platform.base_url,
        list(platform.scopes),
        platform.redirect_allowlist,
        "set" if platform.client_secret else "unset",
        "valkey" if platform.storage_url else "memory",
        settings.zammad_url,
        settings.browser_url,
    )
    if platform.redirect_allowlist is None:
        logger.warning(
            "MCP_ALLOW_ANY_REDIRECT_URI=true and no MCP_ALLOWED_CLIENT_REDIRECT_URIS: "
            "dynamic client registration accepts any redirect_uri"
        )
    if settings.browser_url == settings.zammad_url:
        logger.warning("ZAMMAD_PUBLIC_URL and ZAMMAD_URL are unset: links in tool results point at the internal URL")
    if not platform.storage_url:
        logger.warning(
            "MCP_OAUTH_STORAGE_URL is unset: OAuth state, minted tokens and confirmations live in this process "
            "and are lost on restart%s",
            " (production)" if platform.production else "",
        )
    _log_api_key_route(platform)
    logger.info("register this callback URL in the Cognito app client: %s%s", platform.base_url, CALLBACK_PATH)


def _log_api_key_route(platform: PlatformSettings) -> None:
    if not platform.http_api_key_route:
        logger.info(
            "personal-token route %s%s is disabled; set ZAMMAD_HTTP_API_KEY_ROUTE=true to serve it",
            API_KEY_MOUNT_PATH,
            MCP_PATH,
        )
        return
    logger.warning(
        "ZAMMAD_HTTP_API_KEY_ROUTE=true: %s%s replays any caller's X-Zammad-Token to ZAMMAD_INTERNAL_BASE_URL, "
        "bypassing the SSO proxy (mPass/oauth2-proxy) and the corporate-ID gate, so a leaver's or leaked personal "
        "token keeps working from wherever this server is reachable",
        API_KEY_MOUNT_PATH,
        MCP_PATH,
    )


def run() -> None:
    try:
        platform = PlatformSettings.from_env()
        settings = community_settings()
        redis = build_redis(parse_storage_url(platform.storage_url)) if platform.storage_url else None
    except (ConfigError, ValueError) as error:
        sys.exit(f"zammad-mcp: {error}")
    configure_logging(platform.log_level)
    _log_startup(settings, platform)
    app = build_platform_app(settings, platform, redis=redis)
    logger.info("serving %s on :%d", ", ".join(served_paths(platform)), settings.http_port)
    uvicorn.run(app, host="0.0.0.0", port=settings.http_port, access_log=False, log_config=None)
