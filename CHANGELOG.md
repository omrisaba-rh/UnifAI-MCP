# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Workflow execution timeout protection (5 minute default)
- Blueprint caching with configurable TTL (5 minute default)
- Progress reporting with elapsed time for long-running workflows
- `clear_cache()` method in UnifAIClient
- `VERIFY_SSL` environment variable for SSL configuration
- Warning log when SSL verification is disabled

### Changed
- SSL verification now enabled by default (breaking change for self-signed cert environments)
- Enhanced error handling in `run_workflow()` with better retry logic
- Progress updates now show elapsed time every 30 seconds
- Blueprint lookups now use cache by default (can be disabled with `use_cache=False`)

### Fixed
- Workflows no longer hang indefinitely if execution gets stuck
- Better error messages when workflows timeout or fail
- Stream status check failures no longer crash the entire workflow execution

### Security
- SSL verification enabled by default to prevent MITM attacks
- Added explicit warning when SSL verification is disabled

## [0.1.0] - 2024-XX-XX

### Added
- Initial release
- OAuth 2.1 with Red Hat SSO authentication
- Streamable HTTP transport
- Dynamic Client Registration
- Automatic workflow discovery
- Concurrent data loading for sessions and workflows
- Tools: authenticate, list_workflows, run_workflow, get_session_chat
