# Zammad MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for the [Zammad](https://zammad.org) helpdesk.
It lets Claude, or any MCP client, work with Zammad tickets using **the caller's own Zammad permissions**: every
request carries a per-user API token, never a shared admin token, so Zammad itself decides what each user can see
and change.

Status: community core. Ticket, article, attachment, search, user, organization, tag and knowledge base tools
work in stdio and HTTP, and email replies and macros are available behind opt-in flags. Platform mode (SSO with
per-user minted tokens) lands next.

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
| `ZAMMAD_ENABLE_TAGS` | `true` | `list_ticket_tags`, `add_ticket_tag`, `remove_ticket_tag` |
| `ZAMMAD_ENABLE_KB` | `true` | `search_knowledge_base`, `get_kb_answer` |
| `ZAMMAD_ENABLE_EMAIL_REPLIES` | `false` | `prepare_email_reply`, `send_email_reply`. Needs an outgoing email channel in Zammad |
| `ZAMMAD_EMAIL_ALLOW_ANY_RECIPIENT` | `false` | `true` lets email replies go to any address, not only the ticket's participants (never to Zammad's own addresses) |
| `ZAMMAD_ENABLE_MACROS` | `false` | `list_macros`, `prepare_apply_macro`, `apply_macro` |
| `ZAMMAD_FILTER_TOOLS_BY_ROLE` | `false` | Hide agent-only tools from customer tokens in the tool list (Zammad enforces permissions either way) |
| `ZAMMAD_CONFIRM_TTL_SECONDS` | `600` | Lifetime of a two-step confirmation token (email replies and macros) |
| `MCP_HTTP_PORT` | `8214` | Listen port in `http` mode |

A disabled module's tools are never registered, and `ZAMMAD_READ_ONLY=true` leaves out every tool that changes
Zammad, including the email and macro tools. Flags accept `true`, `1`, `yes` or `on`, with surrounding spaces
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
| `search_knowledge_base` | all | Knowledge base answers with a snippet; customers find published answers only |
| `get_kb_answer` | all | One knowledge base answer, every translation's title and plain-text body |
| `create_ticket` | all | A new ticket with a first public message; agents name the customer |
| `update_ticket` | all | Title, state, priority, group, owner or pending time |
| `add_ticket_note` | all | Agents: an internal note by default; customers: a public reply. Never sends email |
| `search_users` | agents | Users by name, login, email or phone |
| `search_organizations` | agents | Organizations by name or domain |
| `get_ticket_history` | agents | A ticket's change history, newest first |
| `add_ticket_tag` | agents | Add a tag; a new tag needs Zammad's `tag_new` setting |
| `remove_ticket_tag` | agents | Remove a tag |
| `prepare_email_reply` | agents, opt-in | Check the group's outgoing address, settle recipients and attachments, return a preview and a confirmation token. Sends nothing |
| `send_email_reply` | agents, opt-in | Send the approved reply as one email article; returns the article id and `queued` |
| `list_macros` | agents, opt-in | Active macros, with the groups each is limited to |
| `prepare_apply_macro` | agents, opt-in | Preview a macro on up to 50 tickets and return a confirmation token. Changes nothing |
| `apply_macro` | agents, opt-in | Apply the approved macro through `POST /tickets/mass_macro` |

The "Who" column is a convenience: Zammad still decides what each token may do. Callers whose role has no
`ticket.*` permission (for example an Admin-only account) report tier `none`. Article bodies are
cut to 4000 characters with a `truncated` flag and a hint. Search on a Zammad without Elasticsearch is a
case-insensitive substring match with no field syntax or ranking; use the `search_tickets` filters to narrow it.

### Two-step tools

Sending an email and applying a macro each take two calls. The `prepare_*` tool checks the request, stores it
behind a confirmation token and returns a preview for the user. The confirming tool must echo the preview's key
fields (`to` and `subject` for email, `macro_id` and `ticket_ids` for macros), and its token:

- works once, and is spent even when a check then fails;
- expires after `ZAMMAD_CONFIRM_TTL_SECONDS`;
- only works for the user it was issued to;
- is stored as a hash, next to the sha256 of the exact payload. Recomputing that digest on use detects a corrupted
  record, not a forged one. Tamper protection for a shared store comes from the platform-mode Valkey backend, which
  encrypts records with Fernet (authenticated encryption);
- is limited to 20 waiting per user. In the in-memory store each user may hold at most a quarter of its 128 MiB
  (sizes are UTF-8 bytes of the stored JSON), and records over 64 KiB cannot use the last 8 MiB, which stay free
  for small ones such as macro confirmations. The largest possible email record is about 20 MB.

Email replies:

- `prepare_email_reply` fails clearly when the ticket's group has no outgoing email address, or the address has no
  active email channel (Zammad: Admin, Groups and Admin, Channels, Email).
- `to` defaults to the ticket customer's email.
- A ticket's **participants** are:
  - the ticket customer's email;
  - the From, To and Cc of every public article an agent wrote. An agent email counts only when Zammad sent it
    itself (it carries `preferences.email_address_id`); agent notes, phone and web articles always count;
  - the From, To, Cc and Reply-To of every public article the ticket's customer wrote (matched by the article's
    author or on-behalf-of user, or by its From address).

  Internal notes and messages from anyone else, such as an outsider who emailed in with the ticket number, add no
  participants. Unless `ZAMMAD_EMAIL_ALLOW_ANY_RECIPIENT=true`, every recipient must be a participant.
- No recipient may be one of Zammad's own email addresses (any group's), with or without
  `ZAMMAD_EMAIL_ALLOW_ANY_RECIPIENT`.
- The preview lists a `recipient_warnings` entry for every recipient who is not the ticket's customer.
- Limits: at most 10 recipients (to and cc together) of up to 320 characters each, a body of 1,000,000
  characters, and up to 20 attachments with 10 MB of attachment content (decoded) in total. The subject must be one
  line, without control characters or bidi embeddings and overrides (emoji joiners and LRM/RLM marks are fine).
- Spoofing that remains possible, because Zammad trusts the From header of inbound mail:
  - Mail with a forged From of the **customer's** address is filed as the customer's message, so its Reply-To and Cc
    become participants.
  - Mail with a forged From of one of **Zammad's own** addresses is filed as sent by an Agent. This server ignores
    such articles because they lack `preferences.email_address_id`, which relies on Zammad setting that only on mail
    it sends (true in Zammad 7.1).

  Neither can change the default `to`, and every recipient other than the customer is flagged in
  `recipient_warnings`, so the user sees it before confirming.
- `send_email_reply` posts one `email` article (sender Agent, not internal) and is never retried once the request
  may have reached Zammad. Zammad delivers the email in the background, so the result is `queued`. When the outcome
  is unclear (a timeout, a 5xx, or a 2xx whose answer cannot be read) the error says so and asks to check the
  ticket before preparing the reply again. Each accepted send writes one audit log line (`zammad_mcp.audit`) with
  the caller's identity hash, ticket, article id (`unknown` when unreadable) and recipient count, never the body.

Macros:

- Only active macros are listed or applied. Zammad itself would run an inactive one.
- `apply_macro` uses `POST /tickets/mass_macro`, which checks the macro's group restriction and your change access
  on every ticket and changes nothing if any ticket fails. After a timeout or a 5xx the error says the macro may or
  may not have applied.

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
  roles, and state, priority, group and macro names, which only admins set. Knowledge base titles, snippets and
  bodies are framed too. Tools that send email are disabled by default and need a two-step confirmation.
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
