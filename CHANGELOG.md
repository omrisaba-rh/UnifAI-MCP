# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Pre-run workflow validation** now calls `/blueprints/blueprint.validate` (same as the UnifAI UI) instead of rehydrating the workflow and posting to `/blueprints/draft.validate`. Draft validation has no authenticated credential context, which caused false INVALID results for OAuth MCP providers (e.g. "cancel scope" / connection failures) even when those providers were valid in the UI.
- **Startup tool visibility**: renamed `authenticate` → `get_user_context` → `get_startup_context` so clients that inject an OAuth helper (e.g. Cursor `mcp_auth`) do not hide or replace the UnifAI entrypoint that loads the display name, teams, and recent sessions. Cursor was still stripping `get_user_context` after OAuth reconnect; name/teams wording was scrubbed from the tool description and server instructions opener.
- **`list_recent_5_sessions` fallback**: also returns display name and teams so greeting/scope still work if the startup tool is unavailable to the client.

### Added
- **Post-auth workspace scope prompt**: `get_startup_context` lists the user's teams (when any) and instructs the client to ask Personal vs Team before continuing; server startup instructions remember that choice and pass `team=` to `list_workflows` / `run_workflow`. Users with no teams are not asked.
- **Team workspace support**: Optional `team` parameter on `list_workflows` and `run_workflow` resolves team name/ID via the Identity Service, lists team workflows, and creates sessions under the team workspace (visible in the team UI)
- **Native HTTPS**: Uvicorn TLS termination when `SSL_CERTFILE` and `SSL_KEYFILE` are set
- **`get_guide` tool**: Interactive guidance system with 7 topics — `quick_start`, `workflow_patterns`, `llm_selection`, `resource_types`, `build_agent`, `build_workflow`, `system_prompts`
- **Enhanced server instructions**: UX directives for LLM clients — always offer 2-3 options, discover before building, explain trade-offs, validate before saving
- **Cursor rule** (`.cursor/rules/unifai-guide.mdc`): Persistent Cursor-specific guidance for working with UnifAI
- **Resource management tools**: `list_resources`, `get_resource_details`, `create_resource`, `update_resource`, `delete_resource`
- **`get_resource_details`** resolves `$ref` IDs to human-readable resource names (LLM, provider, etc.)
- **Workflow management tools**: `get_workflow_schema`, `create_workflow`, `update_workflow`, `validate_workflow`, `delete_workflow`, `get_workflow_details`
- **Auto-enrichment of `$ref` entries**: `create_workflow`, `update_workflow`, and `validate_workflow` now auto-populate missing `name` and `type` fields for referenced resources
- **Catalog tools**: `list_catalog`, `get_element_schema` for discovering available resource types and their config schemas
- **Session tools**: `list_sessions`, `list_recent_5_sessions` for browsing workflow history
- Full UnifAI REST API client coverage: catalog, resources, blueprints, sessions

### Changed
- **`authenticate` → `get_user_context` → `get_startup_context`**: same behavior (display name from token claims, teams, sessions, workflow routing hints); server instructions and docs updated to call the new name. Display name still comes from the SSO token claims via this tool — not from Cursor's `mcp_auth`.
- **Renamed all "blueprint" tools to "workflow"** for consistency (e.g. `create_blueprint` → `create_workflow`, `get_blueprint_details` → `get_workflow_details`)
- All user-facing output now uses "workflow" terminology instead of "blueprint"
- Server instructions completely rewritten with UX guidelines, key concepts, and workflow pattern reference
- Documented **client-agnostic** design: all capabilities ship via MCP tools/instructions/guides; host-specific config is never required
- Server instructions direct clients to pass `team=` when the user asks for a team workflow
- Blueprint listing prefers the summary API endpoint, with fallback to the resolved endpoint

### Improved
- **`validate_workflow` output**: Failed and passed resources are now grouped separately with clear summary counts (e.g. "INVALID — 4 failed, 9 passed"). Each failure shows the reason, element type, and dependency chain. Includes an informational note about known backend limitations with OAuth-based providers
- **Validation timeout**: Increased from 10s (backend default) to 30s to give MCP provider connectivity probes sufficient time
- **Error transparency**: API errors now surface the actual backend error body instead of a generic HTTP status message

## [0.2.0] - 2026-06-29

### Added
- Workflow execution timeout protection (5 minute default)
- Workflow caching with configurable TTL (5 minute default)
- Progress reporting with elapsed time for long-running workflows
- `clear_cache()` method in UnifAIClient
- `VERIFY_SSL` environment variable for SSL configuration
- Warning log when SSL verification is disabled

### Changed
- SSL verification now enabled by default (breaking change for self-signed cert environments)
- Enhanced error handling in `run_workflow()` with better retry logic
- Progress updates now show elapsed time every 30 seconds
- Workflow lookups now use cache by default (can be disabled with `use_cache=False`)

### Fixed
- Workflows no longer hang indefinitely if execution gets stuck
- Better error messages when workflows timeout or fail
- Stream status check failures no longer crash the entire workflow execution

### Security
- SSL verification enabled by default to prevent MITM attacks
- Added explicit warning when SSL verification is disabled

## [0.1.0] - 2026-05-08

### Added
- Initial release
- OAuth 2.1 with Red Hat SSO authentication
- Streamable HTTP transport
- Dynamic Client Registration
- Automatic workflow discovery
- Concurrent data loading for sessions and workflows
- Tools: authenticate, list_workflows, run_workflow, get_session_chat
