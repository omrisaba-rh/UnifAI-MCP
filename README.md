# UnifAI MCP Server

MCP server for [UnifAI](https://github.com/redhat-community-ai-tools/UnifAI) — run multi-agent AI workflows from any MCP client (Cursor, Claude Desktop, etc.) with Red Hat SSO authentication.

## Features

- **OAuth 2.1 with Red Hat SSO** — authentication via the UnifAI Identity Service (Keycloak proxy), with in-memory token management
- **Streamable HTTP transport** — production-ready `/mcp` endpoint
- **Dynamic Client Registration** — MCP clients self-register locally (no Keycloak redirect-URI allowlist needed)
- **Automatic workflow discovery** — on authentication, available workflows are loaded into the LLM context for intelligent routing
- **Concurrent data loading** — sessions and workflows are fetched in parallel for fast startup

## Tools

| Tool | Description |
|------|-------------|
| `authenticate` | Check auth status, display profile & recent sessions, and silently load available workflows into context |
| `list_workflows` | Explicitly list all available workflows (blueprints) with full details |
| `run_workflow` | Run a UnifAI workflow by blueprint name or ID with a user prompt |
| `get_session_chat` | Retrieve the chat history and output of a previous workflow session |

## How Workflow Routing Works

When `authenticate` is called at conversation start, the server fetches both recent sessions and available workflows concurrently. The workflow list is returned with curated routing hints (defined in `WORKFLOW_HINTS`) that tell the LLM **when** to use each workflow:

```
• AskRH — Red Hat product knowledge (RHEL, OpenShift, Ansible, security, lifecycle)
• Web Search — Internet search for current events, public docs, non-Red Hat topics
• Deep Agent Jira — Create, search, or update Jira issues
• Google flow — Gmail, Calendar, Drive requests
...
```

The LLM is instructed to use these silently — it picks the most appropriate workflow for the user's request without displaying the list. This avoids the user needing to know workflow names while ensuring accurate routing.

To add routing hints for new workflows, add entries to the `WORKFLOW_HINTS` dict in `server.py`. Workflows not in the dict fall back to their `spec_dict["description"]` from the UnifAI backend.

## Architecture

```
MCP Client (Cursor, Claude, etc.)
    │
    │  1. Discover AS via /.well-known/oauth-protected-resource
    │  2. Dynamic client registration (in-memory)
    │  3. /authorize → redirect to UnifAI Identity Service → Keycloak SSO
    │  4. Identity Service callback with session cookie + user info
    │  5. Bearer token on every subsequent request
    │
    ▼
┌──────────────────┐         ┌──────────────────────────┐
│  UnifAI MCP      │────────►│  UnifAI Identity Service │
│  Server (:13456) │         │  (Keycloak SSO proxy)    │
│  /mcp            │         └──────────────────────────┘
└────────┬─────────┘
         │
         │  REST API (session cookie forwarded)
         ▼
┌──────────────────┐
│  UnifAI MAS      │
│  Backend (/api2) │
└──────────────────┘
```

**Authentication flow:**
1. Cursor discovers the OAuth AS via protected-resource metadata
2. Client registers itself locally (in-memory DCR)
3. `/authorize` redirects the browser to the UnifAI Identity Service SSO endpoint
4. The Identity Service handles Keycloak login and redirects back with a pre-signed session cookie
5. The MCP server issues its own access/refresh tokens (in-memory, 1h/24h TTL)
6. The session cookie is embedded in token claims and forwarded to the UnifAI backend on API calls

**No credentials are persisted to disk** — all auth state lives in process memory and is lost on restart.

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env if your UnifAI backend or SSO URLs differ from defaults
```

### 3. Run

```bash
unifai-mcp
# or
python -m unifai_mcp.server
```

The server starts on `http://127.0.0.1:13456` with the MCP endpoint at `/mcp`.

### 4. Connect from an MCP Client

Add this to your MCP client config (e.g. Cursor `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "unifai": {
      "url": "http://127.0.0.1:13456/mcp"
    }
  }
}
```

On first connection the client will:
1. Discover the AS via `/.well-known/oauth-protected-resource`
2. Register itself dynamically (local in-memory registration)
3. Open a browser for Red Hat SSO login (via Identity Service)
4. Receive the token via localhost callback

## Configuration

All settings are read from environment variables or a `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HOST` | `0.0.0.0` | Server bind address |
| `MCP_PORT` | `13456` | Server port |
| `MCP_SERVER_URL` | `http://127.0.0.1:13456` | Public URL of this server (used in OAuth metadata) |
| `SSO_URL` | *(staging URL)* | UnifAI Identity Service URL (handles Keycloak SSO) |
| `UNIFAI_BASE_URL` | *(staging URL)* | UnifAI deployment URL |
| `UNIFAI_API_PREFIX` | `/api2` | API path prefix on the UnifAI deployment |

## Project Structure

```
src/unifai_mcp/
├── server.py           # MCP server, tools, workflow routing hints
├── config.py           # Pydantic settings (env vars / .env)
├── unifai_client.py    # Async HTTP client for the UnifAI backend
└── auth/
    ├── provider.py     # OAuth AS provider (Identity Service integration)
    ├── settings.py     # MCP auth settings (scopes, registration)
    └── token_verifier.py
```

## License

Apache-2.0
