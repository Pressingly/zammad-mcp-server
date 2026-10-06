# Phase 0 spikes: Zammad REST API under mPass SSO

Devstack spikes for FOSS-352 (Zammad MCP), run on 2026-10-06 against the
Pressingly Zammad fork. They check the assumptions in sections 2, 3 and 4 of the
build plan before the core server and platform mode are written.

## Plan changes needed

Every item below is backed by a request recorded in the spike sections further
down.

1. **Mint must send `X-Browser-Fingerprint` (plan §2, step 1).** For agents and
   admins the header-session `GET /api/v1/user_access_token` returns
   `422 {"error":"Need fingerprint param!"}`. Their roles hold
   `user_preferences.device`, so Zammad's device logging needs a fingerprint on
   session auth. Customers don't hold that permission and are never asked.
   - `mint.py` always sends `X-Browser-Fingerprint: <constant, max 160 chars>`
     and a constant `User-Agent`.
   - Use the same User-Agent in steady state too. Zammad logs a `UserDevice`
     row for header sessions and for `token_auth` calls, and emails the user
     "new device" once they have 2 or more devices. A constant fingerprint and
     User-Agent keep that to one row and at most one email per user.
2. **The session cookie is `Secure`, and the MCP talks plain http (plan §1
   client, §2 mint).** `Set-Cookie: _zammad_session_…; path=/; secure; httponly`
   over `http://zammad-nginx:8080`.
   - curl drops it outright.
   - httpx stores it (jar size 1) but never sends it back over http, so the
     follow-up POST gets `403 Authentication required`.
   - A fresh isolated client per mint is still right, but it is not enough.
     `mint.py` has to read the session cookie out of the GET's `Set-Cookie` and
     send it as an explicit `Cookie:` header on the POST and the DELETEs. That
     works (200 and a token).
3. **Header session beats the token (plan §1 client guard, §8 risk), now
   proven.** If `X-Auth-Request-Email` arrives with `Authorization: Token`, the
   middleware opens a session for the header's user. Zammad checks the session
   before the token, so the token and its permission ceiling are ignored.
   - A **Customer** token plus an **agent's** email header returned the agent
     from `/users/me` and every ticket from `/tickets`.
   - A token minted with `[]` plus its owner's header listed tickets normally.
   - Writes then need CSRF (`401 CSRF token verification failed.`), which
     confirms the call ran as a session.
   - An unknown email in the header auto-creates that user.
   - So the outbound hook that blocks `X-Auth-Request-*` is a hard security
     invariant, not hygiene. Give it a dedicated unit test, and strip those
     headers from inbound requests on `/http/api-key/mcp`.
4. **Blank permission list (plan §2, step 2).**
   - `permission: []` is accepted and stored. Under that token every
     permission check fails, so writes return 403. Reads don't error, though:
     `/tickets` and `/tickets/search` return `200 []`, and `/users/me` still
     works. Keep "never POST an empty list", and don't read an empty result
     as "no data".
   - Leaving `permission` out returns `422 "Error ID …: Please contact your
     administrator."`. Internally that is an `ArgumentError` (missing keyword),
     but it is mapped to 422, not 500.
   - Zammad does not check the requested list against the user's
     permissions: an agent token minted with `ticket.customer` and
     `knowledge_base.editor` stores both verbatim. The role ∩ token
     intersection at check time makes that harmless, but the client-side ∩ in
     `ceiling.py` stays, so the stored list (and the fingerprint) is honest.
5. **`permissions` in the GET includes parent nodes** (`ticket`,
   `user_preferences`, `knowledge_base`, …) next to the leaves. Intersect only
   against the explicit leaf set, as the plan says. Never let a bare `ticket`
   into the token.
