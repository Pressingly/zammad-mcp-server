# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`zammad-mcp-server`: an MIT-licensed MCP server for the Zammad helpdesk, built on FastMCP. Every Zammad call
carries a per-user token (`Authorization: Token token=...`), never a shared admin token.

- `zammad_mcp/__main__.py`: `zammad-mcp stdio|http`.
- `zammad_mcp/server.py`: `build_server(settings)` registers tools; `build_http_app(mcp)` serves `/mcp` and
  `GET /healthz`.
- `zammad_mcp/client/`: the async Zammad client. It refuses cookies and raises if an `X-Auth-Request-*` header is
  about to be sent. Keep both guarantees.
- `zammad_mcp/tools/`: one module per Zammad domain, each with `register(mcp, context)`.
- `zammad_mcp/credentials/`: where each request's token comes from (`ZAMMAD_HTTP_TOKEN`, or `X-Zammad-Token` on
  `/http/api-key/mcp`). In http mode `/mcp` is only mounted with `ZAMMAD_HTTP_SHARED_TOKEN_ROUTE=true`. `zammad_mcp/platform/` (FOSS-513) is a placeholder.
- `zammad_mcp/tiers.py`: the `tier:*` tags and the one `auth=` check that filters tools by tier.
- `zammad_mcp/shaping.py`: compact output, `<untrusted_content>` framing, truncation. `confirmations.py`: two-step
  confirm tokens.

## Commands

```bash
uv sync
uv run ruff format .
uv run ruff check .
uv run pytest --cov=zammad_mcp --cov-fail-under=80
docker build --platform linux/amd64 .
```

## Branch policy

| Branch | Role |
|---|---|
| `foss-sandbox` | **Staging and the default branch.** Everything lands here first and is verified on staging. |
| `foss-main` | **Production.** Prod images are built from here. |

- Work goes feature branch, then a PR into `foss-sandbox`, then a promotion PR from `foss-sandbox` into
  `foss-main`. Never commit or push directly to either branch, and never target `foss-main` from a feature branch.
- Promotion PRs use a **merge commit**, never squash or rebase, so the two branches stay convergent.
- Releases are tags: `vYY.MM.PATCH-rc.N` on `foss-sandbox` builds a staging image, `vYY.MM.PATCH` on `foss-main`
  builds the prod image (with approval). Never build images on a server.

## Isolation rule

- The community core (`client/`, `credentials/`, `tools/`, `server.py`) works against any Zammad and imports
  nothing from `platform/`.
- Platform-only logic (Cognito, token minting, Valkey storage) lives in `platform/` and is reached by a one-line
  hook, only when `COGNITO_USER_POOL_ID` is set.
- Disabled tool modules never register. Each module has one `ZAMMAD_ENABLE_*` flag, read through
  `env_flags.env_flag`.

## Tool conventions

- `snake_case` verb_noun names.
- Register through `context.tool(title, <spec>, module=..., tier=...)` with a spec from `tools/context.py`
  (`READ`, `CREATE`, `ADD`, `OVERWRITE`, `PREPARE`, `IRREVERSIBLE`): it sets all four
  `ToolAnnotations` hints, the `tier:*` and `module:*` tags and the tier check. `tests/test_tools.py` enforces it.
- PUT is retried on timeouts, so a PUT body never carries an `article`; messages go through `POST /ticket_articles`.
- Tools return an `Error: ...` string instead of raising.
- Text written by Zammad users is untrusted: return every free-text value through `shaping.untrusted_field` or
  `frame_untrusted`, never raw.
- Validate responses with `Model.parse(...)` inside the tool's `try`, so an odd payload becomes an `Error:` string.
- Agent-only arguments go through `agent_only_refusal`, which fails closed when the caller's tier is unknown.
- Actions that leave Zammad or change many tickets (email, macros) are a `prepare_*` tool plus a confirming tool,
  through `context.confirmations`, and their module is off by default.

## Commits and PRs

- [Conventional Commits](https://www.conventionalcommits.org/) for every subject: `type(scope): subject`,
  imperative, lowercase, no trailing period.
- No AI attribution anywhere: no `Co-Authored-By` trailer, no "Generated with" line, in commits or PRs.
- PR descriptions have a `## Description` section and an optional `## Testing` section, written as bullet points.
