"""UnifAI MCP Server — multi-agent workflow orchestration over MCP.

Exposes UnifAI workflows as MCP tools, authenticated via the UnifAI
Identity Service (which proxies Keycloak SSO).  The server acts as its
own OAuth Authorization Server (handling DCR locally) and delegates
login to the Identity Service.

Transport: Streamable HTTP (``/mcp``)
Auth:      Local AS with Identity Service login proxy + in-memory DCR
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import Context, FastMCP

from unifai_mcp.auth.provider import SWEEP_INTERVAL, IdentityServiceProvider
from unifai_mcp.auth.settings import create_auth_settings
from unifai_mcp.config import Settings
from unifai_mcp.unifai_client import UnifAIClient

logger = logging.getLogger(__name__)

_SENSITIVE_PARAM = re.compile(r"(user=)[^\s&]+")

# Curated routing hints for available workflows.
# The LLM uses these to silently pick the best workflow for a user request.
# Falls back to spec_dict["description"] for unlisted workflows.
WORKFLOW_HINTS: dict[str, str] = {
    "AskRH": "Red Hat product knowledge — use for RHEL, OpenShift, Ansible, security, lifecycle questions",
    "Web Search": "Internet search — use for current events, public docs, non-Red Hat topics",
    "Deep Agent Jira": "Jira operations — use when user wants to create, search, or update issues",
    "Google flow": "Google services — use for email, calendar, or Drive requests",
    "Web Fetch flow": "Fetch and analyze content from a specific URL",
    "Full WF- OCP, RHEL and Tools": "Comprehensive multi-source Red Hat support — use for complex questions spanning multiple products",
    "OCP 4.18 & OCP 4.20": "OpenShift 4.18/4.20 version-specific troubleshooting and guidance",
}


class _RedactFilter(logging.Filter):
    """Strip the base64 'user=' query param from uvicorn access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "msg") and isinstance(record.msg, str):
            record.msg = _SENSITIVE_PARAM.sub(r"\1<redacted>", record.msg)
        if hasattr(record, "args") and record.args:
            record.args = tuple(
                _SENSITIVE_PARAM.sub(r"\1<redacted>", str(a))
                if isinstance(a, str) else a
                for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactFilter())

settings = Settings()

# ── Auth wiring ─────────────────────────────────────────────────

auth_provider = IdentityServiceProvider(
    sso_url=settings.sso_url,
    server_url=settings.mcp_server_url,
)

auth_settings = create_auth_settings(
    issuer_url=settings.mcp_server_url,
    resource_server_url=settings.mcp_resource_url,
)

# ── Lifespan (shared UnifAI HTTP client) ────────────────────────


