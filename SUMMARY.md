# UnifAI MCP Server — Summary

## Overview

The UnifAI MCP server exposes the UnifAI multi-agent workflow orchestration platform over MCP (Model Context Protocol), enabling any MCP-compatible client (Cursor, Claude Desktop, etc.) to run AI workflows with Red Hat SSO authentication.

## Tool Categories

### Workflow Execution (6 tools)
- `authenticate` — SSO login, session/workflow discovery
- `list_workflows` — list available workflows
- `run_workflow` — execute a workflow by name or ID
- `get_session_chat` — retrieve session history/output
- `list_sessions` — browse session history with filtering
- `list_recent_5_sessions` — quick access to latest sessions

### Resource Management (7 tools)
- `list_resources` — list saved resources with optional category/type filters
- `get_resource_details` — full resource config with resolved `$ref` names
- `create_resource` — create new resources (agents, LLMs, tools, providers, retrievers)
- `update_resource` — modify existing resource config
- `delete_resource` — remove resources
- `list_catalog` — discover available element types
- `get_element_schema` — get config schema for any element type

### Workflow Management (6 tools)
- `get_workflow_details` — full workflow definition (nodes, plan, providers)
- `get_workflow_schema` — JSON schema for workflow drafts
- `create_workflow` — create new workflows
- `update_workflow` — modify existing workflows
- `validate_workflow` — validate drafts before saving
- `delete_workflow` — remove workflows

## Architecture

- **Transport**: Streamable HTTP (`/mcp`)
- **Auth**: OAuth 2.1 with Red Hat SSO via UnifAI Identity Service
- **Client**: Async HTTP client (`unifai_client.py`) with caching (5-min TTL)
- **Security**: SSL verification enabled by default

## Key Design Decisions

- All "blueprint" terminology in user-facing tools has been replaced with "workflow" for clarity
- The `get_resource_details` tool resolves `$ref` resource IDs to their names for readability
- Internal client methods and API endpoints still use "blueprint" since that's the backend API contract
- In-memory auth state (no disk persistence) — restarts require re-authentication
