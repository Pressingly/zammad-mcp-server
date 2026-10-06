from __future__ import annotations

import pytest

from zammad_mcp.config import DEFAULT_HTTP_PORT, ConfigError, Settings
from zammad_mcp.env_flags import env_flag


def test_from_env_reads_all_settings():
    settings = Settings.from_env(
        {
            "ZAMMAD_URL": "https://zammad.test/ ",
            "ZAMMAD_HTTP_TOKEN": "tok",
            "ZAMMAD_TIMEOUT_SECONDS": "12.5",
            "ZAMMAD_CONNECT_TIMEOUT_SECONDS": "3",
            "MCP_HTTP_PORT": "9000",
        }
    )
    assert settings == Settings(
        zammad_url="https://zammad.test",
        http_token="tok",
        timeout_seconds=12.5,
        connect_timeout_seconds=3.0,
        http_port=9000,
    )
    assert settings.api_base_url == "https://zammad.test/api/v1"


def test_from_env_defaults():
    settings = Settings.from_env({"ZAMMAD_URL": "https://zammad.test"})
    assert settings.http_token is None
    assert settings.http_port == DEFAULT_HTTP_PORT


def test_from_env_requires_url():
    with pytest.raises(ConfigError, match="ZAMMAD_URL"):
        Settings.from_env({})


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_from_env_rejects_bad_timeout(value):
    with pytest.raises(ConfigError, match="ZAMMAD_TIMEOUT_SECONDS"):
        Settings.from_env({"ZAMMAD_URL": "https://z.test", "ZAMMAD_TIMEOUT_SECONDS": value})


@pytest.mark.parametrize("value", ["http", "0", "70000"])
def test_from_env_rejects_bad_port(value):
    with pytest.raises(ConfigError, match="MCP_HTTP_PORT"):
        Settings.from_env({"ZAMMAD_URL": "https://z.test", "MCP_HTTP_PORT": value})


@pytest.mark.parametrize(("value", "expected"), [("true ", True), ("ON", True), ("0", False), ("", False)])
def test_env_flag(monkeypatch, value, expected):
    monkeypatch.setenv("ZAMMAD_READ_ONLY", value)
    assert env_flag("ZAMMAD_READ_ONLY") is expected
