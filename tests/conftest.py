from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from zammad_mcp.config import Settings

Handler = Callable[[httpx.Request], httpx.Response]

AGENT_ME = {
    "id": 7,
    "login": "agent@example.com",
    "firstname": "Ada",
    "lastname": "Agent",
    "email": "agent@example.com",
    "roles": ["Agent"],
    "role_ids": [2],
    "organization": "Example",
    "active": True,
    "preferences": {"locale": "en-us"},
}
CUSTOMER_ME = {**AGENT_ME, "id": 9, "login": "cara@example.com", "roles": ["Customer"], "role_ids": [3]}
ROLES = {
    2: {"id": 2, "name": "Agent", "permissions": ["ticket.agent", "user_preferences"]},
    3: {"id": 3, "name": "Customer", "permissions": ["ticket.customer", "user_preferences"]},
}


@pytest.fixture
def settings() -> Settings:
    return Settings(zammad_url="https://zammad.test", http_token="secret-token")


class RecordingTransport(httpx.MockTransport):
    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(record)


@pytest.fixture
def recording_transport() -> Callable[[Handler], RecordingTransport]:
    return RecordingTransport


Route = httpx.Response | Callable[[httpx.Request], httpx.Response] | Any


class FakeZammad(RecordingTransport):
    """A routed fake: ``fake.on("GET", "/tickets/1", json={...})``; unknown routes answer 404."""

    def __init__(self, me: dict[str, Any] | None = None) -> None:
        self.routes: dict[tuple[str, str], Route] = {}
        super().__init__(self._dispatch)
        self.on("GET", "/users/me", json=me or AGENT_ME)
        for role_id, role in ROLES.items():
            self.on("GET", f"/roles/{role_id}", json=role)

    def on(self, method: str, path: str, *, json: Any = None, status: int = 200, handler: Handler | None = None):
        self.routes[(method, f"/api/v1{path}")] = handler or httpx.Response(status, json=json)
        return self

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": "Not Found"})
        if callable(route):
            return route(request)
        return httpx.Response(route.status_code, content=route.content, headers=route.headers)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == f"/api/v1{path}"]

    def last_json(self, method: str, path: str) -> Any:
        return json.loads(self.calls(method, path)[-1].content)


@pytest.fixture
def fake() -> FakeZammad:
    return FakeZammad()


@pytest.fixture
def customer_fake() -> FakeZammad:
    return FakeZammad(me=CUSTOMER_ME)
