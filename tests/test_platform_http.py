from __future__ import annotations

import json
import logging
import sys

import pytest
from fakeredis import FakeAsyncRedis
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from starlette.testclient import TestClient

from tests.conftest import FakeZammad
from tests.helpers import rpc, tool_names
from zammad_mcp import __main__ as cli
from zammad_mcp import platform
from zammad_mcp.config import ConfigError
from zammad_mcp.platform import http as platform_http
from zammad_mcp.platform.settings import (
    DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_TOKEN_FALLBACK_SECONDS,
    PlatformSettings,
    community_settings,
)

ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_test"
DISCOVERY = OIDCConfiguration(
    strict=False,
    issuer=ISSUER,
    authorization_endpoint="https://auth.example.test/oauth2/authorize",
    token_endpoint="https://auth.example.test/oauth2/token",
    jwks_uri=f"{ISSUER}/.well-known/jwks.json",
)
ENV = {
    "MCP_BASE_URL": "https://support-mcp.example.test/",
    "COGNITO_USER_POOL_ID": "ap-southeast-1_test",
    "COGNITO_AWS_REGION": "ap-southeast-1",
    "OIDC_CLIENT_ID": "cognito-app-client",
    "OIDC_CLIENT_SECRET": "cognito-app-secret",
    "ZAMMAD_INTERNAL_BASE_URL": "http://zammad-nginx:8080/",
    "ZAMMAD_URL": "https://support.example.test",
    "DEFAULT_EMAIL_DOMAIN": "AskII.ai",
}


@pytest.fixture(autouse=True)
def discovery(monkeypatch):
    monkeypatch.setattr(OIDCProxy, "get_oidc_configuration", lambda *_args, **_kwargs: DISCOVERY)


def test_settings_read_the_platform_env():
    settings = PlatformSettings.from_env(
        {
            **ENV,
            "MCP_OIDC_SCOPES": "openid, email profile",
            "MCP_ALLOWED_CLIENT_REDIRECT_URIS": "https://claude.ai/api/mcp/auth_callback, http://localhost:*/*",
            "MCP_ACCESS_TOKEN_TTL_SECONDS": "3600",
            "MCP_LOG_LEVEL": "debug",
            "MCP_ALLOWED_ORIGINS": "https://a.test,https://b.test",
            "MCP_ENV": "Production",
            "ZAMMAD_SSO_EMAIL_HEADER": "X-Forwarded-Email",
        }
    )

    assert settings.base_url == "https://support-mcp.example.test"
    assert settings.internal_url == "http://zammad-nginx:8080"
    assert settings.default_email_domain == "askii.ai"
    assert settings.scopes == ("openid", "email", "profile")
    assert settings.redirect_allowlist == ["https://claude.ai/api/mcp/auth_callback", "http://localhost:*/*"]
    assert settings.access_token_ttl_seconds == 3600
    assert settings.log_level == "DEBUG"
    assert settings.cors_origins == ("https://a.test", "https://b.test")
    assert settings.production is True
    assert settings.email_header == "X-Forwarded-Email"
    assert settings.access_token_header == "X-Auth-Request-Access-Token"
    assert settings.key_material == "cognito-app-secret"
    assert "cognito-app-secret" not in repr(settings)


def test_settings_defaults():
    settings = PlatformSettings.from_env(
        {**ENV, "OIDC_CLIENT_SECRET": "", "MCP_JWT_SIGNING_KEY": "k", "MCP_LOG_LEVEL": "x"}
    )

    assert settings.scopes == ("openid",)
    assert settings.redirect_allowlist is None
    assert settings.access_token_ttl_seconds == DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    assert settings.cors_origins == ("*",)
    assert settings.log_level == "INFO"
    assert settings.key_material == "k"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"DEFAULT_EMAIL_DOMAIN": ""}, "DEFAULT_EMAIL_DOMAIN"),
        ({"ZAMMAD_INTERNAL_BASE_URL": " "}, "ZAMMAD_INTERNAL_BASE_URL"),
        ({"OIDC_CLIENT_SECRET": ""}, "MCP_JWT_SIGNING_KEY"),
        ({"ZAMMAD_HTTP_TOKEN": "shared"}, "never uses a shared token"),
        ({"ZAMMAD_HTTP_SHARED_TOKEN_ROUTE": "true"}, "never uses a shared token"),
        ({"MCP_ACCESS_TOKEN_TTL_SECONDS": "0"}, "at least 1"),
    ],
)
def test_settings_refuse_an_unsafe_or_incomplete_env(overrides, message):
    with pytest.raises(ConfigError, match=message):
        PlatformSettings.from_env({**ENV, **overrides})


def test_community_settings_call_the_internal_url_and_link_the_public_one():
    settings = community_settings(ENV)

    assert settings.api_base_url == "http://zammad-nginx:8080/api/v1"
    assert settings.browser_url == "https://support.example.test"
    assert community_settings({**ENV, "ZAMMAD_PUBLIC_URL": "https://p.test"}).browser_url == "https://p.test"


