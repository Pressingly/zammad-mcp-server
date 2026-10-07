"""Runtime settings, read once from the environment.

``Settings.from_env`` is pure over the mapping it is given, so tests pass a
dict instead of patching ``os.environ``.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from zammad_mcp.env_flags import env_flag

DEFAULT_HTTP_PORT = 8214
DEFAULT_CONFIRM_TTL_SECONDS = 600
TOOL_MODULES = ("reference", "tickets", "articles", "attachments", "search", "users", "organizations", "tags", "kb")
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


class ConfigError(ValueError):
    """Raised when a required setting is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    zammad_url: str
    http_token: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    http_port: int = DEFAULT_HTTP_PORT
    public_url: str = ""
    read_only: bool = False
    filter_tools_by_role: bool = False
    confirm_ttl_seconds: int = DEFAULT_CONFIRM_TTL_SECONDS
    enabled_modules: frozenset[str] = field(default_factory=lambda: frozenset(TOOL_MODULES))
    http_shared_token_route: bool = False

    @property
    def api_base_url(self) -> str:
        return f"{self.zammad_url}/api/v1"

    @property
    def browser_url(self) -> str:
        return self.public_url or self.zammad_url

    def module_enabled(self, module: str) -> bool:
        return module in self.enabled_modules

    def check_http(self) -> None:
        """Refuse an HTTP setup that would hand ``ZAMMAD_HTTP_TOKEN`` to anyone who can reach ``/mcp``."""
        if self.http_token and not self.http_shared_token_route:
            raise ConfigError(
                "ZAMMAD_HTTP_TOKEN is set in http mode: /mcp would act as that token's owner for anyone who can "
                "reach it. Unset it and have each user send X-Zammad-Token to /http/api-key/mcp, or set "
                "ZAMMAD_HTTP_SHARED_TOKEN_ROUTE=true to accept that"
            )
        if self.http_shared_token_route and not self.http_token:
            raise ConfigError("ZAMMAD_HTTP_SHARED_TOKEN_ROUTE=true needs ZAMMAD_HTTP_TOKEN")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        zammad_url = _read(env, "ZAMMAD_URL").rstrip("/")
        if not zammad_url:
            raise ConfigError("ZAMMAD_URL is required (e.g. https://support.example.com)")
        return cls(
            zammad_url=zammad_url,
            http_token=_read(env, "ZAMMAD_HTTP_TOKEN") or None,
            timeout_seconds=_positive_float(env, "ZAMMAD_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            connect_timeout_seconds=_positive_float(
                env, "ZAMMAD_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS
            ),
            http_port=_port(env, "MCP_HTTP_PORT", DEFAULT_HTTP_PORT),
            public_url=_read(env, "ZAMMAD_PUBLIC_URL").rstrip("/"),
            read_only=env_flag("ZAMMAD_READ_ONLY", environ=env),
            filter_tools_by_role=env_flag("ZAMMAD_FILTER_TOOLS_BY_ROLE", environ=env),
            confirm_ttl_seconds=_positive_int(env, "ZAMMAD_CONFIRM_TTL_SECONDS", DEFAULT_CONFIRM_TTL_SECONDS),
            enabled_modules=frozenset(
                module for module in TOOL_MODULES if env_flag(module_flag(module), default=True, environ=env)
            ),
            http_shared_token_route=env_flag("ZAMMAD_HTTP_SHARED_TOKEN_ROUTE", environ=env),
        )


def module_flag(module: str) -> str:
    return f"ZAMMAD_ENABLE_{module.upper()}"


def _read(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _read(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a finite number greater than zero")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise ConfigError(f"{name}={raw!r} must be a whole number of at least 1")
    return int(raw)


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= 65535:
        raise ConfigError(f"{name}={raw!r} is not a valid TCP port")
    return int(raw)
