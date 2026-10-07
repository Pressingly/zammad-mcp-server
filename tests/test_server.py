from __future__ import annotations

import dataclasses

import pytest
from starlette.testclient import TestClient

from zammad_mcp import __main__ as cli
from zammad_mcp.config import ConfigError
from zammad_mcp.server import build_http_app, build_server


def test_healthz_returns_ok(shared_settings, fake):
    with TestClient(build_http_app(build_server(shared_settings, transport=fake), shared_settings)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_endpoint_is_mounted(shared_settings, fake):
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(build_http_app(build_server(shared_settings, transport=fake), shared_settings)) as client:
        response = client.post("/mcp", json=initialize, headers=headers)
    assert response.status_code == 200
    assert '"serverInfo"' in response.text


@pytest.fixture
def zammad_env(monkeypatch):
    monkeypatch.setenv("ZAMMAD_URL", "https://zammad.test")
    monkeypatch.setenv("MCP_HTTP_PORT", "9123")


def test_main_http_runs_uvicorn_on_configured_port(monkeypatch, zammad_env):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    cli.main(["http"])
    ((app, kwargs),) = calls
    assert kwargs["port"] == 9123
    assert kwargs["host"] == "0.0.0.0"
    assert any(getattr(route, "path", None) == "/healthz" for route in app.routes)


def test_main_defaults_to_stdio(monkeypatch, zammad_env):
    runs = []
    monkeypatch.setattr("fastmcp.FastMCP.run", lambda self, *a, **k: runs.append(self.name))
    cli.main([])
    assert runs == ["zammad"]


def test_main_exits_on_missing_config(monkeypatch):
    monkeypatch.delenv("ZAMMAD_URL", raising=False)
    with pytest.raises(SystemExit, match="ZAMMAD_URL"):
        cli.main(["stdio"])


def test_main_rejects_unknown_transport():
    with pytest.raises(SystemExit):
        cli.main(["sse"])


def test_main_http_refuses_a_static_token_without_the_opt_in(monkeypatch, zammad_env):
    monkeypatch.setenv("ZAMMAD_HTTP_TOKEN", "operator-token")
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit, match="ZAMMAD_HTTP_SHARED_TOKEN_ROUTE"):
        cli.main(["http"])


def test_main_http_serves_the_shared_route_on_opt_in(monkeypatch, zammad_env):
    monkeypatch.setenv("ZAMMAD_HTTP_TOKEN", "operator-token")
    monkeypatch.setenv("ZAMMAD_HTTP_SHARED_TOKEN_ROUTE", "true")
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: calls.append(app))
    cli.main(["http"])
    assert any(getattr(route, "path", None) == "" for route in calls[0].routes)


def test_shared_route_without_a_token_is_refused(settings):
    with pytest.raises(ConfigError, match="needs ZAMMAD_HTTP_TOKEN"):
        build_http_app(
            build_server(settings), dataclasses.replace(settings, http_token=None, http_shared_token_route=True)
        )


def test_stdio_keeps_working_with_a_static_token(monkeypatch, zammad_env):
    monkeypatch.setenv("ZAMMAD_HTTP_TOKEN", "operator-token")
    runs = []
    monkeypatch.setattr("fastmcp.FastMCP.run", lambda self, *a, **k: runs.append(self.name))
    cli.main(["stdio"])
    assert runs == ["zammad"]