def test_provider_carries_the_session_lifetimes_and_upstream_overrides():
    platform_settings = PlatformSettings.from_env(
        {
            **ENV,
            "MCP_ACCESS_TOKEN_TTL_SECONDS": "7200",
            "COGNITO_UPSTREAM_AUTH_URL": "https://mpass.example.test/authorize",
            "COGNITO_UPSTREAM_TOKEN_URL": "https://mpass.example.test/token",
        }
    )

    provider = platform_http.build_cognito_provider(platform_settings, None)

    assert provider._fallback_refresh_token_expiry_seconds == REFRESH_TOKEN_FALLBACK_SECONDS == 30 * 24 * 3600
    assert provider._fastmcp_access_token_expiry_seconds == 7200
    assert provider._upstream_authorization_endpoint == "https://mpass.example.test/authorize"
    assert provider._upstream_token_endpoint == "https://mpass.example.test/token"


def test_provider_keeps_discovered_endpoints_without_overrides():
    provider = platform_http.build_cognito_provider(PlatformSettings.from_env(ENV), None)

    assert provider._upstream_authorization_endpoint == DISCOVERY.authorization_endpoint
    assert provider._upstream_token_endpoint == DISCOVERY.token_endpoint


@pytest.fixture
def app_parts():
    fake = FakeZammad()
    return community_settings(ENV), PlatformSettings.from_env(ENV), fake


def test_app_serves_health_discovery_and_both_mcp_routes(app_parts):
    settings, platform_settings, fake = app_parts
    app = platform_http.build_platform_app(settings, platform_settings, transport=fake)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["issuer"].startswith("https://support-mcp.example.test")
        unauthenticated = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert unauthenticated.status_code == 401
        listed = rpc(
            client,
            "/http/api-key/mcp",
            "tools/list",
            {},
            headers={"X-Zammad-Token": "personal", "X-Auth-Request-Email": "boss@example.com"},
        )

    assert "get_me" in tool_names(listed)
    assert all("x-auth-request-email" not in request.headers for request in fake.requests)


def test_app_closes_valkey_on_shutdown(app_parts, monkeypatch):
    settings, platform_settings, fake = app_parts
    redis = FakeAsyncRedis(decode_responses=True)
    closed = []
    monkeypatch.setattr(redis, "aclose", lambda: closed.append(True) or _done())

    with TestClient(platform_http.build_platform_app(settings, platform_settings, redis=redis, transport=fake)):
        pass

    assert closed == [True]


async def _done() -> None:
    return None


def test_json_formatter_includes_the_error():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("zammad_mcp", logging.ERROR, __file__, 1, "failed %s", ("x",), sys.exc_info())

    entry = json.loads(platform_http.JSONFormatter().format(record))

    assert entry["message"] == "failed x"
    assert entry["level"] == "ERROR"
    assert entry["error"] == {"type": "ValueError", "message": "boom"}


def test_configure_logging_installs_one_json_handler(monkeypatch):
    for name in platform_http.LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "handlers", [])
    platform_http.configure_logging("WARNING")
    platform_http.configure_logging("WARNING")

    target = logging.getLogger("zammad_mcp")
    assert logging.getLogger(platform_http.AUDIT_LOGGER).getEffectiveLevel() == logging.INFO
    assert len(target.handlers) == 1
    assert isinstance(target.handlers[0].formatter, platform_http.JSONFormatter)
    assert target.level == logging.WARNING


@pytest.fixture
def platform_env(monkeypatch):
    for key in ("ZAMMAD_HTTP_TOKEN", "ZAMMAD_HTTP_SHARED_TOKEN_ROUTE", "MCP_OAUTH_STORAGE_URL", "MCP_JWT_SIGNING_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(platform_http, "configure_logging", lambda level: None)


def test_run_serves_the_platform_app(monkeypatch, platform_env):
    monkeypatch.setenv("MCP_OAUTH_STORAGE_URL", "valkey://valkey:6379/15")
    monkeypatch.setenv("MCP_HTTP_PORT", "9214")
    calls = []
    monkeypatch.setattr(platform_http.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    platform_http.run()

    ((app, kwargs),) = calls
    assert kwargs["port"] == 9214
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/healthz", "/http/api-key", ""} <= paths


def test_run_exits_on_a_bad_env(monkeypatch, platform_env):
    monkeypatch.setenv("MCP_OAUTH_STORAGE_URL", "http://nope")

    with pytest.raises(SystemExit, match="scheme"):
        platform_http.run()


def test_enabled_reads_the_pool_id():
    assert platform.enabled({"COGNITO_USER_POOL_ID": "pool"}) is True
    assert platform.enabled({"COGNITO_USER_POOL_ID": "  "}) is False
    assert platform.enabled({}) is False


def test_main_hands_http_to_platform_mode(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    runs = []
    monkeypatch.setattr(platform_http, "run", lambda: runs.append("platform"))

    cli.main(["http"])

    assert runs == ["platform"]


def test_main_keeps_stdio_in_community_mode(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    monkeypatch.setenv("ZAMMAD_URL", "https://zammad.test")
    runs = []
    monkeypatch.setattr("fastmcp.FastMCP.run", lambda self, *a, **k: runs.append(self.name))

    cli.main(["stdio"])

    assert runs == ["zammad"]


def test_startup_warns_when_links_would_point_at_the_internal_url(caplog):
    env = {key: value for key, value in ENV.items() if key != "ZAMMAD_URL"}

    with caplog.at_level(logging.WARNING, logger="zammad_mcp"):
        platform_http._log_startup(community_settings(env), PlatformSettings.from_env(env))

    assert "point at the internal URL" in caplog.text
    assert "MCP_ALLOWED_CLIENT_REDIRECT_URIS is unset" in caplog.text
