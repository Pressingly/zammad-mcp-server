# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Reporting a bug

Search the existing issues first. A useful report includes:

- the server version or commit, and the mode (stdio, HTTP, platform);
- the Zammad version;
- the MCP client (Claude Desktop, Claude.ai, MCP Inspector, ...);
- the smallest steps that reproduce it, with tokens and personal data removed.

Security issues go through [SECURITY.md](SECURITY.md), not public issues.

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff format .
uv run ruff check .
uv run pytest --cov=zammad_mcp --cov-fail-under=80
```

CI runs the same checks on every pull request. Tests must not need a live Zammad: use `httpx.MockTransport`.

## Branches

| Branch | Role |
|---|---|
| `foss-sandbox` | Default branch and staging. Every change lands here first. |
| `foss-main` | Production. Updated only by a promotion PR from `foss-sandbox`. |

Open your pull request against `foss-sandbox`.

## Commits and pull requests

- Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): subject`,
  imperative, lowercase, no trailing period, e.g. `feat(tools): add search_tickets`.
- Keep pull requests small and focused, with tests for new behaviour.
- Every new tool declares all four MCP annotations, returns an error string instead of raising, and wraps text
  that came from Zammad users as untrusted content.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