async def _auth_sweep_loop() -> None:
    """Periodically purge expired auth state from memory."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            auth_provider.sweep_expired()
        except Exception:
            logger.exception("Auth sweep failed")


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[dict]:
    client = UnifAIClient(settings.unifai_api_url, verify_ssl=settings.verify_ssl)
    if not settings.verify_ssl:
        logger.warning(
            "SSL verification is DISABLED. This should only be used in "
            "development/testing environments. Enable VERIFY_SSL=true in production."
        )
    sweep_task = asyncio.create_task(_auth_sweep_loop())
    try:
        yield {"unifai": client}
    finally:
        sweep_task.cancel()
        await client.close()


# ── MCP Server ──────────────────────────────────────────────────

mcp = FastMCP(
    "UnifAI",
    instructions=(
        "MCP server for UnifAI — a multi-agent workflow orchestration platform. "
        "Authenticate with your Red Hat SSO credentials, then run AI workflows "
        "by workflow name or ID.\n\n"
        "IMPORTANT: Always call the 'authenticate' tool FIRST at the start of "
        "every conversation, even before the user asks for anything. The tool "
        "returns your recent UnifAI sessions and available workflows.\n\n"
        "Present only the recent sessions to the user and ask if they'd like to "
        "continue or start something new. Do NOT list the available workflows to "
        "the user — use them silently when deciding which workflow to invoke via "
        "run_workflow. Pick the most specific workflow matching the user's intent."
    ),
    auth_server_provider=auth_provider,
    auth=auth_settings,
    lifespan=lifespan,
    host=settings.mcp_host,
    port=settings.mcp_port,
)


# ── Identity Service callback route ─────────────────────────────


@mcp.custom_route("/api/auth/callback/{state_key}", methods=["GET"])
async def identity_callback(request: Request) -> Response:
    """Receive the redirect from the UnifAI Identity Service after SSO login.

    The Identity Service redirects here with:
        /api/auth/callback/<state_key>?auth=success&user=<base64_user_info>
    """
    state_key = request.path_params.get("state_key", "")
    auth_status = request.query_params.get("auth", "")
    user_b64 = request.query_params.get("user", "")

    if auth_status != "success":
        logger.error("Identity Service returned auth=%s", auth_status)
        return Response(
            content="Authentication failed. Please try again.",
            status_code=400,
            media_type="text/plain",
        )

    if not state_key or not user_b64:
        return Response(
            content="Missing state or user parameter",
            status_code=400,
            media_type="text/plain",
        )

    try:
        redirect_url = await auth_provider.handle_identity_callback(user_b64, state_key)
        logger.info("Identity callback completed for state=%s…", state_key[:8])
        return RedirectResponse(url=redirect_url, status_code=302)
    except ValueError as exc:
        logger.exception("Identity callback failed")
        return Response(
            content="Authentication callback failed. Please try again.",
            status_code=400,
            media_type="text/plain",
        )
    except Exception:
        logger.exception("Unexpected error in Identity callback")
        return Response(
            content="Internal server error during authentication",
            status_code=500,
            media_type="text/plain",
        )


# ── Helpers ─────────────────────────────────────────────────────


def _get_unifai(ctx: Context) -> UnifAIClient:
    return ctx.request_context.lifespan_context["unifai"]


def _require_auth() -> tuple[str, str]:
    """Return (session_cookie, username) or raise."""
    access = get_access_token()
    if access is None or not access.subject:
        raise PermissionError("Not authenticated — complete the OAuth login flow first.")
    claims = access.claims or {}
    username = claims.get("preferred_username", access.subject)
    session_cookie = auth_provider.get_session_cookie(access.token) or ""
    if not session_cookie:
        raise PermissionError(
            "Session cookie missing. Please re-authenticate."
        )
    return session_cookie, username


# ── Tools ───────────────────────────────────────────────────────


@mcp.tool()
async def authenticate(ctx: Context) -> str:
    """Check authentication status, return user profile and recent sessions.

    Call this tool at the start of every conversation. It returns the user's
    identity, their most recent UnifAI workflow sessions so you can offer to
    continue where they left off, and the available workflows for internal
    routing context.
    """
    access = get_access_token()
    if access is None or not access.subject:
        return (
            "Not authenticated.\n"
            "Your MCP client should automatically start the OAuth login flow. "
            "If prompted, log in with your Red Hat SSO credentials."
        )

    claims = access.claims or {}
    session_cookie = auth_provider.get_session_cookie(access.token) or ""
    username = claims.get("preferred_username", access.subject)

    parts = [
        "Authenticated successfully.",
        f"  Username : {username}",
        f"  Name     : {claims.get('name', 'n/a')}",
        f"  Email    : {claims.get('email', 'n/a')}",
        f"  Subject  : {access.subject}",
    ]

    if session_cookie:
        unifai = _get_unifai(ctx)
        unifai.set_session_cookie(session_cookie)

        # Fetch sessions and blueprints concurrently
        sessions_result, blueprints_result = await asyncio.gather(
            unifai.list_user_sessions(user_key=username),
            unifai.list_blueprints(username),
            return_exceptions=True,
        )

        # ── Recent sessions (displayed to user) ──
        if isinstance(sessions_result, Exception):
            logger.debug("Could not fetch recent sessions: %s", sessions_result)
            parts.append("\n(Could not fetch recent sessions.)")
        else:
            sessions = sessions_result
            if sessions:
                sessions.sort(
                    key=lambda s: s.get("started_at") or "",
                    reverse=True,
                )
                recent = sessions[:3]
                parts.append("")
                parts.append(f"Recent sessions ({len(recent)} of {len(sessions)}):")

                async def _short_summary(sid: str) -> str | None:
                    try:
                        chat = await unifai.get_session_chat(sid)
                        text = (chat.get("output") or "").strip()
                        if not text:
                            msgs = chat.get("messages", [])
                            if msgs:
                                text = msgs[-1].get("content", "").strip()
                        if text:
                            sentences = text.split(". ", 2)[:2]
                            return ". ".join(sentences).rstrip(".") + "."
                    except Exception:
                        return None
                    return None

                sids = [
                    s.get("session_id") or s.get("sessionId") or "?"
                    for s in recent
                ]
                summaries = await asyncio.gather(
                    *[_short_summary(sid) for sid in sids]
                )

                for s, summary in zip(recent, summaries):
                    metadata = s.get("metadata", {}) or {}
                    sid = s.get("session_id") or s.get("sessionId") or "?"
                    title = metadata.get("title") or s.get("title") or "Untitled"
                    started = s.get("started_at", "")
                    if started:
                        started = started[:10]
                    bp_id = s.get("blueprint_id", "")
                    line = f"  - {title}"
                    if started:
                        line += f"  ({started})"
                    line += f"  [id: {sid}]"
                    if bp_id:
                        line += f"  [workflow: {bp_id}]"
                    parts.append(line)
                    if summary:
                        parts.append(f"    Summary: {summary}")
                parts.append("")
                parts.append(
                    "Ask the user if they want to continue working on any of "
                    "these sessions, or start something new."
                )
            else:
                parts.append("\nNo recent sessions found.")

        # ── Available workflows (internal context — do NOT display to user) ──
        if isinstance(blueprints_result, Exception):
            logger.debug("Could not fetch workflows: %s", blueprints_result)
        else:
            blueprints = blueprints_result
            if blueprints:
                parts.append("")
                parts.append(
                    "[INTERNAL — do not display the following to the user. "
                    "Use silently to route user requests to the appropriate workflow "
                    "via the run_workflow tool.]"
                )
                parts.append(f"Available workflows ({len(blueprints)}):")
                for bp in blueprints:
                    spec = bp.get("spec_dict", {})
                    bp_name = spec.get("name", "Unnamed")
                    hint = WORKFLOW_HINTS.get(bp_name)
                    if not hint:
                        desc = spec.get("description", "")
                        hint = desc.split(".")[0][:80] if desc else ""
                    line = f"  • {bp_name}"
                    if hint:
                        line += f" — {hint}"
                    parts.append(line)

    return "\n".join(parts)


@mcp.tool()
async def list_workflows(ctx: Context) -> str:
    """List all available UnifAI workflows for the current user."""
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        blueprints = await unifai.list_blueprints(username)
    except Exception as exc:
        logger.exception("Failed to list blueprints")
        return "Failed to list workflows. Please try again later."

    if not blueprints:
        return "No workflows available."

    lines = [f"Available workflows ({len(blueprints)}):\n"]
    for bp in blueprints:
        spec = bp.get("spec_dict", {})
        bp_id = bp.get("blueprint_id", "?")
        bp_name = spec.get("name", "Unnamed")
        desc = spec.get("description", "")
        line = f"  - {bp_name}  (id: {bp_id})"
        if desc:
            line += f"\n    {desc}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
async def run_workflow(
    workflow: str,
    prompt: str,
    ctx: Context,
) -> str:
    """Run a UnifAI multi-agent workflow.

    Args:
        workflow: Workflow name (e.g. "Multi-Source Knowledge Search")
                  or workflow ID.
        prompt:   The user question or instruction to send to the workflow.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    blueprint_id = await _resolve_blueprint(unifai, workflow, username)

    try:
        await ctx.info(f"Creating session for workflow {blueprint_id}...")
        session_id = await unifai.create_session(blueprint_id)

        await ctx.info(f"Submitting workflow (session {session_id})...")
        await unifai.submit_session(
            session_id,
            {"user_prompt": prompt},
        )

        # Configurable timeout and polling
        MAX_POLL_DURATION = 300  # 5 minutes
        POLL_INTERVAL = 3  # seconds
        MAX_RETRIES = MAX_POLL_DURATION // POLL_INTERVAL

        await ctx.info("Waiting for workflow to complete...")
        import asyncio
        import time

        start_time = time.time()
        seen_active = False

        for i in range(MAX_RETRIES):
            elapsed = int(time.time() - start_time)

            # Check for timeout
            if elapsed > MAX_POLL_DURATION:
                logger.warning(
                    "Workflow timeout after %ds (session=%s, blueprint=%s)",
                    MAX_POLL_DURATION, session_id, blueprint_id
                )
                return (
                    f"Workflow execution timeout after {MAX_POLL_DURATION}s.\n"
                    f"Session : {session_id}\n"
                    f"Workflow: {blueprint_id}\n\n"
                    f"The workflow may still be running. Check the session status manually "
                    f"using get_session_chat with session_id: {session_id}"
                )

            try:
                stream = await unifai.get_stream_status(session_id)
            except Exception as stream_exc:
                logger.warning(
                    "Failed to get stream status (attempt %d/%d): %s",
                    i + 1, MAX_RETRIES, stream_exc
                )
                await asyncio.sleep(POLL_INTERVAL)
                continue

            is_active = stream.get("is_active", False)

            if is_active:
                seen_active = True
                # Enhanced progress reporting
                if i % 10 == 0:  # Report every 30 seconds
                    await ctx.info(f"Workflow running... ({elapsed}s elapsed)")
            elif seen_active or stream.get("not_found"):
                # Workflow completed or stopped
                break

            await asyncio.sleep(POLL_INTERVAL)

        # Retrieve final results
        try:
            chat = await unifai.get_session_chat(session_id)
        except Exception as chat_exc:
            logger.exception("Failed to retrieve session chat for %s", session_id)
            return (
                f"Workflow may have completed, but failed to retrieve results: {chat_exc}\n"
                f"Session : {session_id}\n"
                f"Workflow: {blueprint_id}"
            )

        output = chat.get("output", "")
        messages = chat.get("messages", [])

        if output:
            return (
                f"Workflow completed.\n"
                f"Session : {session_id}\n"
                f"Workflow: {blueprint_id}\n\n"
                f"Result:\n{output}"
            )

        formatted = json.dumps(chat, indent=2, ensure_ascii=False, default=str)
        return (
            f"Workflow completed.\n"
            f"Session : {session_id}\n"
            f"Workflow: {blueprint_id}\n\n"
            f"Result:\n{formatted}"
        )
    except PermissionError:
        raise
    except Exception as exc:
        logger.exception("Workflow execution failed for blueprint=%s", blueprint_id)
        return (
            f"Workflow execution failed. Please try again.\n"
            f"Workflow: {blueprint_id}"
        )


