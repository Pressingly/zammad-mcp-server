# Platform mode

Platform mode serves Zammad to MCP clients that sign in with OAuth (AWS Cognito, optionally behind an
mPass-style auth proxy). Each signed-in user works in Zammad with a token minted for them, bounded by their own
Zammad role. Nobody shares a token, and there is no fallback to one.

It needs a Zammad behind a trusted-header SSO middleware (the Pressingly fork's `MpassProxyAuth`) reachable on an
internal URL that the public SSO proxy does not front. Install the `[platform]` extra (the Docker image includes
it) and set `COGNITO_USER_POOL_ID`; `zammad-mcp http` then runs platform mode. `stdio` is unaffected.

## Routes

| Route | Auth | Credential |
|---|---|---|
| `/mcp` | FastMCP's Cognito provider (OAuth 2.0 with dynamic client registration and RFC 8414/9728 discovery) | The caller's minted token |
| `/authorize`, `/auth/callback`, `/token`, `/register`, `/.well-known/*` | Public OAuth endpoints | n/a |
| `/http/api-key/mcp` | None at the MCP layer | The caller's own `X-Zammad-Token`; `X-Auth-Request-*` headers are stripped |
| `/healthz` | None | n/a |

Register `<MCP_BASE_URL>/auth/callback` as a callback URL on the Cognito app client. The router in front of the
server must not add an SSO ForwardAuth to these paths: the Cognito provider is the only auth layer on `/mcp`.

## How a token is minted

On a user's first call, or when their cached token needs replacing, the server opens a fresh httpx client of its
own against `ZAMMAD_INTERNAL_BASE_URL` and:

1. Sends `GET /api/v1/user_access_token` with `X-Auth-Request-Email: <identity>` and, when the session has one,
   `X-Auth-Request-Access-Token: <upstream Cognito access token>` for the corporate-ID gate. The middleware opens a
   session for that user; Zammad answers with a `CSRF-TOKEN` header, the session cookie and the user's permissions.
2. Builds the ceiling: `{ticket.agent, ticket.customer, knowledge_base.reader, knowledge_base.editor}` intersected
   with the leaf permissions the role holds. `admin.*`, `report` and `user_preferences.*` never enter a token. A
   `narrow_ceiling(claims, ceiling)` hook can only remove permissions. An empty ceiling fails closed.
3. Sends `POST /api/v1/user_access_token` with that explicit list, `expires_at = today + 9 days` and the name
   `zammad-mcp (auto) <date>`. The session cookie is `Secure` and the internal URL is plain http, so it is copied
   by hand into a `Cookie` header together with `X-CSRF-Token`.
4. Deletes, best effort, this user's own expired `zammad-mcp*` tokens. A live token is never revoked, since another
   replica may be using it.
5. Always, even when an earlier step failed: `DELETE /api/v1/signout` with the same cookie. The middleware opened a
   persistent web session carrying the user's full role; signing out deletes that session row at once instead of
   leaving it for Zammad's four-week session reaper. It ends only this session, never the user's browser sessions.

Every bootstrap request sends the same `X-Browser-Fingerprint` and `User-Agent` as the steady-state client, so
Zammad records one device per user and sends at most one new-device email.

The identity is the id_token's `email` (lowercased), or `cognito:username` when there is no email. An unverified
platform user carries exactly `<cognito:username>@<DEFAULT_EMAIL_DOMAIN>` (mpass-auth-proxy's email overlay), and
Zammad's middleware turns a value without `@` into `<value>@DEFAULT_EMAIL_DOMAIN`. The server refuses both shapes
before sending anything to Zammad, because the first request of any kind would create that user, and tells the user
to verify their email in the portal first. It matches the launchpad verify-gate (ADR-0004): the domain alone is not
enough, since it can also be a real mail domain (`jane@askii.ai` is minted). Any other address in that domain is
accepted only from a token mpass-auth-proxy's email overlay stamped (`preferred_username == cognito:username`): the
overlay only emits the caller's own synthetic address or a launchpad-verified real one, so an unstamped token
carrying someone else's `<sid>@<domain>` is refused, as is an address there without a `cognito:username`. With
email capture off, mpass does not stamp tokens, so real addresses in `DEFAULT_EMAIL_DOMAIN` are refused too.

## Cache, lifetime and role changes

- The token is cached under `zammad-mcp:token:v1:<namespace>:<sha256(identity)>`, Fernet-encrypted with a key
  derived from `OIDC_CLIENT_SECRET` (or `MCP_JWT_SIGNING_KEY`). The namespace is a short hash of `OIDC_CLIENT_ID`
  and `ZAMMAD_INTERNAL_BASE_URL`, so two deployments sharing one Valkey database never read each other's tokens.
  The value holds the token, its name, permissions, tier, the ceiling fingerprint, `expires_at` and `checked_at`.
- Zammad stores a date-only `expires_at` as the start of that day in its `timezone_default` (00:00 UTC on a default
  install), so a token asked for `today + 9 days` lives about 8 days. The cache keeps it for 6 days and treats it as
  stale from one day before 00:00 UTC of the expiry date, which stays ahead of the real expiry in any time zone.
- Downgrades apply at once, because Zammad intersects the role and the token on every check.
- Upgrades are found by a re-check every 4 hours, and after a role-gated 403. The 403 only writes a re-check
  marker (`SET NX EX`, so at most once per 5 minutes per identity) and never rewrites the cached token, so it cannot
  race a concurrent re-mint; a token checked after the marker was set needs no re-check. A changed ceiling mints a
  new token.
- A 401 (expired or revoked token) drops the cache entry, mints once and retries the request once.
- A denial while re-checking (corporate gate, customer token access withdrawn) drops the cache entry and fails the
  request. Token auth itself does not pass the corporate gate, so a user removed from the corporate ID keeps access
  until the next re-check, at most 4 hours.
- Any cache or mint error fails the request with an `Error: ...` message. Nothing falls back to another token. The
  one exception is an entry that no longer decrypts (a rotated secret): it is treated as absent and re-minted.
- Every decision writes one JSON line on the `zammad_mcp.audit` logger: `token.minted` (with the reason),
  `token.recheck_unchanged`, `token.recheck_requested`, `token.renew_reused`, `token.denied` and `token.refused`.
  Each carries the identity hash and, where there is a token, its tier, sorted permissions and name. Never the token
  or the email.

## Tool visibility

`tools/list` is filtered per user by tier: agents see every tool, customers see the customer tools, and an account
whose ceiling holds no `ticket.*` permission (an Admin without the Agent role) sees only the tier-free tools. The
tier comes from the minted token and is memoised in process for 60 seconds per identity.

Unlike community mode, platform mode fails closed: if the identity is missing, the mint fails or anything raises,
the caller is treated as having no tier and only `get_me` stays visible, so calling it shows why. Zammad still
enforces every permission; the filter only keeps the model from offering tools that would fail.

A promoted user sees the new tools after the re-check and a reconnect of the MCP client.

## Customers

Stock Zammad does not let Customers create API tokens. An admin grants `user_preferences.access_token` to the
Customer role once (Admin, Roles, Customer, or `Role.find_by(name: 'Customer')&.permission_grant(...)`). Until
then Customers get "Customer token access is not enabled on this Zammad server".

## Storage

With `MCP_OAUTH_STORAGE_URL` set (`redis://`, `rediss://`, `valkey://` or `valkeys://`, query string kept), one
Valkey database holds:

| Keys | Content |
|---|---|
| FastMCP collections | OAuth state (clients, transactions, codes, upstream tokens, JTI mappings), Fernet-encrypted |
| `zammad-mcp:token:v1:<namespace>:*` | Minted tokens, Fernet-encrypted |
| `zammad-mcp:recheck:v1:<namespace>:*` | 403 re-check markers, 5-minute TTL |
| `zammad-mcp:confirm:v1:*` | Two-step confirmations, Fernet-encrypted (they can hold a whole email) |
| `zammad-mcp:confirm-bytes:v1:<namespace>:*` | Confirmation sizes, expiries and running total, for the byte cap |

Confirmations in Valkey share a 128 MiB cap (plaintext bytes; Fernet adds about a third in Valkey memory), with the
last 8 MiB kept for small records, like the in-process store. A full store refuses the next large record with a
clear message. Each store and take is one Lua script, so the cap holds across replicas without retries. Without `MCP_OAUTH_STORAGE_URL` everything lives in process: it is lost on restart and not shared
between replicas.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `COGNITO_USER_POOL_ID` | required | Turns platform mode on |
| `COGNITO_AWS_REGION` | required | Cognito region |
| `OIDC_CLIENT_ID` | required | Cognito app client id |
| `OIDC_CLIENT_SECRET` | | Cognito app client secret (confidential client); also the encryption key material |
| `MCP_JWT_SIGNING_KEY` | | Signing key for public (PKCE) clients; needed when there is no client secret |
| `MCP_BASE_URL` | required | Public URL of this server, e.g. `https://support-mcp.example.com` |
| `ZAMMAD_INTERNAL_BASE_URL` | required | Zammad on the internal network, e.g. `http://zammad-nginx:8080`; every Zammad call goes here |
| `DEFAULT_EMAIL_DOMAIN` | required | Synthetic domain of unverified users (a bare domain: no `@`, scheme or trailing dot); `<cognito:username>@` it is refused |
| `ZAMMAD_PUBLIC_URL` | `ZAMMAD_URL` | Base URL for the `url` links in results |
| `MCP_OAUTH_STORAGE_URL` | unset | Valkey URL for OAuth state, the token cache and confirmations |
| `MCP_ALLOWED_CLIENT_REDIRECT_URIS` | required | Comma-separated redirect URI patterns (fnmatch) for dynamic client registration, e.g. `https://claude.ai/api/mcp/auth_callback,http://localhost:*/*`. An empty value refuses to start |
| `MCP_ALLOW_ANY_REDIRECT_URI` | `false` | `true` (with no allow-list) accepts any redirect URI, with a startup warning |
| `COGNITO_UPSTREAM_AUTH_URL` | discovered | Send users to an auth proxy's `/authorize` instead of Cognito's hosted UI |
| `COGNITO_UPSTREAM_TOKEN_URL` | discovered | Exchange codes through an auth proxy's `/token` |
| `MCP_OIDC_SCOPES` | `openid` | Upstream scopes, space- or comma-separated |
| `MCP_ACCESS_TOKEN_TTL_SECONDS` | `86400` | Lifetime of the token FastMCP issues to the MCP client (the upstream token refreshes hourly underneath) |
| `MCP_ALLOWED_ORIGINS` | `*` | CORS origins |
| `MCP_LOG_LEVEL` | `INFO` | Log level; logs are JSON lines on stderr and audit lines are always kept |
| `MCP_ENV` | | `production` adds a note to the warning when storage is in process |
| `ZAMMAD_SSO_EMAIL_HEADER` | `X-Auth-Request-Email` | Identity header the SSO middleware trusts, for other deployments |
| `ZAMMAD_SSO_ACCESS_TOKEN_HEADER` | `X-Auth-Request-Access-Token` | Access-token header for a corporate-ID gate |

The refresh lifetime falls back to 30 days, since Cognito sends no `refresh_expires_in`. The community settings
(`ZAMMAD_ENABLE_*`, `ZAMMAD_READ_ONLY`, timeouts, `MCP_HTTP_PORT`) apply as usual. `ZAMMAD_HTTP_TOKEN` and
`ZAMMAD_HTTP_SHARED_TOKEN_ROUTE` are refused in platform mode.

## Security notes

- The steady-state client sends only `Authorization: Token token=...`, never a cookie and never an
  `X-Auth-Request-*` header (a hard guard in the client). The identity headers appear only on the bootstrap `GET`.
- Any container on the internal network can send `X-Auth-Request-Email` to Zammad directly. That exposure exists
  without this server; keep the internal URL off untrusted networks.
- Logs never contain tokens, cookies, CSRF tokens or bodies; cache keys are hashes of the identity.
