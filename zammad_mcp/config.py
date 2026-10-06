"""Runtime settings, read once from the environment.

``Settings.from_env`` is pure over the mapping it is given, so tests pass a
dict instead of patching ``os.environ``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_HTTP_PORT = 8214
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

    @property
    def api_base_url(self) -> str:
        return f"{self.zammad_url}/api/v1"

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
        )


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
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if not raw:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ConfigError(f"{name}={raw!r} is not a valid TCP port")
    return int(raw)