@mcp.tool()
async def list_sessions(ctx: Context, limit: int = 20) -> str:
    """List the current user's recent UnifAI workflow sessions.

    Returns session IDs, titles, timestamps, and workflow info so the user
    can browse history or resume a previous session via get_session_chat.

    Args:
        limit: Maximum number of sessions to return (default 20, most recent first).
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        sessions = await unifai.list_user_sessions(user_key=username)
    except Exception as exc:
        logger.exception("Failed to list sessions")
        return "Failed to list sessions. Please try again later."

    if not sessions:
        return "No sessions found."

    sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    sessions = sessions[:limit]

    lines = [f"Sessions ({len(sessions)} shown):\n"]
    for s in sessions:
        metadata = s.get("metadata", {}) or {}
        sid = s.get("session_id") or s.get("sessionId") or "?"
        title = metadata.get("title") or s.get("title") or "Untitled"
        started = s.get("started_at", "")
        if started:
            started = started[:19]
        bp_id = s.get("blueprint_id", "")
        status = s.get("status", "")

        line = f"  - {title}"
        if started:
            line += f"  ({started})"
        if status:
            line += f"  [{status}]"
        line += f"\n    id: {sid}"
        if bp_id:
            line += f"  |  workflow: {bp_id}"
        lines.append(line)

    return "\n".join(lines)


@mcp.tool()
async def list_recent_5_sessions(ctx: Context) -> str:
    """Fetch the 5 most recent UnifAI workflow sessions.

    A quick-access tool that returns the last 5 sessions with their IDs,
    titles, and timestamps. Use get_session_chat to retrieve full details.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        sessions = await unifai.list_user_sessions(user_key=username)
    except Exception:
        logger.exception("Failed to list recent sessions")
        return "Failed to list sessions. Please try again later."

    if not sessions:
        return "No sessions found."

    sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    recent = sessions[:5]

    lines = [f"Last {len(recent)} sessions:\n"]
    for s in recent:
        metadata = s.get("metadata", {}) or {}
        sid = s.get("session_id") or s.get("sessionId") or "?"
        title = metadata.get("title") or s.get("title") or "Untitled"
        started = s.get("started_at", "")
        if started:
            started = started[:19]
        bp_id = s.get("blueprint_id", "")
        status = s.get("status", "")

        line = f"  - {title}"
        if started:
            line += f"  ({started})"
        if status:
            line += f"  [{status}]"
        line += f"\n    id: {sid}"
        if bp_id:
            line += f"  |  workflow: {bp_id}"
        lines.append(line)

    return "\n".join(lines)


