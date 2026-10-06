from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from zammad_mcp import __main__ as cli
from zammad_mcp.server import build_http_app, build_server


def test_healthz_returns_ok(settings, fake):
    with TestClient(build_http_app(build_server(settings, transport=fake))) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_endpoint_is_mounted(settings, fake):
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(build_http_app(build_server(settings, transport=fake))) as client:
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
