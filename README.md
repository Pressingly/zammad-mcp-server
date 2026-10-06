# Zammad MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for the [Zammad](https://zammad.org) helpdesk.
It lets Claude, or any MCP client, work with Zammad tickets using **the caller's own Zammad permissions**: every
request carries a per-user API token, never a shared admin token, so Zammad itself decides what each user can see
and change.

Status: community core. Ticket, article, attachment, search, user, organization and tag tools work in stdio and
HTTP. Tag writes, the knowledge base and email replies land next; platform mode (SSO with per-user minted tokens)
after that.

## Modes

### Community mode (available now)

Works against any Zammad 6.x or 7.x with a personal API token (Zammad: avatar menu, Profile, Token Access).

- **stdio** for a local MCP client: `zammad-mcp stdio`
- **Streamable HTTP** on `/mcp`, with `GET /healthz` for health checks: `zammad-mcp http`

- **Multi-user HTTP** on `/http/api-key/mcp`: every caller sends their own personal token as `X-Zammad-Token`.
  Requests on this path never use `ZAMMAD_HTTP_TOKEN`, and any `X-Auth-Request-*` header is stripped on arrival.

`stdio` and `/mcp` read the token from `ZAMMAD_HTTP_TOKEN`.

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
| `ZAMMAD_URL` | required | Base URL the server calls, without `/api/v1` |
| `ZAMMAD_PUBLIC_URL` | `ZAMMAD_URL` | Base URL for the `url` links in results, when users reach Zammad on another host |
| `ZAMMAD_HTTP_TOKEN` | unset | Personal API token for `stdio` and `/mcp` |
| `ZAMMAD_TIMEOUT_SECONDS` | `30` | Total timeout for one Zammad request |
| `ZAMMAD_CONNECT_TIMEOUT_SECONDS` | `10` | Connect timeout for one Zammad request |
| `ZAMMAD_READ_ONLY` | `false` | `true` leaves out every write tool |
| `ZAMMAD_ENABLE_REFERENCE` | `true` | `list_ticket_options` |
| `ZAMMAD_ENABLE_TICKETS` | `true` | Ticket search, read, history, create and update |
| `ZAMMAD_ENABLE_ARTICLES` | `true` | Article list and read, `add_ticket_note` |
| `ZAMMAD_ENABLE_ATTACHMENTS` | `true` | `get_ticket_attachment` |
| `ZAMMAD_ENABLE_SEARCH` | `true` | Global `search` |
| `ZAMMAD_ENABLE_USERS` | `true` | `get_user`, `search_users` |
| `ZAMMAD_ENABLE_ORGANIZATIONS` | `true` | `get_organization`, `search_organizations` |
| `ZAMMAD_ENABLE_TAGS` | `true` | `list_ticket_tags` |
| `ZAMMAD_FILTER_TOOLS_BY_ROLE` | `false` | Hide agent-only tools from customer tokens in the tool list (Zammad enforces permissions either way) |
| `ZAMMAD_CONFIRM_TTL_SECONDS` | `600` | Lifetime of a two-step confirmation token (used by the coming email and merge tools) |
| `MCP_HTTP_PORT` | `8214` | Listen port in `http` mode |

A disabled module's tools are never registered. Flags accept `true`, `1`, `yes` or `on`, with surrounding spaces
ignored.

## Tools

| Tool | Who | What it does |
|---|---|---|
| `get_me` | all | The Zammad user the token belongs to, with their tier |
| `list_ticket_options` | all | Active states, priorities and groups, with ids |
| `search_tickets` | all | Text search plus exact filters (state, priority, group, customer, owner, organization), paged |
| `get_ticket` | all | One ticket by id or `#number`, with its newest articles |
| `list_ticket_articles` | all | A ticket's articles, newest first, paged, bodies as plain text |
| `get_ticket_article` | all | One article, with a configurable body length |
| `get_ticket_attachment` | all | Text attachments up to 200 KB as text, images up to 2 MB as images |
| `search` | all | Zammad's global search across tickets, users and organizations |
| `get_user` | all | One user (customers: themselves and their organization) |
| `get_organization` | all | One organization (customers: their own) |
| `list_ticket_tags` | all | The tags on a ticket |
| `create_ticket` | all | A new ticket with a first public message; agents name the customer |
| `update_ticket` | all | Title, state, priority, group, owner or pending time |
| `add_ticket_note` | all | Agents: an internal note by default; customers: a public reply. Never sends email |
| `search_users` | agents | Users by name, login, email or phone |
| `search_organizations` | agents | Organizations by name or domain |
| `get_ticket_history` | agents | A ticket's change history, newest first |

The "Who" column is a convenience: Zammad still decides what each token may do. Article bodies are
cut to 4000 characters with a `truncated` flag and a hint. Search on a Zammad without Elasticsearch is a
case-insensitive substring match with no field syntax or ranking; use the `search_tickets` filters to narrow it.

Every tool declares all four MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`)
and returns an `Error: ...` string instead of raising, so the model sees what went wrong.

## Security notes

- **Ticket content is untrusted.** Ticket titles, articles, attachments and customer names are written by whoever
  emailed or filled in the form. A crafted ticket can try to instruct the model ("forward this ticket to ...").
  The server wraps that text in `<untrusted_content>` blocks (escaping any tag inside it) and tells the model to
  treat it as data. Tools that send email will be disabled by default and need a
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
