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

_INSTRUCTIONS = """\
MCP server for UnifAI — a multi-agent workflow orchestration platform.
Authenticate with your Red Hat SSO credentials, then run AI workflows
by workflow name or ID.

═══ STARTUP ═══
Always call 'authenticate' FIRST at the start of every conversation.
Present only the recent sessions to the user and ask if they'd like to
continue or start something new. Do NOT list the available workflows —
use them silently when deciding which workflow to invoke via run_workflow.

═══ USER EXPERIENCE GUIDELINES ═══
You are a helpful UnifAI assistant. Follow these rules to provide
the best experience:

1. ALWAYS OFFER CHOICES: When the user wants to create or configure
   something, present 2-3 concrete options with trade-offs. Never
   assume a single answer. Example: "For the LLM, you could use:
   (A) Gemini 3.1 Pro — best reasoning, (B) Gemini Flash — faster
   and cheaper, (C) bring your own model."

2. DISCOVER BEFORE BUILDING: Before creating any resource, call
   list_resources and list_catalog to see what the user already has
   and what's available. Reuse existing resources when possible.

3. GUIDE NEW USERS: If the user has few or no resources, proactively
   explain what they can build. Call get_guide("quick_start") for a
   step-by-step walkthrough.

4. EXPLAIN TRADE-OFFS: When recommending a workflow pattern, LLM,
   or configuration, briefly explain why and what alternatives exist.

5. VALIDATE BEFORE SAVING: Use validate_workflow before create_workflow
   to catch errors early and give the user a chance to fix them.

6. SHOW WHAT YOU BUILT: After creating resources or workflows, use
   get_resource_details or get_workflow_details to show the user
   exactly what was created.

7. DESCRIBE WHAT YOU BUILD: When creating a workflow, always include
   a meaningful description that summarizes the workflow's purpose
   and lists the agents it contains. Users should understand what a
   workflow does at a glance from its description.

═══ KEY CONCEPTS ═══
• RESOURCE: A reusable building block — an LLM, tool, MCP provider,
  agent node, retriever, or router condition.
• WORKFLOW: A multi-agent graph that wires resources together with
  an execution plan. Contains nodes, a plan, and optional conditions.
• $ref: Resources are referenced by ID using "$ref:<resource_id>".
  The system resolves these to the saved resource automatically.

═══ COMMON WORKFLOW PATTERNS ═══
• SINGLE AGENT: User → Agent → Answer. Simple, one tool/provider.
• ORCHESTRATOR: User → Orchestrator → [Agent A, Agent B, ...] → Answer.
  Best for multi-source tasks. The orchestrator plans and delegates.
  Agents can serve any role (data, reporting, analysis, etc.) —
  the orchestrator treats all branches the same.
• SEQUENTIAL PIPELINE: User → Agent A → Agent B → Answer.
  Best when output of one feeds into another (e.g. fetch → report).

═══ AVAILABLE GUIDES ═══
Call get_guide(topic) for detailed help on any of these topics:
  quick_start, workflow_patterns, llm_selection, resource_types,
  build_agent, build_workflow, system_prompts
"""