@mcp.tool()
async def get_session_chat(
    session_id: str,
    ctx: Context,
) -> str:
    """Retrieve the chat history and output of a previous UnifAI workflow session.

    Args:
        session_id: The session ID to retrieve (from the authenticate tool's
                    recent sessions list, or from a previous run_workflow result).
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    if not await unifai.user_owns_session(session_id, username):
        logger.warning(
            "IDOR blocked: user=%s tried to access session=%s",
            username, session_id,
        )
        return f"Session {session_id} not found or access denied."

    try:
        chat = await unifai.get_session_chat(session_id)
    except Exception as exc:
        logger.exception("Failed to fetch session chat for %s", session_id)
        return f"Failed to fetch session {session_id}. Please try again later."

    output = chat.get("output", "")
    if output:
        return (
            f"Session: {session_id}\n\n"
            f"Result:\n{output}"
        )

    messages = chat.get("messages", [])
    if messages:
        lines = [f"Session: {session_id}\n"]
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    return f"Session {session_id} has no output or messages yet."


# ── Catalog & Resource Management Tools ──────────────────────


@mcp.tool()
async def list_catalog(ctx: Context) -> str:
    """List all available element types that can be created as resources.

    Returns every category (tools, llms, providers, retrievers, nodes,
    conditions) and the element types within each. Use this to discover
    what kinds of resources can be created via create_resource.
    """
    _require_auth()
    unifai = _get_unifai(ctx)

    try:
        elements = await unifai.list_catalog_elements()
    except Exception:
        logger.exception("Failed to list catalog elements")
        return "Failed to list catalog. Please try again later."

    if not elements:
        return "No catalog elements found."

    lines = ["Available element types:\n"]
    for category, items in sorted(elements.items()):
        lines.append(f"  {category}:")
        for item in items:
            name = item.get("name", item.get("type", "?"))
            type_key = item.get("type", "?")
            line = f"    - {name}  (type: {type_key})"
            hints = item.get("hints", [])
            if hints:
                line += f"  [{', '.join(str(h) for h in hints)}]"
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def get_element_schema(
    category: str,
    element_type: str,
    ctx: Context,
) -> str:
    """Get the configuration schema for a specific element type.

    Use this before create_resource to understand the required config
    fields for the element you want to create.

    Args:
        category:     Resource category (e.g. "tools", "llms", "providers",
                      "retrievers", "nodes", "conditions").
        element_type: The type key (e.g. "mcp_proxy", "openai", "ssh_exec").
    """
    _require_auth()
    unifai = _get_unifai(ctx)

    try:
        spec = await unifai.get_element_spec(category, element_type)
    except Exception:
        logger.exception("Failed to get element spec for %s/%s", category, element_type)
        return f"Failed to get schema for {category}/{element_type}."

    lines = [
        f"Element: {spec.get('name', element_type)}",
        f"Category: {spec.get('category', category)}",
        f"Type: {spec.get('type', element_type)}",
    ]
    desc = spec.get("description", "")
    if desc:
        lines.append(f"Description: {desc}")

    tags = spec.get("tags", [])
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")

    config_schema = spec.get("config_schema")
    if config_schema:
        lines.append(f"\nConfig schema:\n{json.dumps(config_schema, indent=2)}")

    output_schema = spec.get("output_schema")
    if output_schema:
        lines.append(f"\nOutput schema:\n{json.dumps(output_schema, indent=2)}")

    return "\n".join(lines)


@mcp.tool()
async def list_resources(
    ctx: Context,
    category: str = "",
    element_type: str = "",
) -> str:
    """List the user's saved resources (tools, LLMs, providers, etc.).

    Args:
        category:     Optional filter by category (e.g. "tools", "llms").
        element_type: Optional filter by type within category.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        result = await unifai.list_resources(
            user_id=username,
            category=category or None,
            element_type=element_type or None,
        )
    except Exception:
        logger.exception("Failed to list resources")
        return "Failed to list resources. Please try again later."

    resources = result.get("resources", []) if isinstance(result, dict) else []
    pagination = result.get("pagination", {})

    if not resources:
        return "No resources found."

    lines = [f"Resources ({pagination.get('total', len(resources))} total):\n"]
    for r in resources:
        rid = r.get("rid", "?")
        name = r.get("name", "Unnamed")
        cat = r.get("category", "?")
        rtype = r.get("type", "?")
        lines.append(f"  - {name}")
        lines.append(f"    category: {cat}  |  type: {rtype}  |  id: {rid}")
    return "\n".join(lines)


