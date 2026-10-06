"""Per-request Zammad credential resolution (lands in FOSS-512).

Planned modules:

- ``base.py``: the ``CredentialSource`` protocol every mode implements.
- ``static.py``: community mode, the ``ZAMMAD_HTTP_TOKEN`` environment token.
- ``header.py``: community HTTP mode, a per-request ``X-Zammad-Token`` header
  on ``/http/api-key/mcp``.

Until then the server reads the static token straight from ``Settings``.
"""
