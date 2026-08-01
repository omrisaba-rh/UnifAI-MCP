# UnifAI MCP Improvements

## v0.5.0 — Team Workspaces & Native HTTPS (2026-08-01)

### New: Team Workspace Support

`list_workflows` and `run_workflow` accept an optional `team` argument (name or ID):

- Resolves the team via the UnifAI Identity Service (`/api/teams/teams.list`)
- Lists workflows from the team workspace (`identityType=team`)
- Creates sessions under that team so they appear in the team UI
- Server instructions and Cursor rule tell clients to pass `team=` for team requests (e.g. "UIE Agent")

### New: Native HTTPS

- When `SSL_CERTFILE` and `SSL_KEYFILE` are set, Uvicorn serves the MCP endpoint over HTTPS
- `.env.example` documents the TLS variables; README covers client trust for self-signed certs

---

## v0.4.0 — Guided User Experience & Validation UX (2026-07-09)

### New: Interactive Guidance System

A layered system designed for cross-client compatibility (Cursor, Claude Code, etc.):

**Layer 1 — Enhanced Server Instructions** (works in every MCP client):
- UX directives embedded in the `FastMCP()` instructions parameter
- Rules: always offer 2-3 options, discover before building, explain trade-offs, validate before saving
- Quick reference for key concepts, workflow patterns, and available guides

**Layer 2 — `get_guide` Tool** (works in every MCP client):
- 7 detailed playbooks: `quick_start`, `workflow_patterns`, `llm_selection`, `resource_types`, `build_agent`, `build_workflow`, `system_prompts`
- Each guide includes step-by-step instructions, decision matrices, examples, tips, and anti-patterns
- Designed to walk new users from zero to a working workflow

**Layer 3 — Cursor Rule** (Cursor-specific bonus):
- `.cursor/rules/unifai-guide.mdc` provides persistent Cursor-specific guidance
- Covers first contact, building resources/workflows, and new user experience

### Improved: Auto-Enrichment of `$ref` Entries

- `create_workflow`, `update_workflow`, and `validate_workflow` now auto-populate missing `name` and `type` fields for `$ref` resources
- Prevents Pydantic validation errors from the backend
- Fetches metadata in parallel for referenced resources

### Improved: Workflow Validation UX

**User-friendly output** — the `validate_workflow` tool now produces structured, actionable results:
- Summary line with counts: `VALID (13 resources checked)` or `INVALID (4 failed, 9 passed)`
- Failed resources listed first with reason, element type, and dependency chain
- Passed resources grouped separately so failures aren't buried in noise
- Informational note when failures are likely due to known backend limitations (OAuth providers in draft mode)

**Better timeout handling** — validation now sends `timeoutSeconds=30` to the backend (up from the default 10s), giving MCP provider connectivity probes adequate time to complete.

**Transparent errors** — API errors now surface the actual backend error body instead of a generic HTTP status message, making it easier to diagnose issues.

---

## v0.3.0 — Resource & Workflow Management (2026-07-09)

### New: Resource Management Tools

Added full CRUD for UnifAI resources (agents, LLMs, tools, providers, retrievers):

- `list_resources` — browse with optional category/type filters
- `get_resource_details` — view full configuration of any resource, with `$ref` IDs resolved to resource names
- `create_resource` — create new resources using catalog schemas
- `update_resource` — modify existing resource config or name
- `delete_resource` — remove resources

The `get_resource_details` tool fetches all referenced resources in parallel and annotates both the summary and the configuration JSON with human-readable names.

### New: Workflow Management Tools

Renamed and expanded the blueprint management tools:

- `get_workflow_schema` — JSON schema for composing workflow drafts
- `create_workflow` — create workflows from JSON drafts (auto-enriches `$ref` entries)
- `update_workflow` — update existing workflows in-place (auto-enriches `$ref` entries)
- `validate_workflow` — dry-run validation before saving (auto-enriches `$ref` entries)
- `delete_workflow` — remove workflows
- `get_workflow_details` — full workflow definition with nodes and execution plan

### New: Catalog Tools

- `list_catalog` — discover all available element types by category
- `get_element_schema` — get the config schema for any element type before creating resources

### Renamed: Blueprint → Workflow

All user-facing tool names and output strings now use "workflow" instead of "blueprint":
- `get_blueprint_details` → `get_workflow_details`
- `get_blueprint_schema` → `get_workflow_schema`
- `create_blueprint` → `create_workflow`
- `update_blueprint` → `update_workflow`
- `validate_blueprint` → `validate_workflow`
- `delete_blueprint` → `delete_workflow`

Internal client methods retain "blueprint" naming since it matches the backend API contract.

---

## v0.2.0 — Error Handling, Caching & Security (2026-06-29)

### Error Handling & Resilience
- 5-minute timeout protection for workflow execution
- Retry logic for stream status checks
- Session IDs included in error messages for manual recovery

### Progress Reporting
- Elapsed time tracking during workflow execution
- Progress updates every 30 seconds

### Caching
- In-memory cache for workflow lookups (5-minute TTL)
- Per-user cache keys
- `clear_cache()` method for manual invalidation

### Security
- SSL verification enabled by default
- Configurable via `VERIFY_SSL` environment variable
- Warning log when SSL verification is disabled