@mcp.tool()
async def create_resource(
    category: str,
    element_type: str,
    name: str,
    config: str,
    ctx: Context,
) -> str:
    """Create a new resource in the user's inventory.

    Use list_catalog and get_element_schema first to discover available
    types and their required configuration.

    Args:
        category:     Resource category ("tools", "llms", "providers",
                      "retrievers", "nodes", "conditions").
        element_type: The type key (e.g. "mcp_proxy", "openai").
        name:         Human-readable name for this resource.
        config:       JSON string with the resource configuration matching
                      the element's config schema.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        config_dict = json.loads(config)
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in config: {exc}"

    try:
        doc = await unifai.create_resource(
            category=category,
            element_type=element_type,
            name=name,
            config=config_dict,
            user_id=username,
        )
    except Exception as exc:
        logger.exception("Failed to create resource %s/%s", category, element_type)
        return f"Failed to create resource: {exc}"

    rid = doc.get("rid", "?")
    return (
        f"Resource created successfully.\n"
        f"  Name    : {name}\n"
        f"  Category: {category}\n"
        f"  Type    : {element_type}\n"
        f"  ID      : {rid}"
    )


@mcp.tool()
async def update_resource(
    resource_id: str,
    config: str,
    ctx: Context,
    name: str = "",
) -> str:
    """Update an existing resource's configuration and/or name.

    Args:
        resource_id: The resource ID to update.
        config:      JSON string with the new configuration.
        name:        Optional new name for the resource.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        config_dict = json.loads(config)
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in config: {exc}"

    try:
        doc = await unifai.update_resource(
            resource_id=resource_id,
            config=config_dict,
            name=name or None,
        )
    except Exception as exc:
        logger.exception("Failed to update resource %s", resource_id)
        return f"Failed to update resource: {exc}"

    return (
        f"Resource updated successfully.\n"
        f"  ID  : {doc.get('rid', resource_id)}\n"
        f"  Name: {doc.get('name', '?')}"
    )


