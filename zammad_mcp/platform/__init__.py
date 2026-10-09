"""Optional platform mode: Cognito/mPass SSO with per-user minted tokens (FOSS-513).

Enabled only when ``COGNITO_USER_POOL_ID`` is set, and needs the
``[platform]`` extra. The community core reaches it through one hook in
``__main__``; nothing else in the core imports from here.

- ``cognito.py``: the OAuth provider that keeps the upstream id_token.
- ``identity.py``: who the caller is, read from the validated token.
- ``storage.py``: Valkey URL parsing, the OAuth-state store and a small
  key-value adapter (Valkey or in-process).
- ``ceiling.py``: the permission ceiling a minted token may carry.
- ``mint.py``: per-user Zammad token minting, caching and re-checks.
- ``credentials.py``: the credential provider, token renewal and the
  fail-closed tier resolver.
- ``settings.py``: platform settings from the environment.
- ``http.py``: the HTTP entry point.

This module stays import-light so the hook works without the extra.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

ENABLE_VARIABLE = "COGNITO_USER_POOL_ID"


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(ENABLE_VARIABLE, "").strip())


def run() -> None:
    from zammad_mcp.platform.http import run as run_platform

    run_platform()
