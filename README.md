# UnifAI MCP Server

MCP server for [UnifAI](https://github.com/redhat-community-ai-tools/UnifAI) — run multi-agent AI workflows from any MCP client (Cursor, Claude Desktop, etc.) with Red Hat SSO authentication.

## Features

- **OAuth 2.1 with Red Hat SSO (Keycloak)** — per-user sessions via Bearer token introspection
- **Streamable HTTP transport** — production-ready `/mcp` endpoint
- **Dynamic Client Registration** — MCP clients self-register with Keycloak (RFC 7591)
- **Protected Resource Metadata** — auto-discovery of the Authorization Server (RFC 9728)

## Tools

| Tool | Description |
|------|-------------|
| `authenticate` | Check auth status and display your Red Hat SSO profile |
| `run_workflow` | Run a UnifAI workflow by blueprint name or ID |

## Architecture

```
MCP Client (Cursor, Claude, etc.)
    │
    │  1. Discover AS via /.well-known/oauth-protected-resource
    │  2. Dynamic client registration (RFC 7591)
    │  3. Authorization code + PKCE → localhost callback
    │  4. Bearer token on every request
    │
    ▼                            token introspection
┌──────────────────┐            ┌──────────────────┐
│  UnifAI MCP      │───────────►│  Red Hat SSO     │
│  Server (:8080)  │            │  (Keycloak)      │
│  /mcp            │            │  EmployeeIDP     │
└────────┬─────────┘            └──────────────────┘
         │
         │  REST API (bearer token forwarded)
         ▼
┌──────────────────┐
│  UnifAI MAS      │
│  Backend         │
└──────────────────┘
```

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env if your UnifAI backend or Keycloak differ from defaults
```

### 3. Run

```bash
unifai-mcp
# or
python -m unifai_mcp.server
```

The server starts on `http://localhost:8080` with the MCP endpoint at `/mcp`.

### 4. Connect from an MCP Client

Add this to your MCP client config (e.g. Cursor `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "unifai": {
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

On first connection the client will:
1. Discover Keycloak via `/.well-known/oauth-protected-resource`
2. Register itself dynamically (RFC 7591)
3. Open a browser for Red Hat SSO login
4. Receive the token via localhost callback

## Configuration

All settings are read from environment variables or a `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HOST` | `0.0.0.0` | Server bind address |
| `MCP_PORT` | `8080` | Server port |
| `MCP_SERVER_URL` | `http://localhost:8080` | Public URL of this server (used in OAuth resource metadata) |
| `KEYCLOAK_BASE_URL` | `https://auth.stage.redhat.com/auth` | Keycloak base URL |
| `KEYCLOAK_REALM` | `EmployeeIDP` | Keycloak realm |
| `CLIENT_ID` | `TAG-001` | Keycloak confidential client ID (for token introspection) |
| `CLIENT_SECRET` | *(set in .env)* | Keycloak confidential client secret |
| `UNIFAI_BASE_URL` | *(staging URL)* | UnifAI deployment URL |
| `UNIFAI_API_PREFIX` | `/api2` | API path prefix on the UnifAI deployment |

## License

Apache-2.0
