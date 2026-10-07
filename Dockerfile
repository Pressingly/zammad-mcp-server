FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra platform --no-install-project

COPY zammad_mcp/ ./zammad_mcp/
RUN uv sync --frozen --no-dev --extra platform

RUN groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid mcp --home-dir /app --no-create-home mcp \
    && chown -R mcp:mcp /app
USER mcp

EXPOSE 8214
ENV MCP_HTTP_PORT=8214

ENTRYPOINT ["python", "-m", "zammad_mcp"]
CMD ["http"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=10 \
    CMD curl -fsS "http://127.0.0.1:${MCP_HTTP_PORT:-8214}/healthz" || exit 1
