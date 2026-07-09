# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- **Renamed all "blueprint" tools to "workflow"** for consistency (e.g. `create_blueprint` → `create_workflow`, `get_blueprint_details` → `get_workflow_details`)
- All user-facing output now uses "workflow" terminology instead of "blueprint"
- Server instructions completely rewritten with UX guidelines, key concepts, and workflow pattern reference

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
