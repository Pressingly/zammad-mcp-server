from __future__ import annotations

import pytest

from zammad_mcp.config import (
    DEFAULT_CONFIRM_TTL_SECONDS,
    DEFAULT_HTTP_PORT,
    TOOL_MODULES,
    ConfigError,
    Settings,
    module_flag,
)
from zammad_mcp.env_flags import env_flag, parse_flag


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


def test_from_env_reads_gates_and_public_url():
    settings = Settings.from_env(
        {
            "ZAMMAD_URL": "https://zammad.internal:8080",
            "ZAMMAD_PUBLIC_URL": "https://support.example.com/",
            "ZAMMAD_READ_ONLY": "true ",
            "ZAMMAD_FILTER_TOOLS_BY_ROLE": "1",
            "ZAMMAD_CONFIRM_TTL_SECONDS": "120",
            "ZAMMAD_ENABLE_ATTACHMENTS": "false",
            "ZAMMAD_ENABLE_SEARCH": "no",
        }
    )
    assert settings.browser_url == "https://support.example.com"
    assert settings.read_only is True
    assert settings.filter_tools_by_role is True
    assert settings.confirm_ttl_seconds == 120
    assert settings.enabled_modules == frozenset(TOOL_MODULES) - {"attachments", "search"}
    assert not settings.module_enabled("search")


def test_gate_defaults():
    settings = Settings.from_env({"ZAMMAD_URL": "https://zammad.test"})
    assert settings.browser_url == "https://zammad.test"
    assert (settings.read_only, settings.filter_tools_by_role) == (False, False)
    assert settings.confirm_ttl_seconds == DEFAULT_CONFIRM_TTL_SECONDS
    assert settings.enabled_modules == frozenset(TOOL_MODULES)


@pytest.mark.parametrize("name", ["ZAMMAD_TIMEOUT_SECONDS", "ZAMMAD_CONNECT_TIMEOUT_SECONDS"])
@pytest.mark.parametrize("value", ["inf", "nan", "-inf", "1e999"])
def test_timeouts_must_be_finite(name, value):
    with pytest.raises(ConfigError, match="finite"):
        Settings.from_env({"ZAMMAD_URL": "https://z.test", name: value})


@pytest.mark.parametrize("value", ["0", "1.5", "abc", "-3", "\u00b2", "inf"])
def test_confirm_ttl_must_be_a_positive_whole_number(value):
    with pytest.raises(ConfigError, match="ZAMMAD_CONFIRM_TTL_SECONDS"):
        Settings.from_env({"ZAMMAD_URL": "https://z.test", "ZAMMAD_CONFIRM_TTL_SECONDS": value})


def test_port_rejects_non_ascii_digits():
    with pytest.raises(ConfigError, match="MCP_HTTP_PORT"):
        Settings.from_env({"ZAMMAD_URL": "https://z.test", "MCP_HTTP_PORT": "\u0668\u0660"})


def test_shared_token_route_flag():
    settings = Settings.from_env(
        {"ZAMMAD_URL": "https://z.test", "ZAMMAD_HTTP_TOKEN": "t", "ZAMMAD_HTTP_SHARED_TOKEN_ROUTE": "true"}
    )
    settings.check_http()
    with pytest.raises(ConfigError, match="ZAMMAD_HTTP_SHARED_TOKEN_ROUTE=true to accept"):
        Settings.from_env({"ZAMMAD_URL": "https://z.test", "ZAMMAD_HTTP_TOKEN": "t"}).check_http()
    Settings.from_env({"ZAMMAD_URL": "https://z.test"}).check_http()


def test_module_flag_names():
    assert module_flag("tickets") == "ZAMMAD_ENABLE_TICKETS"


@pytest.mark.parametrize(
    ("raw", "default", "expected"),
    [(None, True, True), ("  ", True, True), ("off", True, False), ("Yes", False, True), ("maybe", True, False)],
)
def test_parse_flag(raw, default, expected):
    assert parse_flag(raw, default) is expected


def test_env_flag_reads_a_mapping():
    assert env_flag("X", environ={"X": "on"}) is True
    assert env_flag("X", default=True, environ={}) is True
