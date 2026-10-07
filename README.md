# Zammad MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for the [Zammad](https://zammad.org) helpdesk.
It lets Claude, or any MCP client, work with Zammad tickets using **the caller's own Zammad permissions**: every
request carries a per-user API token, never a shared admin token, so Zammad itself decides what each user can see
and change.

Status: Ticket, article, attachment, search, user, organization and tag tools work in stdio and
HTTP. Tag writes, the knowledge base and email replies land next. Platform mode (SSO with per-user minted tokens)
is available.

## Modes

### Community mode (available now)

Works against any Zammad 6.x or 7.x with a personal API token (Zammad: avatar menu, Profile, Token Access).

- **stdio** for a local MCP client: `zammad-mcp stdio`, with the token in `ZAMMAD_HTTP_TOKEN`.
- **Streamable HTTP**: `zammad-mcp http`, with `GET /healthz` for health checks.
  - `/http/api-key/mcp` (always on): every caller sends their own personal token as `X-Zammad-Token`. Requests on
    this path never use `ZAMMAD_HTTP_TOKEN`, and any `X-Auth-Request-*` header (also spelled with `_`) is stripped
    on arrival.
  - `/mcp` (opt-in): serves `ZAMMAD_HTTP_TOKEN` to **anyone who can reach the port**, who then acts in Zammad as
    that token's owner. It is only mounted with `ZAMMAD_HTTP_SHARED_TOKEN_ROUTE=true`; `zammad-mcp http` refuses to
    start with `ZAMMAD_HTTP_TOKEN` set and the flag off. Use it only on a loopback or otherwise private listener.

### Platform mode

For deployments that put Zammad behind an SSO edge with a trusted-header middleware: users sign in with OAuth
(AWS Cognito, optionally through an mPass-style auth proxy), and the server mints and caches a Zammad token for
each user, bounded by that user's Zammad role. It needs the optional `[platform]` extra (included in the Docker
image) and is enabled by `COGNITO_USER_POOL_ID`. Community mode never imports it.

- `/mcp` is guarded by FastMCP's Cognito provider (OAuth 2.0 with dynamic client registration); every call runs
  with the caller's minted token. `/http/api-key/mcp` stays available for personal tokens.
- A token carries an explicit list: `ticket.agent`, `ticket.customer`, `knowledge_base.reader` and
  `knowledge_base.editor`, intersected with the user's role. Never `admin.*`, `report` or `user_preferences.*`.
- Tokens last at least 8 days, are cached for 6 (Fernet-encrypted in Valkey), are re-checked every 4 hours and
  after a permission-gated 403, and are re-minted after a 401. Only expired tokens of this server are cleaned up.
- Unverified users (an identity in `DEFAULT_EMAIL_DOMAIN`, or without `@`) are refused before any Zammad call.
- The tool list is filtered per user and fails closed: if the mint fails, only `get_me` is listed, and it explains
  why.
- Every failure fails closed; there is no fallback to a shared token.

| Variable | Default | Purpose |
|---|---|---|
| `COGNITO_USER_POOL_ID` | required | Turns platform mode on |
| `COGNITO_AWS_REGION` | required | Cognito region |
| `OIDC_CLIENT_ID` | required | Cognito app client id |
| `OIDC_CLIENT_SECRET` or `MCP_JWT_SIGNING_KEY` | one required | Client secret (confidential client) or signing key (public client) |
| `MCP_BASE_URL` | required | Public URL of this server; register `<MCP_BASE_URL>/auth/callback` in Cognito |
| `ZAMMAD_INTERNAL_BASE_URL` | required | Zammad on the internal network; every Zammad call goes here |
| `DEFAULT_EMAIL_DOMAIN` | required | Domain of unverified users, who are refused |
| `MCP_OAUTH_STORAGE_URL` | unset | Valkey URL (`redis://`, `rediss://`, `valkey://`, `valkeys://`) for OAuth state, tokens and confirmations |
| `MCP_ALLOWED_CLIENT_REDIRECT_URIS` | unset (any) | Comma-separated redirect URI patterns for client registration |
| `COGNITO_UPSTREAM_AUTH_URL` / `COGNITO_UPSTREAM_TOKEN_URL` | discovered | Route `/authorize` and `/token` through an auth proxy |
| `MCP_ACCESS_TOKEN_TTL_SECONDS` | `86400` | Lifetime of the token issued to the MCP client |
| `MCP_OIDC_SCOPES` | `openid` | Upstream scopes |
| `MCP_LOG_LEVEL` | `INFO` | JSON log level |

