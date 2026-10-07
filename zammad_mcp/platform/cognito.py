"""Cognito provider that keeps the upstream id_token's identity for every request.

FastMCP's ``AWSCognitoProvider`` validates the Cognito access token, which
carries no ``email`` and, for federated users, an opaque ``username``. The
mint needs the identity the browser login uses, so this provider:

- decodes the id_token at exchange and refresh time and keeps
  ``{id_token, email, cognito:username}`` (:meth:`_extract_upstream_claims`);
- re-reads the id_token, and the raw upstream access token for the
  corporate-ID gate, from the upstream token store on every request and fails
  closed when it is missing (:meth:`load_access_token`). The copy sealed in
  the issued JWT is a snapshot that a transparent refresh never updates;
- corrects the upstream ``expires_in`` on both grants
  (:func:`_correct_expires_in`).

The id_token is decoded without signature verification. That is safe: it
comes from Cognito's token endpoint over the provider's own server-to-server
call, never from the MCP client, and the inbound access token is still
JWKS-verified on every request.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.aws import AWSCognitoProvider

logger = logging.getLogger(__name__)

UPSTREAM_CLAIMS_KEY = "upstream_claims"
ID_TOKEN_KEY = "id_token"
EMAIL_CLAIM = "email"
COGNITO_USERNAME_CLAIM = "cognito:username"
PREFERRED_USERNAME_CLAIM = "preferred_username"
ACCESS_TOKEN_CLAIM = "access_token"

MIN_EXPIRES_IN_SECONDS = 60
EXPIRES_IN_DRIFT_TOLERANCE_SECONDS = 1


def _true_expires_in(token_response: dict[str, Any]) -> int | None:
    """The upstream access token's real remaining lifetime, from its own ``exp``.

    mpass-auth-proxy reports the browser cookie lifetime (days) as
    ``expires_in`` while the Cognito token expires in an hour. FastMCP gates
    its transparent refresh on that value, so left alone the refresh never
    fires and the client is sent through a full re-authorization hourly.
    The floor keeps an already-expired token from yielding a negative TTL.
    """
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    try:
        exp = jwt.decode(access_token, options={"verify_signature": False}).get("exp")
    except jwt.PyJWTError as exc:
        logger.warning("could not decode the upstream access_token to read exp: %s", exc)
        return None
    if not isinstance(exp, int | float):
        return None
    return max(int(exp - time.time()), MIN_EXPIRES_IN_SECONDS)


def _already_truthful(reported: Any, corrected: int) -> bool:
    if isinstance(reported, bool) or not isinstance(reported, int | float):
        return False
    return abs(reported - corrected) <= EXPIRES_IN_DRIFT_TOLERANCE_SECONDS


def _correct_expires_in(response: httpx.Response) -> httpx.Response:
    """authlib compliance hook: rewrite ``expires_in`` to the token's real lifetime."""
    if response.status_code != 200:
        return response
    try:
        body = response.json()
    except ValueError:
        return response
    if not isinstance(body, dict):
        return response
    corrected = _true_expires_in(body)
    if corrected is None or _already_truthful(body.get("expires_in"), corrected):
        return response
    return httpx.Response(
        status_code=response.status_code,
        json={**body, "expires_in": corrected},
        request=response.request,
    )


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _field(record: Any, name: str) -> Any:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


class ZammadCognitoProvider(AWSCognitoProvider):
    def _create_upstream_oauth_client(self) -> AsyncOAuth2Client:
        """One factory serves both the code exchange and the refresh grant, so one registration covers both."""
        client = super()._create_upstream_oauth_client()
        client.register_compliance_hook("access_token_response", _correct_expires_in)
        client.register_compliance_hook("refresh_token_response", _correct_expires_in)
        return client

    async def _extract_upstream_claims(
        self, idp_tokens: dict[str, Any], *, include_access_token: bool = False
    ) -> dict[str, Any] | None:
        """Claims sealed into the issued JWT; the raw access token only on the per-request path."""
        id_token = _string(idp_tokens.get(ID_TOKEN_KEY))
        if id_token is None:
            logger.warning("the Cognito token response has no id_token; the caller's identity is unknown")
            return None
        try:
            claims = jwt.decode(id_token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            logger.warning("could not decode the Cognito id_token: %s", exc)
            return None
        extracted = {
            ID_TOKEN_KEY: id_token,
            EMAIL_CLAIM: _string(claims.get(EMAIL_CLAIM)),
            COGNITO_USERNAME_CLAIM: _string(claims.get(COGNITO_USERNAME_CLAIM)),
            PREFERRED_USERNAME_CLAIM: _string(claims.get(PREFERRED_USERNAME_CLAIM)),
            ACCESS_TOKEN_CLAIM: _string(idp_tokens.get(ACCESS_TOKEN_CLAIM)) if include_access_token else None,
        }
        return {key: value for key, value in extracted.items() if value is not None}

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Attach the current id_token identity, or reject the request when it cannot be found."""
        validated = await super().load_access_token(token)
        if validated is None:
            return None
        upstream = await self._resolve_upstream_claims(token)
        if not upstream or not upstream.get(ID_TOKEN_KEY):
            logger.warning("could not resolve the upstream id_token for this session; rejecting the request")
            return None
        claims = {**(validated.claims or {}), UPSTREAM_CLAIMS_KEY: upstream}
        return validated.model_copy(update={"claims": claims})

    async def _resolve_upstream_claims(self, token: str) -> dict[str, Any] | None:
        """Follow the issued JWT's jti to the stored Cognito token response; ``None`` on any miss."""
        try:
            jti = _field(self.jwt_issuer.verify_token(token), "jti")
            if not jti:
                return None
            mapping = await self._jti_mapping_store.get(key=jti)
            upstream_id = _field(mapping, "upstream_token_id") if mapping else None
            if not upstream_id:
                return None
            token_set = await self._upstream_token_store.get(key=upstream_id)
            raw = _field(token_set, "raw_token_data") if token_set else None
            if not isinstance(raw, dict):
                return None
            return await self._extract_upstream_claims(raw, include_access_token=True)
        except Exception as exc:
            logger.warning("could not resolve the upstream id_token: %s", exc)
            return None
