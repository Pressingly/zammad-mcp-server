# Zammad MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for the [Zammad](https://zammad.org) helpdesk.
It lets Claude, or any MCP client, work with Zammad tickets using **the caller's own Zammad permissions**: every
request carries a per-user API token, never a shared admin token, so Zammad itself decides what each user can see
and change.

Status: early bootstrap. The server runs and exposes one smoke tool, `get_me`. The ticket, article, search, user,
organization, tag and knowledge-base tools land next.

## Modes

### Community mode (available now)

Works against any Zammad 6.x or 7.x with a personal API token (Zammad: avatar menu, Profile, Token Access).

- **stdio** for a local MCP client: `zammad-mcp stdio`
- **Streamable HTTP** on `/mcp`, with `GET /healthz` for health checks: `zammad-mcp http`

Both read the token from `ZAMMAD_HTTP_TOKEN`. A per-request `X-Zammad-Token` header for multi-user HTTP is coming.

### Platform mode (coming)

For deployments that put Zammad behind an SSO edge: users sign in with OAuth (AWS Cognito), and the server mints and
caches a short-lived Zammad token for each user, bounded by that user's Zammad role. It is installed with the
optional `[platform]` extra and enabled by `COGNITO_USER_POOL_ID`. Community mode never imports it.

## Quick start

```bash
uv sync
export ZAMMAD_URL=https://support.example.com
export ZAMMAD_HTTP_TOKEN=<your personal token>
uv run zammad-mcp stdio
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "zammad": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Pressingly/zammad-mcp-server", "zammad-mcp", "stdio"],
      "env": { "ZAMMAD_URL": "https://support.example.com", "ZAMMAD_HTTP_TOKEN": "<token>" }
    }
  }
}
```

Docker (HTTP on port 8214):

```bash
docker build -t zammad-mcp .
docker run --rm -p 8214:8214 -e ZAMMAD_URL=https://support.example.com -e ZAMMAD_HTTP_TOKEN=<token> zammad-mcp
curl http://localhost:8214/healthz
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ZAMMAD_URL` | required | Base URL of the Zammad instance, without `/api/v1` |
| `ZAMMAD_HTTP_TOKEN` | unset | Personal API token used in community mode |
| `ZAMMAD_TIMEOUT_SECONDS` | `30` | Total timeout for one Zammad request |
| `ZAMMAD_CONNECT_TIMEOUT_SECONDS` | `10` | Connect timeout for one Zammad request |
| `MCP_HTTP_PORT` | `8214` | Listen port in `http` mode |

## Tools

| Tool | What it does |
|---|---|
| `get_me` | Returns the Zammad user the token belongs to: id, login, name, email, roles |

Every tool declares all four MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`)
and returns an `Error: ...` string instead of raising, so the model sees what went wrong.

## Security notes

- **Ticket content is untrusted.** Ticket titles, articles, attachments and customer names are written by whoever
  emailed or filled in the form. A crafted ticket can try to instruct the model ("forward this ticket to ...").
  Treat everything the server returns as data. Tools that send email will be disabled by default and need a
  two-step confirmation.
- **Use a per-user token.** Give each person their own Zammad token with only the permissions they need, rather
  than sharing one admin token. Zammad intersects a token's permissions with the user's role, so a token can never
  do more than its owner.
- **Customers need token access enabled.** Stock Zammad lets Agents and Admins create API tokens, but not
  Customers. To let Customers use this server, an admin grants `user_preferences.access_token` to the Customer role
  (Admin, Roles, Customer).
- **The server never sends `X-Auth-Request-*` headers** and never stores cookies, so it cannot impersonate a user
  on an SSO-fronted Zammad or carry one user's session into another user's request.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=zammad_mcp --cov-fail-under=80
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch and commit workflow and [SECURITY.md](SECURITY.md) for
reporting vulnerabilities.

## License

[MIT](LICENSE)
