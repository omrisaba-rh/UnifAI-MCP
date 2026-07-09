# UnifAI MCP Improvements

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
- `create_workflow` — create workflows from JSON drafts
- `update_workflow` — update existing workflows in-place
- `validate_workflow` — dry-run validation before saving
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
