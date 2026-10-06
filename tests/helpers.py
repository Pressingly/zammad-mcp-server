from __future__ import annotations

import json
from typing import Any

from starlette.testclient import TestClient

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client: TestClient, path: str, method: str, params: dict[str, Any], headers: dict[str, str] | None = None):
    """One JSON-RPC call against the stateless MCP endpoint; returns the decoded ``result``."""
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = client.post(path, json=message, headers={**MCP_HEADERS, **(headers or {})})
    assert response.status_code == 200, response.text
    data_lines = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    payload = json.loads(data_lines[-1]) if data_lines else response.json()
    return payload["result"]


def tool_names(result: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in result["tools"]}


def tool_text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]
