from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from fastmcp import Client
from starlette.testclient import TestClient

from zammad_mcp.server import build_server

if TYPE_CHECKING:
    import httpx

    from zammad_mcp.config import Settings

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


_FRAME = re.compile(r'^<untrusted_content source="[a-z_]+" id="[^"]*">\n?(.*?)\n?</untrusted_content>$', re.DOTALL)


def unframed(value: Any) -> Any:
    """Strip ``<untrusted_content>`` frames recursively, for comparing shaped output with plain values."""
    if isinstance(value, dict):
        return {key: unframed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unframed(item) for item in value]
    if isinstance(value, str):
        match = _FRAME.match(value)
        return match.group(1) if match else value
    return value


async def tools(settings: Settings, fake: httpx.AsyncBaseTransport) -> dict[str, Any]:
    """The registered tools by name, as an MCP client lists them."""
    async with Client(build_server(settings, transport=fake)) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def call(settings: Settings, fake: httpx.AsyncBaseTransport, name: str, arguments: dict[str, Any] | None = None):
    async with Client(build_server(settings, transport=fake)) as client:
        return await client.call_tool(name, arguments or {})


async def data(
    settings: Settings, fake: httpx.AsyncBaseTransport, name: str, arguments: dict[str, Any] | None = None
) -> Any:
    return (await call(settings, fake, name, arguments)).data
