"""Optional platform mode: Cognito/mPass SSO with per-user minted tokens (FOSS-513).

Enabled only when ``COGNITO_USER_POOL_ID`` is set and the ``[platform]`` extra
is installed. The community core never imports from here.

Planned modules: ``cognito.py`` (OAuth provider), ``identity.py``,
``storage.py`` (Valkey + Fernet), ``http.py`` (server entry point),
``mint.py`` and ``ceiling.py`` (per-user Zammad token minting bounded by the
user's role), and ``credentials.py``.
"""
