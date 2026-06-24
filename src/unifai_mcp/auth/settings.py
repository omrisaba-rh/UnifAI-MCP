"""MCP AuthSettings — our server is both AS and RS."""

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions


def create_auth_settings(
    issuer_url: str,
    resource_server_url: str,
) -> AuthSettings:
    """Build AuthSettings where our own server is the issuer (AS).

    Args:
        issuer_url: Base URL of this server (e.g. ``http://localhost:8081``).
            Auth routes (``/authorize``, ``/token``, ``/register``) are
            relative to this.
        resource_server_url: The URL that MCP clients connect to
            (e.g. ``http://localhost:8081/mcp``).  Must match the
            ``url`` in the client's MCP config so the protected-resource
            metadata ``resource`` field passes Cursor's origin check.
    """
    return AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=resource_server_url,
        required_scopes=["openid"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["openid", "profile", "email"],
            default_scopes=["openid", "profile", "email"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
