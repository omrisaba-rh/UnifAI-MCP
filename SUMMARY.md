# UnifAI MCP Server — Summary

## Overview

The UnifAI MCP server exposes the UnifAI multi-agent workflow orchestration platform over MCP (Model Context Protocol), enabling any MCP-compatible client (Cursor, Claude Desktop, etc.) to run AI workflows with Red Hat SSO authentication.

## Tool Categories

### Discovery & Guidance (2 tools)
- `get_startup_context` — display name, teams, session/workflow discovery (call first each conversation)
- `get_guide` — interactive guides: quick_start, workflow_patterns, llm_selection, resource_types, build_agent, build_workflow, system_prompts

### Workflow Execution (5 tools)
- `list_workflows` — list available workflows (personal, or optional `team` for a team workspace)
- `run_workflow` — execute a workflow by name or ID; optional `team` creates the session under that team
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
- `create_workflow` — create new workflows (auto-enriches `$ref` entries)
- `update_workflow` — modify existing workflows (auto-enriches `$ref` entries)
- `validate_workflow` — validate drafts before saving with grouped pass/fail output, reasons, and dependency chains (30s timeout, auto-enriches `$ref` entries)
- `delete_workflow` — remove workflows

## Architecture

- **Transport**: Streamable HTTP (`/mcp`), with optional native HTTPS via `SSL_CERTFILE` / `SSL_KEYFILE`
- **Auth**: OAuth 2.1 with Red Hat SSO via UnifAI Identity Service
- **Client**: Async HTTP client (`unifai_client.py`) with caching (5-min TTL); Identity Service used for team listing
- **Security**: Outbound SSL verification enabled by default
- **Guidance**: MCP-native — server instructions + `get_guide` (all clients). Optional host-local rules must never be required.

## Key Design Decisions

- **Client-agnostic**: The MCP must not depend on Cursor (or any host) specific config. Every capability ships as MCP tools, tool descriptions, and server instructions so any MCP client gets full behavior.
- All "blueprint" terminology in user-facing tools has been replaced with "workflow" for clarity
- The `get_resource_details` tool resolves `$ref` resource IDs to their names for readability
- Internal client methods and API endpoints still use "blueprint" since that's the backend API contract
- Team ownership is set at session create time (`teamId`); `list_workflows` / `run_workflow` accept a `team` name or ID and resolve it via the Identity Service
- In-memory auth state (no disk persistence) — restarts require re-authentication
- Host-local files (e.g. `.cursor/rules/`) are optional convenience only; they must duplicate—not replace—MCP instructions
- `create_workflow`, `update_workflow`, and `validate_workflow` auto-enrich `$ref` entries with `name` and `type` to prevent backend validation errors
