"""Per-request Zammad credential resolution.

- ``base.py``: the :class:`CredentialProvider` protocol and :class:`ZammadCredential`.
- ``static.py``: the ``ZAMMAD_HTTP_TOKEN`` environment token.
- ``header.py``: a per-request ``X-Zammad-Token`` header on ``/http/api-key/mcp``.
"""

from zammad_mcp.credentials.base import CredentialProvider, Profile, ZammadCredential, token_identity
from zammad_mcp.credentials.header import HeaderCredentialProvider, MountRoutedCredentialProvider, api_key_mount
from zammad_mcp.credentials.static import StaticCredentialProvider

__all__ = [
    "CredentialProvider",
    "HeaderCredentialProvider",
    "MountRoutedCredentialProvider",
    "Profile",
    "StaticCredentialProvider",
    "ZammadCredential",
    "api_key_mount",
    "token_identity",
]