mcp = FastMCP(
    "UnifAI",
    instructions=_INSTRUCTIONS,
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


# ── Guide / Help ─────────────────────────────────────────────

_GUIDES: dict[str, str] = {
    "quick_start": """\
═══ QUICK START: Build Your First UnifAI Agent ═══

Follow these steps to go from zero to a working agent workflow:

STEP 1 — Choose what your agent will connect to
  Ask yourself: "What data source or service does my agent need?"
  Common choices:
    • Confluence → search/read wiki pages
    • Jira → manage issues and boards
    • Google Workspace → Gmail, Calendar, Drive
    • GitHub/GitLab → repos, PRs, issues
    • A custom API → via MCP server

STEP 2 — Create or reuse an MCP Provider
  Run: list_resources(category="providers") to see existing providers.
  If yours isn't there, create one:
    get_element_schema(category="providers", element_type="mcp_server")
    create_resource(category="providers", element_type="mcp_server", ...)

STEP 3 — Choose an LLM
  Run: list_resources(category="llms") to see available LLMs.
  Decision guide:
    • Need speed + low cost?     → Gemini Flash variants
    • Need strong reasoning?     → Gemini 3.1 Pro
    • Need orchestration/planning? → Gemini Pro (Orchestrator variant)
    • Have your own model?       → OpenAI-compatible endpoint
  If none fit, create one:
    get_element_schema(category="llms", element_type="google_genai")
    create_resource(category="llms", element_type="google_genai", ...)

STEP 4 — Create the Agent Node
  Combine your LLM + Provider + a system prompt:
    create_resource(
      category="nodes", element_type="custom_agent_node",
      name="My Agent",
      config={"llm": "$ref:<llm_id>", "providers": ["$ref:<provider_id>"],
              "system_message": "You are a helpful assistant that...",
              "strategy_type": "react", "max_rounds": 100}
    )

STEP 5 — Wrap it in a Workflow
  Create a workflow that wires the agent between user input and output.
  Built-in nodes (user_question, final_answer) MUST include rid, name,
  type, AND config — otherwise the UI renders them incorrectly.
  Always include a description that summarizes the workflow's purpose
  and the agents it contains.

    create_workflow(workflow_json='{
      "name": "My Workflow",
      "nodes": [
        {"rid": "user_question", "name": "User Question Node",
         "type": "user_question_node",
         "config": {"retries": 1, "type": "user_question_node"}},
        {"rid": "final_answer", "name": "Final Answer Node",
         "type": "final_answer_node",
         "config": {"retries": 1, "type": "final_answer_node"}},
        {"rid": "$ref:<agent_id>", "name": "My Agent",
         "type": null}
      ],
      "plan": [
        {"uid": "user_input", "node": "user_question",
         "meta": {"description": "", "display_name": "", "tags": []}},
        {"uid": "MyAgent-<id>-3", "node": "<agent_id>",
         "after": "user_input",
         "meta": {"description": "", "display_name": "", "tags": []}},
        {"uid": "finalize", "node": "final_answer",
         "after": "MyAgent-<id>-3",
         "meta": {"description": "", "display_name": "", "tags": []}}
      ]
    }')

  For orchestrator workflows, see get_guide("workflow_patterns")
  — they additionally require branches, exit_condition, and a
  conditions array with a router_direct condition.

STEP 6 — Test it!
  run_workflow(workflow="My Workflow", prompt="Hello, can you help me?")
""",

    "workflow_patterns": """\
═══ WORKFLOW PATTERNS ═══

Choose the pattern that best fits your use case:

─── Pattern 1: SINGLE AGENT ───
  Flow: User → Agent → Answer
  Best for: Simple tasks with one data source.
  Example: "Search Confluence for X" or "List my Jira tickets"

  JSON structure:
    "nodes": [
      {"rid": "user_question", "name": "User Question Node",
       "type": "user_question_node",
       "config": {"retries": 1, "type": "user_question_node"}},
      {"rid": "final_answer", "name": "Final Answer Node",
       "type": "final_answer_node",
       "config": {"retries": 1, "type": "final_answer_node"}},
      {"rid": "$ref:<agent_id>", "name": "My Agent",
       "type": null}
    ],
    "plan": [
      {"uid": "user_input", "node": "user_question",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "Agent-<id>-3", "node": "<agent_id>",
       "after": "user_input",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "finalize", "node": "final_answer",
       "after": "Agent-<id>-3",
       "meta": {"description": "", "display_name": "", "tags": []}}
    ]

─── Pattern 2: ORCHESTRATOR (Fan-Out) ───
  Flow: User → Orchestrator → [Agent A, Agent B, ...] → Answer
  Best for: Questions that need multiple sources. The orchestrator
  decides which agents to call and synthesizes their responses.
  Example: "Find info about project X across Jira and Confluence"

  CRITICAL — ORDER MATTERS when building an orchestrator:
    The router (exit_condition) MUST be set on the orchestrator
    BEFORE branches are added. The router is the mechanism that
    enables branching — without it, branches have no effect and
    the orchestrator will complete immediately with empty output.

    Build in this order:
      1. Add the router_direct condition to the "conditions" array
      2. Set "exit_condition" on the orchestrator step (BARE
         resource ID, no $ref: prefix!)
      3. THEN set "branches" on the orchestrator step

  Orchestrator steps require THREE extra fields:
    • "exit_condition": "<router_resource_id>" — BARE resource ID
      (no $ref: prefix!) of a router_direct condition. SET THIS
      FIRST — it enables branching.
    • "branches": {step_uid: step_uid, ..., "finalize": "finalize"}
      — maps STEP UIDs to STEP UIDs. Branch keys AND values are
      step UIDs (not resource IDs, not arbitrary names). MUST
      include "finalize": "finalize" so the orchestrator can route
      to the final answer when done. SET THIS AFTER exit_condition.
    • "after": ["user_input", ...all agent step UIDs] — creates
      return edges FROM agents back TO the orchestrator.

  The "after" + "branches" combination creates BIDIRECTIONAL
  edges. Without both, the orchestrator cannot delegate or the
  graph has validation errors.

  IMPORTANT: The "finalize" step must have NO "after" dependency.
  The orchestrator handles routing to finalize internally via its
  branches. Adding "after" on finalize blocks runtime execution.

  IMPORTANT: Plan "node" values use BARE resource IDs (not $ref:).
  The $ref: prefix is only for the "rid" field in the nodes array.

  The router_direct condition must also be in the workflow's
  "conditions" array with name, type, AND config.

  JSON structure:
    "nodes": [
      {"rid": "user_question", "name": "User Question Node",
       "type": "user_question_node",
       "config": {"retries": 1, "type": "user_question_node"}},
      {"rid": "final_answer", "name": "Final Answer Node",
       "type": "final_answer_node",
       "config": {"retries": 1, "type": "final_answer_node"}},
      {"rid": "$ref:<orch_id>", "name": "My Orchestrator",
       "type": null},
      {"rid": "$ref:<agent_a_id>", "name": "Agent A",
       "type": null},
      {"rid": "$ref:<agent_b_id>", "name": "Agent B",
       "type": null}
    ],
    "conditions": [
      {"rid": "$ref:<router_id>", "name": "Router",
       "type": "router_direct",
       "config": {"type": "router_direct"}}
    ],
    "plan": [
      {"uid": "user_input", "node": "user_question",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "finalize", "node": "final_answer",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "AgentA-<a_id>-4", "node": "<agent_a_id>",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "AgentB-<b_id>-5", "node": "<agent_b_id>",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "Orch-<orch_id>-3", "node": "<orch_id>",
       "after": ["user_input", "AgentA-<a_id>-4",
                  "AgentB-<b_id>-5"],
       "exit_condition": "<router_id>",
       "branches": {
         "AgentA-<a_id>-4": "AgentA-<a_id>-4",
         "AgentB-<b_id>-5": "AgentB-<b_id>-5",
         "finalize": "finalize"},
       "meta": {"description": "", "display_name": "", "tags": []}}
    ]

  Agent steps have NO "after" — they are branch targets of the
  orchestrator, which excludes them from being start nodes.

  NOTE: Agents in an orchestrator can serve any role — data
  fetching, reporting, analysis, summarization, etc. The
  orchestrator treats all branches the same: delegate a task,
  get a result. For example, a "Reporter" agent is just another
  custom_agent_node with a system prompt focused on formatting
  and summarizing. To add it, simply include it as another
  branch in the orchestrator's branches and after lists.

─── Pattern 3: SEQUENTIAL PIPELINE ───
  Flow: User → Agent A → Agent B → Answer
  Best for: When one agent's output feeds into the next.
  Example: "Fetch data from Jira, then generate a report"

  Plan structure:
    user_input → agent_a (after: user_input)
    agent_b (after: agent_a)
    finalize (after: agent_b)

─── CHOOSING THE RIGHT PATTERN ───
  • One data source, simple task → Pattern 1
  • Multiple sources, flexible routing → Pattern 2
    (includes any mix of agents: data, reporter, analysis, etc.)
  • Data transformation pipeline → Pattern 3
""",

    "llm_selection": """\
═══ LLM SELECTION GUIDE ═══

Choose the right LLM based on your needs:

─── SPEED vs QUALITY TRADE-OFF ───

  FAST (low latency, lower cost):
    • Gemini Flash — great for simple tool-calling, search, CRUD
    • Gemini Flash Lite — even faster, good for routing/classification
    Best for: Agents doing straightforward tasks (search, list, create)

  BALANCED:
    • Gemini 3.1 Pro — strong reasoning + tool use
    Best for: Complex agents that need to plan multi-step actions

  SPECIALIZED:
    • Gemini Pro (Orchestrator variant) — tuned for work planning
    Best for: Orchestrator nodes that coordinate multiple agents
    • Qwen / OpenAI-compatible — self-hosted or custom models
    Best for: Specialized tasks, private data, or cost control

─── RECOMMENDATIONS BY USE CASE ───

  Simple tool-calling agent (Jira, Confluence, etc.)
    → Gemini Flash (fast, cost-effective)

  Complex reasoning agent (analysis, troubleshooting)
    → Gemini 3.1 Pro (best quality)

  Orchestrator (plans and delegates)
    → Gemini 3.1 Pro or Orchestrator variant

  Report generator (formatting, synthesis)
    → Gemini 3.1 Pro or Qwen (good at structured output)

─── KEY PARAMETERS ───

  max_tokens: How much output the model can generate.
    • 8192 (default) — fine for most agents
    • 32768+ — needed for long reports or code generation

  temperature: Creativity vs consistency.
    • 0.0-0.3 — deterministic, best for tool-calling agents
    • 0.5-0.7 — balanced (default)
    • 0.8-1.0 — creative, best for writing/brainstorming
""",

    "resource_types": """\
═══ RESOURCE TYPES GUIDE ═══

UnifAI resources are reusable building blocks organized by category:

─── NODES (Agents & Orchestrators) ───
  custom_agent_node
    The standard agent. Has an LLM, MCP providers, tools, retriever,
    and a system prompt. Uses the ReAct strategy by default.
    → Use for: Any agent that calls tools to accomplish tasks.
    Config: llm, providers[], tools[], retriever, system_message,
      strategy_type ("react"), max_rounds (100)

  orchestrator_node
    Coordinates work by creating plans, delegating to adjacent nodes,
    and synthesizing results. Has built-in planning/delegation logic
    — you just give it an LLM and connect it to agent nodes in the
    workflow via branches.
    → Use for: Multi-agent coordination and routing.
    Config: llm, tools[], system_message, max_rounds (100)
    NOTE: No providers — orchestrators delegate, not call tools.

  claude_agent_node
    Autonomous agent powered by the Claude Agent SDK via Vertex AI.
    Runs full Claude sessions with file I/O, bash, web search, etc.
    Has its own model selection and effort/reasoning controls.
    → Use for: Complex autonomous tasks, code generation, deep
      analysis, tasks needing Claude's strengths.
    Config: vertex_project_id, vertex_region ("us-east5"),
      model ("claude-sonnet-4-6"), system_prompt,
      max_turns (200), effort ("low"/"medium"/"high"/"xhigh"),
      permission_mode ("bypassPermissions"/"acceptEdits"/"plan"),
      allowed_tools[], disallowed_tools[], skills_repos{},
      providers[], tools[], retriever
    NOTE: Does NOT use a UnifAI LLM resource — it connects to
      Claude directly via Vertex AI project credentials.

  a2a_agent_node
    Delegates work to a remote agent via the Agent-to-Agent (A2A)
    protocol. Minimal config — just the endpoint URL.
    → Use for: Calling external agent services, cross-platform
      agent communication, microservice-style agent architectures.
    Config: base_url, bearer_token (optional), agent_card (auto-
      fetched), retriever
    NOTE: No LLM, no providers — the remote agent handles
      everything. You just point to its URL.

  deep_agent_node
    Powered by LangChain Deep Agents with built-in planning (todos),
    context management, and automatic subagent delegation.
    → Use for: Complex multi-step reasoning, tasks needing
      structured planning and context tracking.
    Config: llm, providers[], tools[], retriever,
      system_message, cwd, env_vars{}

─── PROVIDERS (External Connections) ───
  mcp_server
    Connects to an external MCP server that provides tools.
    → Use for: Jira, Confluence, Google Workspace, GitHub, Salesforce,
    or any service exposed via MCP.

─── LLMs (Language Models) ───
  google_genai
    Google Gemini models. Configure model name, API key, temperature.
    → Use for: Most agents. Wide range of models available.

  openai
    OpenAI-compatible endpoint. Works with OpenAI, Azure, or any
    compatible API (vLLM, Ollama, etc.).
    → Use for: Custom/self-hosted models or OpenAI models.

─── TOOLS ───
  ssh_exec — Run commands over SSH on a remote host.
  oc_exec — Run commands on an OpenShift cluster.
  web_fetch — Fetch and parse content from a URL.

─── RETRIEVERS ───
  docs_rag — RAG retriever over a document collection.
    → Use for: Searching through uploaded documents.

─── CONDITIONS ───
  router_direct — Routes execution based on LLM classification.
    → Use for: Conditional branching in workflows.
""",

    "build_agent": """\
═══ HOW TO BUILD AN AGENT ═══

STEP 0 — Choose the right agent type
  Ask: "What kind of agent fits this task?"

  custom_agent_node (DEFAULT — use for most tasks)
    Standard agent with LLM + MCP providers + system prompt.
    Best for: tool-calling agents (Jira, Confluence, Google, etc.)

  orchestrator_node (COORDINATOR — not a standalone agent)
    Plans work and delegates to other agents via branches.
    Best for: multi-agent workflows. See build_workflow guide.

  claude_agent_node (AUTONOMOUS — Claude SDK)
    Runs full Claude sessions with built-in tools (file I/O,
    bash, web search). Connects to Claude via Vertex AI.
    Best for: complex autonomous tasks, code generation, deep
    analysis. Does NOT use a UnifAI LLM resource.

  a2a_agent_node (REMOTE — external agent)
    Delegates to a remote agent via the A2A protocol. Just needs
    a URL endpoint. No LLM, no providers.
    Best for: calling external agent services.

  deep_agent_node (ADVANCED — LangChain Deep Agents)
    Built-in planning (todos), context management, and subagent
    delegation. Similar to custom_agent but with deeper reasoning.
    Best for: complex multi-step tasks needing structured planning.

  If unsure, start with custom_agent_node — it covers most cases.

═══ BUILDING A CUSTOM AGENT (custom_agent_node) ═══

STEP 1 — Decide what it connects to
  Run: list_resources(category="providers")
  See existing MCP providers. Pick one or create a new one.

STEP 2 — Pick an LLM
  Run: list_resources(category="llms")
  Quick picks:
    • Simple tasks → Gemini Flash
    • Complex reasoning → Gemini 3.1 Pro
    Call get_guide("llm_selection") for detailed guidance.

STEP 3 — Write a system prompt
  Good system prompts include:
    • ROLE: "You are a [specific role] assistant that..."
    • CAPABILITIES: What tools/data the agent has access to
    • RESPONSE FORMAT: How to structure output (tables, citations)
    • CONSTRAINTS: What NOT to do (no guessing, no PII, etc.)
    • TONE: Professional, concise, collaborative, etc.
  Tip: Be specific! "You are a Jira expert agent" > "helpful assistant"

STEP 4 — Create the resource
  create_resource(
    category="nodes",
    element_type="custom_agent_node",
    name="My Agent Name",
    config='{
      "llm": "$ref:<llm_id>",
      "providers": ["$ref:<provider_id>"],
      "system_message": "<your system prompt>",
      "strategy_type": "react",
      "max_rounds": 100,
      "retries": 1
    }'
  )

STEP 5 — Verify
  get_resource_details(resource_id="<new_id>")

OPTIONS: max_rounds (100), retries (1), strategy_type ("react"),
  retriever (optional RAG), tools (ssh_exec, web_fetch, etc.)

═══ BUILDING A CLAUDE AGENT (claude_agent_node) ═══

Claude agents connect directly to Claude via Vertex AI — they
do NOT use a UnifAI LLM resource.

STEP 1 — Get Vertex AI credentials
  You need: vertex_project_id and vertex_region (default: us-east5)

STEP 2 — Choose model and effort
  Models: claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5
  Effort: low (fast), medium (balanced), high (deep), xhigh (Opus only)

STEP 3 — Create the resource
  create_resource(
    category="nodes",
    element_type="claude_agent_node",
    name="My Claude Agent",
    config='{
      "vertex_project_id": "<gcp_project>",
      "vertex_region": "us-east5",
      "model": "claude-sonnet-4-6",
      "system_prompt": "<your system prompt>",
      "max_turns": 200,
      "effort": "medium",
      "providers": ["$ref:<provider_id>"],
      "retries": 1
    }'
  )

OPTIONS: permission_mode ("bypassPermissions"/"acceptEdits"/"plan"),
  allowed_tools[], disallowed_tools[], skills_repos{}, env_vars{}

═══ BUILDING AN A2A AGENT (a2a_agent_node) ═══

A2A agents delegate to a remote agent — no LLM, no providers.
Just point to the URL and optionally authenticate.

  create_resource(
    category="nodes",
    element_type="a2a_agent_node",
    name="My Remote Agent",
    config='{
      "base_url": "http://<remote-agent-host>:10000",
      "bearer_token": "<optional_token>",
      "retries": 1
    }'
  )

The agent_card is auto-fetched from the remote endpoint.

═══ BUILDING A DEEP AGENT (deep_agent_node) ═══

Similar to custom_agent but with LangChain Deep Agent features:
built-in todo planning, context management, and subagent delegation.

  create_resource(
    category="nodes",
    element_type="deep_agent_node",
    name="My Deep Agent",
    config='{
      "llm": "$ref:<llm_id>",
      "providers": ["$ref:<provider_id>"],
      "system_message": "<your system prompt>",
      "retries": 1
    }'
  )

OPTIONS: tools[], retriever, cwd, env_vars{}
""",

    "build_workflow": """\
═══ HOW TO BUILD A WORKFLOW ═══

A workflow wires agents together with an execution plan.

STEP 1 — Choose your pattern
  Call get_guide("workflow_patterns") for detailed options.
  Quick decision:
    • 1 agent → Single Agent pattern
    • Multiple agents, flexible routing → Orchestrator pattern
    • Chain of agents → Sequential Pipeline

STEP 2 — Gather your node IDs
  Run: list_resources(category="nodes")
  Note the IDs of the agents you want to include.
  Every workflow needs these built-in nodes:
    • "user_question" — captures user input (type: user_question_node)
    • "final_answer" — returns the response (type: final_answer_node)

STEP 3 — Define the nodes array
  Built-in nodes MUST include rid, name, type, AND config with
  retries. Referenced nodes use $ref: in rid and set type to null:
    "nodes": [
      {"rid": "user_question", "name": "User Question Node",
       "type": "user_question_node",
       "config": {"retries": 1, "type": "user_question_node"}},
      {"rid": "final_answer", "name": "Final Answer Node",
       "type": "final_answer_node",
       "config": {"retries": 1, "type": "final_answer_node"}},
      {"rid": "$ref:<agent_1_id>", "name": "Agent 1",
       "type": null},
      {"rid": "$ref:<agent_2_id>", "name": "Agent 2",
       "type": null}
    ]

  Without all four fields (rid, name, type, config) on built-in
  nodes, the UI will render them as generic red boxes instead of
  properly styled system nodes.

STEP 4 — Define the execution plan
  Each step has: uid (unique name), node (which node to run),
  and after (dependencies — what must complete first).
  Use the UID convention: {NodeName}-{resourceId}-{number}
  Every step should include a "meta" field for UI display:
    "meta": {"description": "", "display_name": "", "tags": []}

  Example (single agent):
    "plan": [
      {"uid": "user_input", "node": "user_question",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "MyAgent-<id>-3", "node": "<agent_id>",
       "after": "user_input",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "finalize", "node": "final_answer",
       "after": "MyAgent-<id>-3",
       "meta": {"description": "", "display_name": "", "tags": []}}
    ]

  Example (orchestrator with 2 agents — REQUIRES branches):
    ORDER MATTERS: The router (exit_condition) must be set on
    the orchestrator BEFORE branches. Without the router,
    branches have no effect and the orchestrator completes
    immediately with empty output.
    See workflow_patterns guide for the full structure.

    "conditions": [
      {"rid": "$ref:<router_id>", "name": "Router",
       "type": "router_direct",
       "config": {"type": "router_direct"}}
    ],
    "plan": [
      {"uid": "user_input", "node": "user_question",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "finalize", "node": "final_answer",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "AgentA-<a_id>-4", "node": "<agent_a_id>",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "AgentB-<b_id>-5", "node": "<agent_b_id>",
       "meta": {"description": "", "display_name": "", "tags": []}},
      {"uid": "Orch-<orch_id>-3", "node": "<orch_id>",
       "after": ["user_input", "AgentA-<a_id>-4",
                  "AgentB-<b_id>-5"],
       "exit_condition": "<router_id>",
       "branches": {
         "AgentA-<a_id>-4": "AgentA-<a_id>-4",
         "AgentB-<b_id>-5": "AgentB-<b_id>-5",
         "finalize": "finalize"},
       "meta": {"description": "", "display_name": "", "tags": []}}
    ]

STEP 5 — Validate, then create
  validate_workflow(workflow_json="...")  — checks resource
  availability. Note: this does NOT check graph structure
  (orphans, required nodes, cycles). The UI canvas runs those
  additional checks. Never save a workflow with structural errors.

  Include a meaningful description that summarizes what the
  workflow does and which agents it contains. This helps users
  understand the workflow at a glance in the UI.
  Example: "Orchestrator workflow that queries Google Workspace
  and Jira Cloud via dedicated agents, then produces a polished
  summary via a Reporter agent."

  create_workflow(workflow_json="...", title="My Workflow",
    description="<describe the workflow and its agents>")

STEP 6 — Test it
  run_workflow(workflow="My Workflow", prompt="Test question")
  Verify the result contains actual output — not just a COMPLETED
  status with empty output. If the workflow completes but returns
  empty output, see TROUBLESHOOTING below.

TROUBLESHOOTING:
  Workflow completes but returns empty output:
    • Missing exit_condition — the orchestrator needs a router
      to know how to delegate. Check that exit_condition is set
      with a BARE condition resource ID (no $ref: prefix).
    • Missing branches — the orchestrator needs branch targets.
      Branches must be set AFTER exit_condition. Branch keys AND
      values must be step UIDs, and must include
      "finalize": "finalize".
    • "finalize" has "after" — in orchestrator workflows, the
      finalize step must have NO "after" dependency. The
      orchestrator routes to finalize internally via branches.
    • $ref: in plan node values — plan "node" fields must use
      bare resource IDs, not "$ref:<id>".

  UI renders nodes as red boxes:
    • Built-in nodes (user_question, final_answer) are missing
      one of: rid, name, type, or config. All four are required.
      Config must include "retries": 1 and the type field.

  "Too many start nodes" validation error:
    • Agent steps that are branch targets should have NO "after".
      They appear as start nodes only if they aren't listed in
      any orchestrator's branches.

  Validation passes but workflow fails at runtime:
    • validate_workflow only checks resource availability, not
      graph structure. Ensure your plan follows the patterns in
      get_guide("workflow_patterns") exactly.

GRAPH VALIDATION RULES:
  • Exactly 1 start node (user_input) — steps with no "after"
    AND not branch targets count as start nodes.
  • Exactly 1 end node (finalize) — steps with no outgoing edges
    count as end nodes.
  • All nodes must be reachable (no orphans).
  • Orchestrator must have branches + exit_condition.
  • All branch-target agents must have a return path back to the
    orchestrator (via the orchestrator's "after" list).
""",

    "system_prompts": """\
═══ SYSTEM PROMPT BEST PRACTICES ═══

A great system prompt turns a generic LLM into a focused expert.

─── STRUCTURE ───
  1. ROLE — Who is this agent? Be specific.
     ✗ "You are a helpful assistant"
     ✓ "You are a Jira Cloud Expert Agent that manages tickets
        with surgical precision"

  2. CAPABILITIES — What tools/data does it have?
     "You have access to Confluence via MCP tools. Use
     document_retrieval to search pages and spaces."

  3. INSTRUCTIONS — How should it behave?
     "Always search before creating. Present existing tickets
     before making duplicates."

  4. RESPONSE FORMAT — How to structure output.
     "Use markdown tables for lists. Cite sources with
     [Page Title](URL). Provide a TL;DR for long answers."

  5. CONSTRAINTS — What NOT to do.
     "Never guess deadlines. Never expose API keys or PII.
     If data is missing, say so clearly."

  6. EXAMPLES (optional) — Show the expected interaction.
     "User: Create a bug for login crash
      You: First, I'll search for existing bugs..."

─── TIPS ───
  • Shorter is often better. 200-500 words is the sweet spot.
  • Use markdown headers (##) to organize sections.
  • Include specific tool names the agent should use.
  • Define fallback behavior: what to say when data isn't found.
  • Set the tone: "professional teammate" vs "concise engineer"

─── ANTI-PATTERNS ───
  ✗ Repeating what the LLM already knows ("You are an AI...")
  ✗ Vague instructions ("Be helpful and accurate")
  ✗ Overly long prompts (>1000 words) — diminishing returns
  ✗ Contradictory rules ("Always be brief" + "Explain everything")
""",
}

_GUIDE_TOPICS = ", ".join(sorted(_GUIDES.keys()))


@mcp.tool()
async def get_guide(topic: str = "quick_start") -> str:
    """Get a detailed guide on a UnifAI topic.

    Returns step-by-step playbooks, decision matrices, and best
    practices for building agents and workflows.

    Available topics: quick_start, workflow_patterns, llm_selection,
    resource_types, build_agent, build_workflow, system_prompts

    Args:
        topic: The guide topic (default: quick_start).
    """
    content = _GUIDES.get(topic.lower().strip())
    if content:
        return content
    return (
        f"Unknown topic: '{topic}'\n\n"
        f"Available topics: {_GUIDE_TOPICS}\n\n"
        "Call get_guide with one of these topics for detailed guidance."
    )


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


_REF_PREFIX = "$ref:"


async def _enrich_draft_refs(
    draft: dict,
    unifai: "UnifAIClient",
) -> dict:
    """Populate missing ``name`` and ``type`` on ``$ref:`` resources.

    The validate endpoint requires every resource entry to have string
    ``name`` and ``type`` fields, but callers typically only supply a
    ``rid``.  This helper resolves each ``$ref:`` to fill in the gaps
    so both validate and create behave consistently.
    """
    resource_sections = ("nodes", "llms", "tools", "providers", "retrievers", "conditions")
    refs_to_resolve: dict[str, list[dict]] = {}

    for section in resource_sections:
        for entry in draft.get(section, []):
            rid = entry.get("rid", "")
            if rid.startswith(_REF_PREFIX) and (not entry.get("name") or not entry.get("type")):
                raw_id = rid[len(_REF_PREFIX):]
                refs_to_resolve.setdefault(raw_id, []).append(entry)

    if not refs_to_resolve:
        return draft

    async def _fetch_meta(raw_id: str) -> tuple[str, str, str]:
        try:
            doc = await unifai.get_resource(raw_id)
            return raw_id, doc.get("name", ""), doc.get("type", "")
        except Exception:
            return raw_id, "", ""

    results = await asyncio.gather(*[_fetch_meta(rid) for rid in refs_to_resolve])

    for raw_id, name, rtype in results:
        for entry in refs_to_resolve.get(raw_id, []):
            if not entry.get("name") and name:
                entry["name"] = name
            if not entry.get("type") and rtype:
                entry["type"] = rtype

    return draft


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

    await _enrich_draft_refs(draft, unifai)

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

    await _enrich_draft_refs(draft, unifai)

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

    IMPORTANT: This validates resource availability (whether referenced
    resources exist and are reachable). It does NOT validate graph
    structure (orphan nodes, required start/end nodes, orchestrator
    branches, cycle detection). Graph validation is performed by the
    UI canvas. Always ensure your workflow follows the correct pattern
    from get_guide("workflow_patterns") before saving.

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

    await _enrich_draft_refs(draft, unifai)

    try:
        result = await unifai.validate_blueprint_draft(draft, timeout_seconds=30.0)
    except Exception as exc:
        logger.exception("Failed to validate workflow")
        return f"Failed to validate workflow: {exc}"

    is_valid = result.get("is_valid", False)
    element_results = result.get("element_results", {})

    ok_items: list[str] = []
    fail_items: list[str] = []

    for rid, er in element_results.items():
        name = er.get("name") or rid
        el_type = er.get("element_type", "")
        label = f"{name} ({el_type})" if el_type else name

        msgs = er.get("messages", [])
        detail_parts: list[str] = []
        for msg in msgs:
            msg_text = msg.get("message", "") if isinstance(msg, dict) else str(msg)
            if msg_text:
                detail_parts.append(msg_text)

        if er.get("is_valid"):
            ok_items.append(f"  [OK] {label}")
        else:
            reason = detail_parts[0] if detail_parts else "unknown error"
            line = f"  [FAIL] {label}\n         Reason: {reason}"

            dep_results = er.get("dependency_results", {})
            failed_deps = [
                dep_name for dep_name, dep_r in dep_results.items()
                if not dep_r.get("is_valid")
            ] if isinstance(dep_results, dict) else []
            if failed_deps:
                line += f"\n         Failed dependencies: {', '.join(failed_deps)}"

            for extra in detail_parts[1:]:
                line += f"\n         {extra}"

            fail_items.append(line)

    lines: list[str] = []
    if is_valid:
        lines.append(f"Validation result: VALID ({len(ok_items)} resources checked)")
    else:
        lines.append(
            f"Validation result: INVALID "
            f"({len(fail_items)} failed, {len(ok_items)} passed)"
        )

    if fail_items:
        lines.append("\nFailed resources:")
        lines.extend(fail_items)

    if ok_items:
        lines.append("\nPassed resources:")
        lines.extend(ok_items)

    if not is_valid:
        lines.append(
            "\n---"
            "\nNote: Some providers (e.g. Google, GitHub) may fail validation "
            "due to backend connectivity checks that don't fully support "
            "draft-mode credential resolution. If these providers work at "
            "runtime (when you chat with the workflow), these failures can "
            "be safely ignored."
        )

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
            exit_cond = step.get("exit_condition")
            if exit_cond:
                line += f"  (exit_condition: {exit_cond})"
            branches = step.get("branches")
            if branches:
                branch_list = ", ".join(f"{k}→{v}" for k, v in branches.items())
                line += f"  (branches: {branch_list})"
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