See [docs/platform-mode.md](docs/platform-mode.md) for the mint flow, the cache and every setting.

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

Docker (multi-user HTTP on port 8214; no token in the container, each client sends its own):

```bash
docker build -t zammad-mcp .
docker run --rm -p 8214:8214 -e ZAMMAD_URL=https://support.example.com zammad-mcp
curl http://localhost:8214/healthz
```

Point the MCP client at `http://<host>:8214/http/api-key/mcp` with the header `X-Zammad-Token: <your token>`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ZAMMAD_URL` | required | Base URL the server calls, without `/api/v1` |
| `ZAMMAD_PUBLIC_URL` | `ZAMMAD_URL` | Base URL for the `url` links in results, when users reach Zammad on another host |
| `ZAMMAD_HTTP_TOKEN` | unset | Personal API token for `stdio` (and `/mcp` with the opt-in below) |
| `ZAMMAD_HTTP_SHARED_TOKEN_ROUTE` | `false` | `true` serves `/mcp` in http mode, acting as `ZAMMAD_HTTP_TOKEN`'s owner for every caller |
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

The "Who" column is a convenience: Zammad still decides what each token may do. Callers whose role has no
`ticket.*` permission (for example an Admin-only account) report tier `none`. Article bodies are
cut to 4000 characters with a `truncated` flag and a hint. Search on a Zammad without Elasticsearch is a
case-insensitive substring match with no field syntax or ranking; use the `search_tickets` filters to narrow it.

Every tool declares all four MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`)
and returns an `Error: ...` string instead of raising, so the model sees what went wrong.

## Security notes

- **Ticket content is untrusted.** Ticket titles, articles, attachments and customer names are written by whoever
  emailed or filled in the form. A crafted ticket can try to instruct the model ("forward this ticket to ...").
  The server wraps every such value in an `<untrusted_content source=... id=...>` frame and tells the model to treat
  it as data. Framed: ticket titles and owner, customer and organization names; article subjects, bodies, `from`,
  `to`, `cc` and author; attachment file names and text; user logins, names, emails, phones and organizations;
  organization names and domains; tag names; search hit titles, names and emails; history authors and values.
  Inside a frame every `&` and `<` (and the full-width and small `<` look-alikes) is escaped and invisible format
  characters are removed, so no spelling of a closing tag survives. Not framed: ids, numbers, timestamps, flags,
  roles, and state, priority and group names, which only admins set. Tools that send email will be disabled by
  default and need a two-step confirmation.
- **`/mcp` with a shared token acts as that token's owner.** With `ZAMMAD_HTTP_SHARED_TOKEN_ROUTE=true`, anyone who
  can reach the port works in Zammad as the owner of `ZAMMAD_HTTP_TOKEN`. Prefer `/http/api-key/mcp`.
- **Use a per-user token.** Give each person their own Zammad token with only the permissions they need, rather
  than sharing one admin token. Zammad intersects a token's permissions with the user's role, so a token can never
  do more than its owner.
- **Customers need token access enabled.** Stock Zammad lets Agents and Admins create API tokens, but not
  Customers. To let Customers use this server, an admin grants `user_preferences.access_token` to the Customer role
  (Admin, Roles, Customer).
- **The server never sends `X-Auth-Request-*` headers** (in any case, with `-` or `_`) and never stores cookies, so
  it cannot impersonate a user on an SSO-fronted Zammad or carry one user's session into another user's request.

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