6. **Telling 403s apart on the mint GET (plan §2, step 1).**
   - `{"error":"User authorization failed."}` means the role lacks
     `user_preferences.access_token`: a Customer before the grant.
   - The fork middleware's own 403s look different:
     `{"error":"access_denied"}` for the corporate gate,
     `"User account is not active"`, `"Maintenance mode enabled!"`, and
     `"DEFAULT_EMAIL_DOMAIN is required but not set"`.
   - A 403 GET carries no `CSRF-TOKEN` header.
   - Map each body to its own error message.
7. **Re-mint trigger (plan §2, lifetime).** An expired token returns
   `401 {"error":"Not authorized (token expired)!"}`. Zammad accepts an
   `expires_at` in the past without complaint and stores date-only values as
   `00:00 UTC` of that day, so `today+9d` lasts at least 8 days. That is still
   comfortably above the 6-day cache TTL.
8. **Expired-token cleanup (plan §2, step 5) must use the header session.**
   - `DELETE /user_access_token/:id` works with cookie + `X-CSRF-Token`; one
     CSRF token from the GET serves several writes.
   - With token auth it is `403 Token authorization failed.`: the minted token
     never carries `user_preferences.*`, which is correct.
   - Deleting another user's id, or an id already gone, returns
     `422 "The API token could not be found."`, so there is no oracle.
   - Customers can delete their own tokens after the grant.