@mcp.tool()
async def delete_resource(
    resource_id: str,
    ctx: Context,
) -> str:
    """Delete a resource from the user's inventory.

    Args:
        resource_id: The resource ID to delete.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        result = await unifai.delete_resource(resource_id)
    except Exception as exc:
        logger.exception("Failed to delete resource %s", resource_id)
        return f"Failed to delete resource: {exc}"

    return f"Resource {resource_id} deleted successfully."


# ── Resource Details ──────────────────────────────────────────


@mcp.tool()
async def get_resource_details(
    resource_id: str,
    ctx: Context,
) -> str:
    """Get the full details and configuration of a specific resource.

    Returns the resource's name, category, type, and full configuration
    so you can inspect how an agent, tool, LLM, provider, or retriever
    is set up.

    Args:
        resource_id: The resource ID to retrieve (from list_resources).
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        doc = await unifai.get_resource(resource_id)
    except Exception as exc:
        logger.exception("Failed to get resource %s", resource_id)
        return f"Failed to get resource: {exc}"

    rid = doc.get("rid", resource_id)
    name = doc.get("name", "Unnamed")
    category = doc.get("category", "?")
    rtype = doc.get("type", "?")
    config = doc.get("cfg_dict") or doc.get("config") or {}

    lines = [
        f"Resource: {rid}",
        f"  Name    : {name}",
        f"  Category: {category}",
        f"  Type    : {rtype}",
    ]

    identity = doc.get("identity")
    if isinstance(identity, dict):
        owner = identity.get("id") or identity.get("name", "")
        if owner:
            lines.append(f"  Owner   : {owner}")
    elif doc.get("contributed_by"):
        lines.append(f"  Owner   : {doc['contributed_by']}")

    created = doc.get("created") or doc.get("created_at")
    if created:
        lines.append(f"  Created : {created}")

    updated = doc.get("updated") or doc.get("updated_at")
    if updated:
        lines.append(f"  Updated : {updated}")

    version = doc.get("version")
    if version:
        lines.append(f"  Version : {version}")

    # Resolve referenced resource names
    nested = doc.get("nested_refs", [])
    ref_names: dict[str, str] = {}
    if nested:
        async def _fetch_name(ref_id: str) -> tuple[str, str]:
            try:
                ref_doc = await unifai.get_resource(ref_id)
                ref_name = ref_doc.get("name", "?")
                ref_cat = ref_doc.get("category", "")
                ref_type = ref_doc.get("type", "")
                label = ref_name
                if ref_cat or ref_type:
                    label += f"  ({ref_cat}/{ref_type})"
                return ref_id, label
            except Exception:
                return ref_id, "?"

        resolved = await asyncio.gather(*[_fetch_name(r) for r in nested])
        ref_names = dict(resolved)

        lines.append("\n  Referenced resources:")
        for ref_id in nested:
            lines.append(f"    - {ref_names.get(ref_id, '?')}  [id: {ref_id}]")

    if config:
        config_str = json.dumps(config, indent=2, default=str)
        for ref_id, label in ref_names.items():
            config_str = config_str.replace(
                f"$ref:{ref_id}",
                f"$ref:{ref_id} ({label})",
            )
        lines.append(f"\n  Configuration:\n{config_str}")

    return "\n".join(lines)


