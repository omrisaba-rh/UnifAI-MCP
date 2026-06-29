from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MCP server
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 13456
    mcp_server_url: str = "http://127.0.0.1:13456"

    # UnifAI Identity Service (SSO)
    sso_url: str = (
        "https://unifai-identity-tag-ai--pipeline"
        ".apps.stc-ai-e1-pp.imap.p1.openshiftapps.com"
    )

    # UnifAI backend (Multi-Agent System)
    unifai_base_url: str = (
        "https://unifai-ui-tag-ai--pipeline"
        ".apps.stc-ai-e1-pp.imap.p1.openshiftapps.com"
    )
    unifai_api_prefix: str = "/api2"

    # Security settings
    verify_ssl: bool = True  # SSL certificate verification (disable only for dev/testing)

    # TLS termination (Uvicorn serves HTTPS when both are set)
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None

    @property
    def mcp_resource_url(self) -> str:
        """URL that MCP clients actually connect to (origin + /mcp path)."""
        return f"{self.mcp_server_url.rstrip('/')}/mcp"

    @property
    def unifai_api_url(self) -> str:
        base = self.unifai_base_url.rstrip("/")
        prefix = self.unifai_api_prefix.rstrip("/")
        return f"{base}{prefix}"
