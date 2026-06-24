"""Keycloak token verification via RFC 7662 introspection."""

import logging

import httpx

from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)


class KeycloakIntrospectionVerifier:
    """Validates Bearer tokens by calling Keycloak's introspection endpoint.

    The MCP server acts as a Resource Server (RS).  Keycloak is the
    Authorization Server (AS).  On every request the RS sends the bearer
    token to Keycloak's ``/token/introspect`` endpoint (RFC 7662) using
    its own confidential-client credentials.
    """

    def __init__(
        self,
        introspection_url: str,
        client_id: str,
        client_secret: str,
        resource_url: str,
    ):
        self._introspection_url = introspection_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self._introspection_url,
                data={
                    "token": token,
                    "token_type_hint": "access_token",
                },
                auth=(self._client_id, self._client_secret),
            )

            if resp.status_code != 200:
                logger.warning(
                    "Introspection request failed: status=%s body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
                return None

            data = resp.json()
            if not data.get("active"):
                return None

            scopes = data.get("scope", "").split() if data.get("scope") else []
            expires_at = data.get("exp")

            return AccessToken(
                token=token,
                client_id=data.get("client_id", self._client_id),
                scopes=scopes,
                expires_at=expires_at,
                resource=self._resource_url,
                subject=data.get("sub"),
                claims={
                    "iss": data.get("iss", ""),
                    "preferred_username": data.get("preferred_username", ""),
                    "email": data.get("email", ""),
                    "name": data.get("name", ""),
                    "given_name": data.get("given_name", ""),
                    "family_name": data.get("family_name", ""),
                },
            )