# ── Workflow Management Tools ────────────────────────────────


@mcp.tool()
async def get_workflow_schema(ctx: Context) -> str:
    """Get the JSON schema for workflow drafts.

    Returns the full schema describing the structure of a workflow,
    including nodes, llms, tools, providers, retrievers, conditions,
    and the execution plan. Use this to understand how to compose
    a workflow for create_workflow.
    """
    _require_auth()
    unifai = _get_unifai(ctx)

    try:
        schema = await unifai.get_blueprint_draft_schema()
    except Exception:
        logger.exception("Failed to get workflow schema")
        return "Failed to get workflow schema."

    return json.dumps(schema, indent=2)


@mcp.tool()
async def create_workflow(
    workflow_json: str,
    ctx: Context,
    title: str = "",
    description: str = "",
) -> str:
    """Create a new workflow.

    The workflow defines a multi-agent graph with nodes, LLMs,
    tools, providers, and an execution plan. Use get_workflow_schema
    to understand the required structure.

    Resources can be defined inline or referenced via $ref to saved
    resource IDs from the user's inventory.

    Args:
        workflow_json: JSON string containing the workflow draft.
                       Must include at least: nodes, plan, and a name.
        title:         Optional title for the workflow metadata.
        description:   Optional description for the workflow metadata.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        draft = json.loads(workflow_json)
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in workflow: {exc}"

    metadata: dict[str, Any] = {}
    if title:
        metadata["title"] = title
    if description:
        metadata["description"] = description

    try:
        result = await unifai.save_blueprint(
            draft_dict=draft,
            user_id=username,
            metadata=metadata or None,
        )
        unifai.clear_cache()
    except Exception as exc:
        logger.exception("Failed to create workflow")
        return f"Failed to create workflow: {exc}"

    wf_id = result.get("blueprint_id", "?")
    return (
        f"Workflow created successfully.\n"
        f"  ID    : {wf_id}\n"
        f"  Name  : {draft.get('name', 'Untitled')}\n"
        f"  Status: {result.get('status', '?')}"
    )


@mcp.tool()
async def update_workflow(
    workflow_id: str,
    workflow_json: str,
    ctx: Context,
) -> str:
    """Update an existing workflow in-place.

    Args:
        workflow_id:   The workflow ID to update.
        workflow_json: JSON string with the full updated workflow draft.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        draft = json.loads(workflow_json)
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in workflow: {exc}"

    try:
        result = await unifai.update_blueprint(
            blueprint_id=workflow_id,
            draft_dict=draft,
            user_id=username,
        )
        unifai.clear_cache()
    except Exception as exc:
        logger.exception("Failed to update workflow %s", workflow_id)
        return f"Failed to update workflow: {exc}"

    return (
        f"Workflow updated successfully.\n"
        f"  ID    : {workflow_id}\n"
        f"  Status: {result.get('status', '?')}"
    )


