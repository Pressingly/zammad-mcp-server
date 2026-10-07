"""A fake SSO-fronted Zammad for platform-mode tests, modelled on the Phase 0 spikes."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.conftest import RecordingTransport
from zammad_mcp.platform.identity import Identity

AGENT_PERMISSIONS = (
    "chat",
    "chat.agent",
    "knowledge_base",
    "knowledge_base.reader",
    "ticket",
    "ticket.agent",
    "user_preferences",
    "user_preferences.access_token",
    "user_preferences.device",
)
CUSTOMER_PERMISSIONS = ("ticket", "ticket.customer", "user_preferences", "user_preferences.access_token")
ADMIN_PERMISSIONS = ("admin", "admin.user", "knowledge_base", "knowledge_base.editor", "report", "user_preferences")
COOKIE_NAME = "_zammad_session_a138cfd0f37"
TOKEN_PATH = "/api/v1/user_access_token"
SIGNOUT_PATH = "/api/v1/signout"
DELETE_PATH = re.compile(r"^/api/v1/user_access_token/(\d+)$")


@dataclass
class FakeUser:
    permissions: tuple[str, ...]
    tokens: list[dict[str, Any]] = field(default_factory=list)
    token_access: bool = True
    corporate: bool = True


class FakeSsoZammad(RecordingTransport):
    """Header sessions, CSRF, minting and token auth, with the quirks the spikes recorded."""

    def __init__(self, *, corporate_gate: bool = False) -> None:
        self.users: dict[str, FakeUser] = {}
        self.sessions: dict[str, tuple[str, str]] = {}
        self.minted: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.revoked: set[str] = set()
        self.created_users: list[str] = []
        self.corporate_gate = corporate_gate
        self.deleted: list[int] = []
        self.signed_out: list[str] = []
        self.next_id = 100
        super().__init__(self._dispatch)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return await super().handle_async_request(request)

    def add(self, email: str, permissions: tuple[str, ...], **options: Any) -> FakeUser:
        self.users[email] = FakeUser(permissions=permissions, **options)
        return self.users[email]

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path == TOKEN_PATH]

    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET" and r.url.path == TOKEN_PATH]

    def deletes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "DELETE"]

    def posted_body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.posts()[index].content)

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        email = request.headers.get("x-auth-request-email")
        if email is not None:
            denial = self._middleware(request, email.lower())
            if denial is not None:
                return denial
            if request.method != "GET" or request.url.path != TOKEN_PATH:
                return _error(401, "CSRF token verification failed.")
            return self._open(email.lower())
        if request.url.path == SIGNOUT_PATH:
            return self._signout(request)
        if request.url.path == TOKEN_PATH and request.method == "POST":
            return self._with_session(request, self._create)
        match = DELETE_PATH.match(request.url.path)
        if match and request.method == "DELETE":
            return self._with_session(request, lambda owner, req: self._delete(owner, int(match.group(1))))
        return self._token_auth(request)

    def _middleware(self, request: httpx.Request, email: str) -> httpx.Response | None:
        user = self.users.get(email)
        if user is None:
            self.created_users.append(email)
            user = self.add(email, CUSTOMER_PERMISSIONS, token_access=False)
        if self.corporate_gate and (not request.headers.get("x-auth-request-access-token") or not user.corporate):
            return _error(403, "access_denied")
        if "ticket.agent" in user.permissions and not request.headers.get("x-browser-fingerprint"):
            return _error(422, "Need fingerprint param!")
        return None

    def _open(self, email: str) -> httpx.Response:
        user = self.users[email]
        sid = f"sid-{len(self.sessions)}"
        csrf = f"csrf-{len(self.sessions)}"
        self.sessions[sid] = (email, csrf)
        cookie = {"set-cookie": f"{COOKIE_NAME}={sid}; path=/; secure; httponly"}
        if not user.token_access:
            return httpx.Response(403, json={"error": "User authorization failed."}, headers=cookie)
        permissions = [{"id": index, "name": name, "active": True} for index, name in enumerate(user.permissions)]
        body = {"tokens": user.tokens, "permissions": permissions}
        return httpx.Response(200, json=body, headers={**cookie, "csrf-token": csrf})

    def live_sessions(self) -> list[str]:
        return [sid for sid in self.sessions if sid not in self.signed_out]

    def signouts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == SIGNOUT_PATH]

    def _signout(self, request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        sid = cookie.removeprefix(f"{COOKIE_NAME}=")
        if sid in self.sessions:
            self.signed_out.append(sid)
        return httpx.Response(200, json={}, headers={"set-cookie": f"{COOKIE_NAME}=fresh; path=/; secure"})

    def _with_session(self, request: httpx.Request, action) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        sid = cookie.removeprefix(f"{COOKIE_NAME}=") if cookie.startswith(f"{COOKIE_NAME}=") else None
        if sid not in self.sessions or sid in self.signed_out:
            return _error(403, "Authentication required")
        owner, csrf = self.sessions[sid]
        if request.headers.get("x-csrf-token") != csrf:
            return _error(401, "CSRF token verification failed.")
        return action(owner, request)

    def _create(self, owner: str, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        token = f"minted-{len(self.minted) + 1}"
        self.minted[token] = (owner, tuple(body["permission"]))
        self.next_id += 1
        self.users[owner].tokens.append({"id": self.next_id, "name": body["name"], "expires_at": body["expires_at"]})
        return httpx.Response(200, json={"token": token})

    def _delete(self, owner: str, token_id: int) -> httpx.Response:
        tokens = self.users[owner].tokens
        if not any(token["id"] == token_id for token in tokens):
            return _error(422, "The API token could not be found.")
        self.users[owner].tokens = [token for token in tokens if token["id"] != token_id]
        self.deleted.append(token_id)
        return httpx.Response(200, json={})

    def _token_auth(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Token token=")
        if token not in self.minted or token in self.revoked:
            return _error(401, "Not authorized (token expired)!")
        owner, permissions = self.minted[token]
        held = set(permissions) & set(self.users[owner].permissions)
        if request.url.path == "/api/v1/users/me":
            return httpx.Response(200, json={"id": 1, "login": owner, "email": owner, "role_ids": [], "active": True})
        if request.url.path == "/api/v1/users/search" and "ticket.agent" not in held:
            return _error(403, "Not authorized")
        return httpx.Response(200, json=[])


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": message})


def identity(
    email: str = "ada@example.com", *, access_token: str | None = "upstream-access", username: str | None = None
) -> Identity:
    claims = {"id_token": "id", **({"cognito:username": username} if username else {})}
    return Identity(email=email, access_token=access_token, claims=claims)
