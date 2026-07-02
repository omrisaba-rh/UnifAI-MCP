"""MCP OAuth Authorization Server that delegates login to the UnifAI Identity Service.

Instead of talking to Keycloak directly, we redirect the browser to the
UnifAI Identity Service SSO endpoint.  The Identity Service handles the
Keycloak exchange and redirects the browser back to our local callback
with a pre-signed session cookie + user info.

This avoids the need to register every developer's localhost in Keycloak's
redirect-URI allowlist — only the Identity Service's own callback is
registered there.

Flow:
    Cursor ──DCR──► this server (in-memory)
                       │
                       │ /authorize → redirect to Identity Service SSO
                       │ /api/auth/callback ← Identity Service redirects back
                       │ /token → return MCP tokens (embed session cookie)
                       ▼
                  Identity Service → Keycloak (transparent to us)
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
import urllib.parse
from typing import Any

from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

CODE_TTL = 300
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 86400


class IdentityServiceProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """MCP OAuth AS that delegates authentication to the UnifAI Identity Service."""

    def __init__(self, sso_url: str, server_url: str):
        self._sso_url = sso_url.rstrip("/")
        self._server_url = server_url.rstrip("/")

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending_auth: dict[str, dict[str, Any]] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._user_data: dict[str, dict[str, Any]] = {}
        self._token_sessions: dict[str, str] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.redirect_uris:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="At least one redirect_uri is required",
            )
        if not client_info.client_id:
            client_info.client_id = f"mcp-{secrets.token_hex(12)}"
            client_info.client_id_issued_at = int(time.time())
        self._clients[client_info.client_id] = client_info
        logger.info("Registered MCP client %s (local)", client_info.client_id)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        our_state = secrets.token_urlsafe(32)

        self._pending_auth[our_state] = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes,
            "original_state": params.state,
            "resource": params.resource,
            "created_at": time.time(),
        }

        callback_url = f"{self._server_url}/api/auth/callback/{our_state}"

        state_payload = {"cli": True, "callbackUrl": callback_url}
        state_b64 = base64.b64encode(
            json.dumps(state_payload).encode()
        ).decode()

        login_url = (
            f"{self._sso_url}/api/auth/login?state="
            f"{urllib.parse.quote(state_b64)}"
        )

        return login_url

    async def handle_identity_callback(
        self,
        user_b64: str,
        state_key: str,
    ) -> str:
        """Process the Identity Service callback and return a redirect URL
        back to the MCP client (Cursor).
        """
        pending = self._pending_auth.pop(state_key, None)
        if not pending:
            raise ValueError("Unknown or expired state parameter")

        padded = user_b64 + "=" * (-len(user_b64) % 4)
        user_data = json.loads(base64.urlsafe_b64decode(padded).decode())

        logger.debug(
            "Identity callback user_data keys: %s", list(user_data.keys())
        )

        mcp_code = secrets.token_urlsafe(32)

        self._auth_codes[mcp_code] = AuthorizationCode(
            code=mcp_code,
            scopes=pending.get("scopes") or list(("openid", "profile", "email")),
            expires_at=time.time() + CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=pending["code_challenge"],
            redirect_uri=AnyUrl(pending["redirect_uri"]),
            redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            resource=pending.get("resource"),
            subject=user_data.get("sub") or user_data.get("username", ""),
        )

        self._user_data[mcp_code] = user_data

        redirect_uri = construct_redirect_uri(
            pending["redirect_uri"],
            code=mcp_code,
            state=pending.get("original_state"),
        )

        return redirect_uri

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        ac = self._auth_codes.get(authorization_code)
        if not ac:
            return None
        if time.time() > ac.expires_at:
            self._auth_codes.pop(authorization_code, None)
            return None
        if ac.client_id != client.client_id:
            return None
        return ac

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)
        user_data = self._user_data.pop(authorization_code.code, {})

        access_token_str = secrets.token_urlsafe(48)
        refresh_token_str = secrets.token_urlsafe(48)
        now = int(time.time())

        logger.debug(
            "exchange_authorization_code: session_cookie present=%s",
            bool(user_data.get("session_cookie") or user_data.get("sessionCookie")),
        )

        session_cookie = (
            user_data.get("session_cookie", "")
            or user_data.get("sessionCookie", "")
        )

        self._access_tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=client.client_id or "",
            scopes=authorization_code.scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
            claims={
                "iss": self._server_url,
                "preferred_username": user_data.get("username", ""),
                "email": user_data.get("email", ""),
                "name": user_data.get("name", ""),
            },
        )

        if session_cookie:
            self._token_sessions[access_token_str] = session_cookie

        self._refresh_tokens[refresh_token_str] = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id or "",
            scopes=authorization_code.scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            subject=authorization_code.subject,
        )

        self._user_data[refresh_token_str] = user_data

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh_token_str,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        at = self._access_tokens.get(token)
        if not at:
            return None
        if at.expires_at and time.time() > at.expires_at:
            self._access_tokens.pop(token, None)
            return None
        return at

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        rt = self._refresh_tokens.get(refresh_token)
        if not rt:
            return None
        if rt.client_id != client.client_id:
            return None
        if rt.expires_at and time.time() > rt.expires_at:
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)
        user_data = self._user_data.pop(refresh_token.token, {})

        now = int(time.time())
        new_access = secrets.token_urlsafe(48)
        new_refresh = secrets.token_urlsafe(48)
        effective_scopes = scopes or refresh_token.scopes

        session_cookie = (
            user_data.get("session_cookie", "")
            or user_data.get("sessionCookie", "")
        )

        self._access_tokens[new_access] = AccessToken(
            token=new_access,
            client_id=client.client_id or "",
            scopes=effective_scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            subject=refresh_token.subject,
            claims={
                "iss": self._server_url,
                "preferred_username": user_data.get("username", ""),
                "email": user_data.get("email", ""),
                "name": user_data.get("name", ""),
            },
        )

        if session_cookie:
            self._token_sessions[new_access] = session_cookie

        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id or "",
            scopes=effective_scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            subject=refresh_token.subject,
        )

        self._user_data[new_refresh] = user_data

        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(effective_scopes),
            refresh_token=new_refresh,
        )

    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
            self._token_sessions.pop(token.token, None)
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)
            self._user_data.pop(token.token, None)

    def get_session_cookie(self, token: str) -> str | None:
        """Look up the backend session cookie for a given access token."""
        return self._token_sessions.get(token)