@mcp.tool()
async def validate_workflow(
    workflow_json: str,
    ctx: Context,
) -> str:
    """Validate a workflow draft without saving it.

    Use this to check a workflow for errors before calling create_workflow.

    Args:
        workflow_json: JSON string with the workflow draft to validate.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        draft = json.loads(workflow_json)
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in workflow: {exc}"

    try:
        result = await unifai.validate_blueprint_draft(draft)
    except Exception as exc:
        logger.exception("Failed to validate workflow")
        return f"Failed to validate workflow: {exc}"

    is_valid = result.get("is_valid", False)
    lines = [f"Validation result: {'VALID' if is_valid else 'INVALID'}"]

    element_results = result.get("element_results", {})
    if element_results:
        for rid, er in element_results.items():
            status = "OK" if er.get("is_valid") else "FAIL"
            line = f"  [{status}] {rid}"
            error = er.get("error", "")
            if error:
                line += f" — {error}"
            lines.append(line)

    return "\n".join(lines)


@mcp.tool()
async def delete_workflow(
    workflow_id: str,
    ctx: Context,
) -> str:
    """Delete a workflow.

    Args:
        workflow_id: The workflow ID to delete.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        result = await unifai.delete_blueprint(workflow_id)
        unifai.clear_cache()
    except Exception as exc:
        logger.exception("Failed to delete workflow %s", workflow_id)
        return f"Failed to delete workflow: {exc}"

    return f"Workflow {workflow_id} deleted successfully."


@mcp.tool()
async def get_workflow_details(
    workflow_id: str,
    ctx: Context,
) -> str:
    """Get the full details of a specific workflow.

    Args:
        workflow_id: The workflow ID to retrieve.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        doc = await unifai.get_blueprint_info(workflow_id)
    except Exception as exc:
        logger.exception("Failed to get workflow %s", workflow_id)
        return f"Failed to get workflow: {exc}"

    spec = doc.get("spec_dict", {})
    lines = [
        f"Workflow: {doc.get('blueprint_id', workflow_id)}",
        f"  Name       : {spec.get('name', 'Untitled')}",
        f"  Description: {spec.get('description', '')}",
        f"  Created    : {doc.get('created_at', '?')}",
        f"  Updated    : {doc.get('updated_at', '?')}",
    ]

    metadata = doc.get("metadata", {})
    if metadata:
        lines.append(f"  Metadata   : {json.dumps(metadata)}")

    for section in ("nodes", "llms", "tools", "providers", "retrievers", "conditions"):
        items = spec.get(section, [])
        if items:
            lines.append(f"\n  {section} ({len(items)}):")
            for item in items:
                rid = item.get("rid", "?")
                itype = item.get("type", "?")
                iname = item.get("name", "")
                line = f"    - {iname or rid}"
                if itype != "?":
                    line += f"  (type: {itype})"
                lines.append(line)

    plan = spec.get("plan", [])
    if plan:
        lines.append(f"\n  Plan ({len(plan)} steps):")
        for step in plan:
            uid = step.get("uid", "?")
            node = step.get("node", "?")
            after = step.get("after")
            line = f"    {uid} → node: {node}"
            if after:
                deps = after if isinstance(after, list) else [after]
                line += f"  (after: {', '.join(deps)})"
            lines.append(line)

    return "\n".join(lines)


async def _resolve_blueprint(
    unifai: UnifAIClient,
    workflow: str,
    user_id: str,
) -> str:
    """Resolve a user-supplied workflow identifier to a blueprint ID.

    Tries name-based lookup first; falls back to treating the input as
    a literal ID and letting the backend validate.
    """
    found = await unifai.find_blueprint_by_name(workflow, user_id)
    if found:
        return found
    return workflow


# ── Entrypoint ──────────────────────────────────────────────────


def main() -> None:
    if settings.ssl_certfile and settings.ssl_keyfile:
        import uvicorn

        _orig_run = mcp.run_streamable_http_async

        async def _run_https() -> None:
            starlette_app = mcp.streamable_http_app()
            config = uvicorn.Config(
                starlette_app,
                host=mcp.settings.host,
                port=mcp.settings.port,
                log_level=mcp.settings.log_level.lower(),
                ssl_certfile=settings.ssl_certfile,
                ssl_keyfile=settings.ssl_keyfile,
            )
            server = uvicorn.Server(config)
            await server.serve()

        mcp.run_streamable_http_async = _run_https
        logger.info(
            "TLS enabled — cert=%s  key=%s",
            settings.ssl_certfile, settings.ssl_keyfile,
        )

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
