"""UnifAI MCP Server — multi-agent workflow orchestration over MCP.

Exposes UnifAI workflows as MCP tools, authenticated via the UnifAI
Identity Service (which proxies Keycloak SSO).  The server acts as its
own OAuth Authorization Server (handling DCR locally) and delegates
login to the Identity Service.

Transport: Streamable HTTP (``/mcp``)
Auth:      Local AS with Identity Service login proxy + in-memory DCR
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import Context, FastMCP

from unifai_mcp.auth.provider import IdentityServiceProvider
from unifai_mcp.auth.settings import create_auth_settings
from unifai_mcp.config import Settings
from unifai_mcp.unifai_client import UnifAIClient

logger = logging.getLogger(__name__)

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


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[dict]:
    client = UnifAIClient(settings.unifai_api_url, verify_ssl=False)
    try:
        yield {"unifai": client}
    finally:
        await client.close()


# ── MCP Server ──────────────────────────────────────────────────

mcp = FastMCP(
    "UnifAI",
    instructions=(
        "MCP server for UnifAI — a multi-agent workflow orchestration platform. "
        "Authenticate with your Red Hat SSO credentials, then run AI workflows "
        "by blueprint name or ID.\n\n"
        "IMPORTANT: Always call the 'authenticate' tool FIRST at the start of "
        "every conversation, even before the user asks for anything. The tool "
        "returns your recent UnifAI sessions — present them to the user and ask "
        "if they'd like to continue working on any of them, or start something new."
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
        return RedirectResponse(url=redirect_url, status_code=302)
    except ValueError as exc:
        logger.exception("Identity callback failed")
        return Response(
            content=f"Callback error: {exc}",
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
    session_cookie = claims.get("session_cookie", "")
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
    identity **and** their most recent UnifAI workflow sessions so you can
    offer to continue where they left off.
    """
    access = get_access_token()
    if access is None or not access.subject:
        return (
            "Not authenticated.\n"
            "Your MCP client should automatically start the OAuth login flow. "
            "If prompted, log in with your Red Hat SSO credentials."
        )

    claims = access.claims or {}
    session_cookie = claims.get("session_cookie", "")
    username = claims.get("preferred_username", access.subject)

    parts = [
        "Authenticated successfully.",
        f"  Username : {username}",
        f"  Name     : {claims.get('name', 'n/a')}",
        f"  Email    : {claims.get('email', 'n/a')}",
        f"  Subject  : {access.subject}",
    ]

    if session_cookie:
        try:
            unifai = _get_unifai(ctx)
            unifai.set_session_cookie(session_cookie)
            sessions = await unifai.list_user_sessions()
            if sessions:
                sessions.sort(
                    key=lambda s: s.get("started_at") or "",
                    reverse=True,
                )
                recent = sessions[:5]
                parts.append("")
                parts.append(f"Recent sessions ({len(recent)} of {len(sessions)}):")
                for s in recent:
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
                        line += f"  [blueprint: {bp_id}]"
                    parts.append(line)
                parts.append("")
                parts.append(
                    "Ask the user if they want to continue working on any of "
                    "these sessions, or start something new."
                )
            else:
                parts.append("\nNo recent sessions found.")
        except Exception as exc:
            logger.debug("Could not fetch recent sessions: %s", exc)
            parts.append("\n(Could not fetch recent sessions.)")

    return "\n".join(parts)


@mcp.tool()
async def list_workflows(ctx: Context) -> str:
    """List all available UnifAI workflows (blueprints) for the current user."""
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    try:
        blueprints = await unifai.list_blueprints(username)
    except Exception as exc:
        logger.exception("Failed to list blueprints")
        return f"Failed to list workflows: {exc}"

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
        workflow: Blueprint name (e.g. "Multi-Source Knowledge Search")
                  or blueprint ID.
        prompt:   The user question or instruction to send to the workflow.
    """
    session_cookie, username = _require_auth()
    unifai = _get_unifai(ctx)
    unifai.set_session_cookie(session_cookie)

    blueprint_id = await _resolve_blueprint(unifai, workflow, username)

    try:
        await ctx.info(f"Creating session for blueprint {blueprint_id}...")
        session_id = await unifai.create_session(blueprint_id)

        await ctx.info(f"Submitting workflow (session {session_id})...")
        await unifai.submit_session(
            session_id,
            {"user_prompt": prompt},
        )

        await ctx.info("Waiting for workflow to complete...")
        import asyncio
        seen_active = False
        for _ in range(60):
            await asyncio.sleep(3)
            stream = await unifai.get_stream_status(session_id)
            is_active = stream.get("is_active", False)
            if is_active:
                seen_active = True
                await ctx.info("Workflow running...")
            elif seen_active or stream.get("not_found"):
                break

        chat = await unifai.get_session_chat(session_id)
        output = chat.get("output", "")
        messages = chat.get("messages", [])

        if output:
            return (
                f"Workflow completed.\n"
                f"Session : {session_id}\n"
                f"Blueprint: {blueprint_id}\n\n"
                f"Result:\n{output}"
            )

        formatted = json.dumps(chat, indent=2, ensure_ascii=False, default=str)
        return (
            f"Workflow completed.\n"
            f"Session : {session_id}\n"
            f"Blueprint: {blueprint_id}\n\n"
            f"Result:\n{formatted}"
        )
    except PermissionError:
        raise
    except Exception as exc:
        logger.exception("Workflow execution failed for blueprint=%s", blueprint_id)
        return f"Workflow execution failed: {exc}"


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
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
