"""Command-line entry point: ``zammad-mcp stdio`` or ``zammad-mcp http``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from zammad_mcp import platform
from zammad_mcp.config import ConfigError, Settings
from zammad_mcp.server import build_http_app, build_server

TRANSPORTS = ("stdio", "http")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="zammad-mcp", description="Zammad MCP server")
    parser.add_argument("transport", nargs="?", default="stdio", choices=TRANSPORTS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    transport = parse_args(argv).transport
    if transport == "http" and platform.enabled():
        platform.run()
        return
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        sys.exit(f"zammad-mcp: {error}")
    mcp = build_server(settings)
    if transport == "stdio":
        mcp.run()
        return
    try:
        app = build_http_app(mcp, settings)
    except ConfigError as error:
        sys.exit(f"zammad-mcp: {error}")
    uvicorn.run(app, host="0.0.0.0", port=settings.http_port, access_log=False)


if __name__ == "__main__":
    main()
