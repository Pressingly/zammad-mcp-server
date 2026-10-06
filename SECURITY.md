# Security Policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue or pull request.

- Use GitHub private vulnerability reporting: open the repository's **Security** tab and choose
  **Report a vulnerability**, or go to
  <https://github.com/Pressingly/zammad-mcp-server/security/advisories/new>.
- If that option is not available, open an issue titled "Security contact request" with no details, and a
  maintainer will reply with a private channel.

Include what you need for us to reproduce it: the affected version or commit, the mode (stdio, HTTP, platform),
the Zammad version, and the steps.

We aim to acknowledge a report within three business days and keep you updated until it is fixed. Please give us a
reasonable time to release a fix before disclosing publicly. We are happy to credit you once it is resolved.

## In scope

- Any way to act in Zammad as a different user, or with more permissions than the caller's own token allows.
- Leaking a token, a session, or another user's data across requests.
- Sending `X-Auth-Request-*` or other trusted identity headers to Zammad.
- Bypassing a disabled tool flag or a confirmation step.

## Known Zammad behaviour

- **An `X-Auth-Request-Email` header overrides token auth** on an SSO-fronted Zammad: the header opens a session
  for that user and the token's permissions are ignored. This is why the server refuses to send any
  `X-Auth-Request-*` header and strips them from requests arriving on `/http/api-key/mcp`.
- **Customer ticket search matches internal notes.** Without Elasticsearch, Zammad's database search joins every
  article, so a customer's search can match a word that only appears in an agent's internal note on that
  customer's own ticket. Only the fact that the ticket matched is revealed, never the note. The server returns
  tickets, never match snippets.

## Out of scope

- Prompt injection that only makes the model misuse tools the user already holds, unless it bypasses a
  confirmation step or a disabled flag.
- Issues in Zammad itself; report those to the Zammad project.
