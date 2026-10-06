from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from zammad_mcp.config import Settings

Handler = Callable[[httpx.Request], httpx.Response]


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