9. **Search (plan §3 search tool).** With Elasticsearch off:
   - `query` is a case-insensitive substring match (ILIKE) over ticket
     `title`, `number`, and article `body`, `from`, `to` and `subject`.
     `*` is stripped. Field syntax (`state.name:new`) and boolean operators
     (`a AND b`) return nothing.
   - `condition` works on both `GET` and `POST /tickets/search`, alone or
     combined with `query`. Both forms work: simple
     (`{"ticket.customer_id":{"operator":"is","value":["136"]}}`) and expert
     (`{"operator":"OR","conditions":[…]}`). So do `contains` on
     `ticket.title` or `article.body`, and `within last (relative)`.
   - **An unknown attribute in `condition` returns
     `422 "Error ID …"`. It is a PostgreSQL `UndefinedColumn` underneath.**
     The tool must whitelist attribute names and operators and never forward
     model-supplied keys verbatim.
   - **Customer search matches text in internal notes** on the customer's own
     tickets: searching an agent-only word returned that ticket id. This is
     upstream behaviour (the SQL search joins every article) and can only
     reveal that a match exists, never the content. Write it down in
     SECURITY.md and never echo match snippets.
   - `with_total_count=true` returns `{records, total_count}`.
     `page`/`per_page` paging works (server max 200; the plan's 100 is fine).
10. **Macro apply (plan §8 open question, answered).** Use
    `POST /api/v1/tickets/mass_macro {"macro_id": N, "ticket_ids": [id]}`.
    - It applies the whole macro, including `pre_condition` owner changes, and
      checks the macro's group restriction (`applicable_on?`) and
      `agent_update_access` on each ticket.
    - `PUT /tickets/:id {"macro.id": N}` is a silent no-op: it returns 200 and
      changes nothing. It only applies the `perform` keys listed in
      `macro.perform_changes`, because it exists for the legacy UI's delayed
      actions.
    - Per the code, neither path checks `macro.active` (not exercised).
      `list_macros` filters on `active` and the apply tool refuses inactive
      ones.
    - Customers get `422 {"error":true,"ticket_id":…}` from mass_macro and
      `403` from `GET /macros`.
11. **KB access for customers (plan §8 open question, answered: yes).**
    - `POST /knowledge_bases/search {"query":…, "flavor":"public"}` works for a
      Customer token. `knowledge_base_id` and `locale` are optional.
    - Customers only ever see **published** answers, whatever flavor they ask
      for.
    - An agent (`knowledge_base.reader`) with `flavor:"agent"` also sees
      **internal** answers. Drafts are hidden from readers.
    - The search endpoint also answers unauthenticated `public` queries.
    - Answer body:
      `GET /knowledge_bases/:kb/answers/:id?include_contents=<content_id>`.
      The parameter takes **content ids** from the translation asset, not
      answer ids. A customer gets `403` on internal answers.
    - Customers have no `knowledge_base.reader`, so `search_knowledge_base` and
      `get_kb_answer` must be visible to the CUSTOMER tier without assuming
      that permission is in the ceiling.
12. **Customer ticket create (plan §3 MVP).**
    - Zammad forces `customer_id` to the caller, whether it was sent as an id
      or as `customer: <other email>`, and resets `owner_id`.
    - It forces the first article's sender to Customer and `internal` to
      false, and turns `type` into `note` unless it is `web`.
    - **It does let a customer set `state` and `priority` on create** (a
      ticket was created `closed` with `3 high`).
    - `group` is required (`422 "The required value 'group_id' is missing."`).
    - The customer `create_ticket` schema exposes only title, group, body and
      attachments, and sends `type: "web"`. `list_ticket_options` has to give
      customers the groups they may create in.
13. **Customer article create (plan §3 `add_ticket_note`).** Zammad forces
    `sender` to Customer and `internal` to false, and maps any `type` other
    than `note` or `web` to `note`. `type: email` became a `note`, so no mail
    was sent, but the `to` value was still stored. Customer replies send
    `type: "web"` and no `to`/`cc`. Cross-customer posts return `403`.
14. **Admin-only users (plan §2, tier).** The stock Admin role holds
    `admin`, `knowledge_base.editor`, `report` and `user_preferences`, but no
    `ticket.*`.
    - An Admin without Agent gets the ceiling `{knowledge_base.editor}` and
      falls into the CUSTOMER tier with no ticket access at all.
    - The bundle and askii both grant Admin+Agent, so only admins made by hand
      in the UI are affected.
    - Tier logic should treat "no `ticket.*` in the ceiling" as its own case:
      KB-only, or a clear error. It should not be labelled CUSTOMER.
15. **FOSS-511 rollout note.** On the devstack the grant was applied by
    running the new step from `dev/provision/provision-zammad.sh` on its own,
    not the whole script, because a `docker compose up --dry-run` from the
    branch worktree showed it would recreate the shared `postgres` container.
    The step is plain `docker exec … rails runner`, so it behaves the same
    either way.

Not exercised:

- **Corporate gate.** `SMB_CORPORATE_ID` is empty on the devstack. Per the
  code, when it is set the middleware needs `X-Auth-Request-Access-Token` with
  `custom:is_corporate == "true"` and a matching `custom:corporate_id`, and
  otherwise returns `403 {"error":"access_denied"}`. Cover it in the contract
  tests.
- **The `foss-zammad-sbx-trigger` registry check** from the Phase 0 row.
  It was outside this task.

## Environment

| Item | Value |
|---|---|
| Zammad | `7.1.2-511ff797.docker` (fork `foss-sandbox` @ `511ff797f7`), image `foss-devstack/zammad:dev` |
| Env | `AUTH_TYPE=SSO`, `SMB_CORPORATE_ID=` (empty), `ELASTICSEARCH_ENABLED=false`, `DEFAULT_EMAIL_DOMAIN=askii.ai` |
| Settings | `api_token_access=true`, `customer_ticket_create=true` |
| Caller | `docker run --rm --network foss-backend curlimages/curl` → `http://zammad-nginx:8080`, the MCP's own path. The httpx check ran in `python:3.12-slim` on the same network. |

Fixtures created for the spikes (still on the devstack):

| Kind | Ids |
|---|---|
| Users | 133 `9990000000000001@askii.ai` (Customer, synthetic), 134 `spike-agent@spike.test` (Agent, group Users full), 135 `spike-customer@spike.test` (Customer A), 136 `spike-customer2@spike.test` (Customer B), 137 `spike-newbie@spike.test` (auto-created in spike 3) |
| Tickets | 2 (A), 3 (B), 4 and 5 (created by A via token), 6 (macro target) |
| KB | KB 2, category 1, answers 1 published, 2 internal, 3 draft |
| Tokens | ids 1–5 (all expire 2026-10-15) and 8 (expired, left over from the httpx check); 6 and 7 were deleted in spike 7 |

Secrets are redacted throughout. `<cookie>` is the `_zammad_session_a138cfd0f37`
value, `<csrf>` the `CSRF-TOKEN` value and `<token>` a minted token.

## Spike 1: mint flow (agent and customer)

| Case | Result |
|---|---|
| Agent GET without fingerprint | FAIL as found: `422 Need fingerprint param!` (plan change 1) |
| Agent GET + fingerprint | PASS |
| Agent POST with cookie + CSRF | PASS, `{token}` |
| Customer before FOSS-511 grant | PASS, 403 as expected |
| Customer after grant | PASS |

Agent, header session:

```
GET /api/v1/user_access_token
X-Auth-Request-Email: spike-agent@spike.test
X-Browser-Fingerprint: zammad-mcp-spike
User-Agent: zammad-mcp-spike/0

HTTP/1.1 200 OK
csrf-token: <csrf>
set-cookie: _zammad_session_a138cfd0f37=<cookie>; path=/; secure; httponly
{"tokens":[],"permissions":[…]}
```

`permissions` names: `chat, chat.agent, cti, cti.agent, knowledge_base,
knowledge_base.reader, ticket, ticket.agent, user_preferences,
user_preferences.access_token, user_preferences.appearance, user_preferences.avatar,
user_preferences.beta_ui_switch, user_preferences.calendar, user_preferences.device,
user_preferences.language, user_preferences.linked_accounts,
user_preferences.notifications, user_preferences.out_of_office,
user_preferences.overview_sorting, user_preferences.password,
user_preferences.two_factor_authentication`.

Without `X-Browser-Fingerprint`, the same request returns
`422 {"error":"Need fingerprint param!"}`.

```
POST /api/v1/user_access_token
Cookie: _zammad_session_a138cfd0f37=<cookie>
X-CSRF-Token: <csrf>
Content-Type: application/json
{"name":"zammad-mcp (auto) 2026-10-06","permission":["ticket.agent","ticket.customer","knowledge_base.reader","knowledge_base.editor"],"expires_at":"2026-10-15"}

HTTP/1.1 200 OK
{"token":"<token>"}
```

Stored row: `persistent: true`, `expires_at: 2026-10-15 00:00:00 UTC`,
`preferences: {"permission": [the four requested, verbatim]}`.

Negative mint cases:

| Request | Response |
|---|---|
| POST with cookie, no `X-CSRF-Token` | `401 {"error":"CSRF token verification failed.","invalid_csrf_token":true}` |
| POST with header + CSRF from an earlier GET, no cookie | `401 CSRF token verification failed.` (the middleware opens a new session) |
| POST relying on the httpx cookie jar (no explicit `Cookie`) | `403 Authentication required` (plan change 2) |

Customer before the FOSS-511 grant:

```
GET /api/v1/user_access_token   (X-Auth-Request-Email: spike-customer@spike.test)
HTTP/1.1 403 Forbidden            (set-cookie present, no csrf-token)
{"error":"User authorization failed.","error_human":"User authorization failed."}

POST /api/v1/user_access_token  → 403 {"error":"User authorization failed."}
```

Customer after the grant (Part B, run as shown under "FOSS-511 idempotency proof"):

```
GET  → 200, csrf-token + set-cookie present, "tokens":[]
       permissions: ticket, ticket.customer, user_preferences, user_preferences.access_token,
       user_preferences.appearance, user_preferences.avatar, user_preferences.language,
       user_preferences.linked_accounts, user_preferences.password,
       user_preferences.two_factor_authentication
POST {"name":"zammad-mcp (auto) 2026-10-06","permission":["ticket.customer"],"expires_at":"2026-10-15"}
     → 200 {"token":"<token>"}
```

## Spike 2: role ∩ token intersection

| Case | Result |
|---|---|
| Customer token `["ticket.agent","ticket.customer"]` can't see other customers' tickets | PASS |
| …can't search users or add internal notes | PASS |
| Blank `[]` | PASS: all checks fail (empty lists, 403 on writes); `/users/me` still 200 |
| Omitted `permission` | `422` Error ID (ArgumentError underneath), not 500 |

Customer A token minted with `["ticket.agent","ticket.customer"]`:

| Request | Response |
|---|---|
| `GET /tickets?per_page=50` | `200`, ids `[2]` (agent token sees `[1,2,3]`) |
| `GET /tickets/3` (customer B's) | `403 {"error":"Not authorized"}` |
| `GET /users/search?query=spike` | `403 {"error":"Token authorization failed."}` |
| `GET /users/136` | `403 Not authorized` |
| `POST /ticket_articles {"ticket_id":2,"type":"note","internal":true,"sender":"Agent",…}` | `201`, stored as `type: note, sender: Customer, internal: false` |

Blank and omitted lists:

| Mint body | Mint response | `/users/me` | `/tickets` | `/tickets/2` | `/tickets/search` | `POST /ticket_articles` | `/users/search` |
|---|---|---|---|---|---|---|---|
| agent, `"permission":[]` | `200 {token}` | 200 | `200 []` | 403 | `200 []` | 403 | 403 |
| customer, `"permission":[]` | `200 {token}` | 200 | `200 []` | 403 | `200 []` | n/a | n/a |
| agent, no `permission` key | `422 {"error":"Error ID GG9vEFqH: Please contact your administrator."}`; log: `missing keyword: :permission (ArgumentError)` | | | | | | |

## Spike 3: steady state

| Case | Result |
|---|---|
| Token alone (no cookie, no `X-Auth-Request-*`) | PASS |
| Header next to the token | Session wins and the token ceiling is ignored (plan change 3) |

`Authorization: Token token=<token>` with no cookie and no `X-Auth-Request-*`.
No response sets a cookie.

| Token | `/users/me` | `/tickets` | `/tickets/search?query=Spike` |
|---|---|---|---|
| agent | 200, id 134, role_ids [2] | 200, `[1,2,3]` | 200, `[2,3]` |
| customer A | 200, id 135, role_ids [3] | 200, `[2]` | 200, `[2]` |

Header next to the token:

| Request | Response |
|---|---|
| customer A token + `X-Auth-Request-Email: spike-agent@spike.test` → `/users/me` | `200`, **id 134 spike-agent**, and `set-cookie` issued |
| same → `/tickets` | `200`, **`[1,2,3]`** (the agent's view) |
| customer `[]` token + its own email header → `/tickets` | `200`, `[2]` (the session ignores the empty ceiling) |
| agent token + own header, `POST /ticket_articles` without CSRF | `401 CSRF token verification failed.` |
| agent token + `X-Auth-Request-Email: spike-newbie@spike.test` | `200`, new user **137** auto-created |

## Spike 4: search with Elasticsearch disabled

| Case | Result |
|---|---|
| `query` | PASS: substring only |
| `condition` via POST and GET | PASS |
| Unknown attribute | 422 with a SQL error underneath (plan change 9) |

Agent token unless noted.

| Request | Ids |
|---|---|
| `GET ?query=27003` (number) | `[3]` |
| `GET ?query=body of` (article body) | `[2,3]` |
| `GET ?query=Spi*` | `[2,3]` (`*` stripped) |
| `GET ?query=SPIKE TICKET` | `[2,3]` (case-insensitive) |
| `GET ?query=state.name:new` | `[]` |
| `GET ?query=customer AND Spike` | `[]` |
| customer A, `GET ?query=<word only in an agent internal note on ticket 2>` | **`[2]`** |
| `POST {"condition":{"ticket.customer_id":{"operator":"is","value":["136"]}}}` | `[3]` |
| `POST {"query":"Spike","condition":{"ticket.state_id":{"operator":"is","value":["1"]}}}` | `[2,3]` |
| `POST {"condition":{"ticket.title":{"operator":"contains","value":"customer B"}}}` | `[3]` |
| `POST {"condition":{"article.body":{"operator":"contains","value":"<internal word>"}}}` | `[2]` |
| `POST {"condition":{"ticket.created_at":{"operator":"within last (relative)","value":"1","range":"day"}}}` | `[2,3]` |
| `POST {"condition":{"operator":"OR","conditions":[{customer_id is 135},{customer_id is 136}]}}` | `[2,3]` |
| same with `AND` | `[]` |
| `OR` 135 \| 999 | `[2]` |
| customer A, `POST` condition `customer_id is 136` | `[]` (scope still applies) |
| `GET ?condition[ticket.customer_id][operator]=is&condition[ticket.customer_id][value]=136` | `[3]` |
| `POST {"condition":{"ticket.nonexistent":{…}}}` | `422 "Error ID w-rb3jm-: …"`; log: `PG::UndefinedColumn` |
| `POST ?with_total_count=true&per_page=1&page=2 {"query":"Spike"}` | `{"records":[ticket 3],"total_count":2}` |

## Spike 5: customer capabilities

| Case | Result |
|---|---|
| KB search | PASS: published only |
| Article forcing | PASS |
| Tags on own ticket | PASS (read only) |
| Customer ticket create | `customer_id` forced, but `state`/`priority` not (plan change 12) |

KB search, `POST /knowledge_bases/search {"knowledge_base_id":2,"locale":"en-us","flavor":<f>,"query":"walrus"}`:

| Caller | `flavor: public` | `flavor: agent` |
|---|---|---|
| customer A token | answer 1 (published) | answer 1 |
| customer token with `ticket.agent` in its list | | answer 1 |
| agent token (`knowledge_base.reader`) | answer 1 | answers 2 (internal) and 1 |
| no auth at all | answer 1 | |

Without `knowledge_base_id` or `locale`, a customer gets the same result.
`POST /knowledge_bases/init` works for customers (KB ids, no locale assets).

Answer body, `GET /knowledge_bases/2/answers/<id>?include_contents=<content_id>`:

| Caller | Answer 1 (published) | Answer 2 (internal) |
|---|---|---|
| customer | 200, body returned | `403 Not authorized` |
| agent | 200 | 200 |

Customer A article create on own ticket 2:

| Sent | Stored |
|---|---|
| `type: email, sender: Agent, internal: true, to: x@evil.test` | `type: note, sender: Customer, internal: false, to: x@evil.test` (stored, nothing sent) |
| `type: phone` | `type: note` |
| `type: web` | `type: web`, `from: Spike-customer <spike-customer@spike.test>` |
| any type on ticket 3 (customer B) | `403 Not authorized` |

Tags:

| Request | Response |
|---|---|
| customer `GET /tags?object=Ticket&o_id=2` (own) | `200 {"tags":["spike-tag"]}` |
| customer `GET /tags?object=Ticket&o_id=3` | `403` |
| customer `POST /tags/add` (own ticket) | `403` |
| customer `GET /tag_search?term=spike` | `403` |

Customer ticket create, `POST /tickets` with a customer token:

| Sent | Result |
|---|---|
| `customer_id: 136, owner_id: 134, state: closed, priority: 3 high, article.type: email, internal: true, sender: Agent` | `201` ticket 4: **customer_id 135**, owner 1, **state closed, priority 3 high**, article `note / Customer / internal false` |
| `customer: "spike-customer2@spike.test"` | `201` ticket 5, customer_id **135** |
| no `group` | `422 "The required value 'group_id' is missing."` |

## Spike 6: macro apply (agent)

Result: PASS via `POST /tickets/mass_macro`. The PUT shape is unsuitable.

Macro 1 "Close & Tag as Spam": `perform = {ticket.state_id: 4, ticket.tags: add spam, ticket.owner_id: pre_condition current_user.id}`, `active: true`, no group restriction.

| Request | Response | Ticket after |
|---|---|---|
| `PUT /tickets/5 {"macro.id":1}` | 200 | **unchanged** (`new`, no tags) |
| `PUT /tickets/5 {"macro.id":1,"macro.perform_changes":["ticket.state_id","ticket.tags"]}` | 200 | `closed`, `[spam]`; owner unchanged (key not listed) |
| `POST /tickets/mass_macro {"macro_id":1,"ticket_ids":[6]}` | 200 `{"ticket_ids":[6],"assets":{…}}` | `closed`, `[spam]`, **owner 134** |
| customer token, `POST /tickets/mass_macro {"macro_id":1,"ticket_ids":[2]}` | `422 {"error":true,"ticket_id":2}` | unchanged |
| customer token, `GET /macros` | `403 Token authorization failed.` | |

## Spike 7: expired-token cleanup

Result: PASS under the header session with cookie + CSRF.

```
POST /user_access_token {"name":"zammad-mcp (auto) 2020-01-01","permission":["ticket.agent"],"expires_at":"2020-01-01"}
→ 200 {"token":"<token>"}                       (past dates are accepted)

GET /users/me  (Authorization: Token token=<that token>)
→ 401 {"error":"Not authorized (token expired)!"}

GET /user_access_token (header session)
→ tokens[]: {"id":6,"name":"zammad-mcp (auto) 2020-01-01","expires_at":"2020-01-01T00:00:00.000Z",
             "last_used_at":"2026-10-06T13:54:03Z",…}       (no token values are listed)
```

| Request | Response |
|---|---|
| `DELETE /user_access_token/6`, cookie, no CSRF | `401 CSRF token verification failed.` |
| `DELETE /user_access_token/6`, cookie + CSRF | `200 {}` |
| same again | `422 "The API token could not be found."` |
| `DELETE /user_access_token/2` (another user's) | `422 "The API token could not be found."` |
| `DELETE /user_access_token/4` with agent **token** auth | `403 Token authorization failed.` |
| customer A, own expired token 7, cookie + CSRF | `200 {}` |

## Spike 8: synthetic identity

First request with a bare mPass id:

```
GET /api/v1/users/me
X-Auth-Request-Email: 9990000000000001

HTTP/1.1 200 OK
set-cookie: _zammad_session_a138cfd0f37=<cookie>; path=/; secure; httponly
{"id":133,"login":"9990000000000001@askii.ai","email":"9990000000000001@askii.ai",
 "firstname":"9990000000000001","lastname":"","verified":false,"role_ids":[3],…}
```

The middleware appends `DEFAULT_EMAIL_DOMAIN` to any claim without an `@` and
creates the user with the signup role (Customer) on the first request of any
kind. Spike 3 showed this happens even when a token is also present. The MCP
must refuse `<anything>@DEFAULT_EMAIL_DOMAIN` before its first Zammad call, as
plan §2 says. Once the user exists the refusal no longer prevents anything.

## FOSS-511 idempotency proof

The step added to `foss-server-bundle` `dev/provision/provision-zammad.sh`
(branch `usama/zammad-customer-token-grant`) was cut out of the script with
`sed` and run twice:

```
== run 1
--- Granting the Customer role API token access (used by zammad-mcp)...
Customer role granted user_preferences.access_token.
== run 2
--- Granting the Customer role API token access (used by zammad-mcp)...
Customer role already has user_preferences.access_token, nothing to do.
```

Afterwards the Customer role has exactly one `user_preferences.access_token`
row and 8 permissions (7 before). The customer mint in spike 1 then succeeded.
