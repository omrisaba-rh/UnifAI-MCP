"""Async HTTP client for the UnifAI Multi-Agent System API."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class UnifAIClient:
    """Thin wrapper around the UnifAI MAS REST API.

    Authenticates via a pre-signed session cookie from the Identity Service,
    matching the pattern used by the UnifAI CLI.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 120,
        verify_ssl: bool = True,
        cache_ttl: int = 300,  # 5 minutes default cache TTL
    ):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=True,
            verify=verify_ssl,
            headers={"Accept": "application/json"},
        )
        # Cache for blueprints: {cache_key: (data, timestamp)}
        self._blueprint_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
        # Cache for user session ID sets: {cache_key: (set_of_ids, timestamp)}
        self._session_cache: dict[str, tuple[set[str], float]] = {}
        self._cache_ttl = cache_ttl

    def set_session_cookie(self, session_cookie: str) -> None:
        """Install the pre-signed session cookie for API authentication."""
        self._http.cookies.set("session", session_cookie)

    # ── Blueprints ──────────────────────────────────────────────

    async def list_blueprints(
        self,
        user_id: str,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """List available blueprints for a user.

        Args:
            user_id: The user ID to fetch blueprints for
            use_cache: Whether to use cached results (default: True)

        Returns:
            List of blueprint dictionaries
        """
        cache_key = f"bp_{user_id}"

        # Check cache if enabled
        if use_cache and cache_key in self._blueprint_cache:
            cached_data, timestamp = self._blueprint_cache[cache_key]
            age = time.time() - timestamp
            if age < self._cache_ttl:
                logger.debug(
                    "Using cached blueprints for user=%s (age=%.1fs)",
                    user_id, age
                )
                return cached_data
            else:
                logger.debug(
                    "Cache expired for user=%s (age=%.1fs > ttl=%ds)",
                    user_id, age, self._cache_ttl
                )

        # Fetch from API
        resp = await self._http.get(
            "/blueprints/available.blueprints.resolved.get",
            params={"userId": user_id, "identityType": "user"},
        )
        resp.raise_for_status()
        data = self._parse_json(resp)

        if isinstance(data, dict) and "items" in data:
            result = data["items"]
        else:
            result = data if isinstance(data, list) else []

        # Update cache
        self._blueprint_cache[cache_key] = (result, time.time())
        logger.debug(
            "Cached %d blueprints for user=%s",
            len(result), user_id
        )

        return result

    async def find_blueprint_by_name(
        self,
        name: str,
        user_id: str,
        use_cache: bool = True,
    ) -> str | None:
        """Return the blueprint ID whose name matches *name* (case-insensitive).

        Args:
            name: Blueprint name to search for
            user_id: User ID to search blueprints for
            use_cache: Whether to use cached blueprint list (default: True)

        Returns:
            Blueprint ID if found, None otherwise
        """
        blueprints = await self.list_blueprints(user_id, use_cache=use_cache)
        for bp in blueprints:
            bp_name = bp.get("spec_dict", {}).get("name", "")
            if bp_name.lower() == name.lower():
                return bp.get("blueprint_id", "")
        return None

    def _update_session_id_cache(
        self, user_key: str, sessions: list[dict[str, Any]]
    ) -> None:
        """Refresh the session-ownership cache from a session list response."""
        ids = set()
        for s in sessions:
            sid = s.get("session_id") or s.get("sessionId")
            if sid:
                ids.add(sid)
        self._session_cache[user_key] = (ids, time.time())

    async def user_owns_session(
        self, session_id: str, user_key: str
    ) -> bool:
        """Return True if *session_id* belongs to the authenticated user.

        Uses a cached set of session IDs (populated by list_user_sessions)
        to avoid an API call on every check.  Falls back to a fresh fetch
        when the cache is cold or expired.
        """
        cached = self._session_cache.get(user_key)
        if cached:
            ids, ts = cached
            if time.time() - ts < self._cache_ttl:
                return session_id in ids

        sessions = await self.list_user_sessions()
        self._update_session_id_cache(user_key, sessions)
        ids = self._session_cache[user_key][0]
        return session_id in ids

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._blueprint_cache.clear()
        self._session_cache.clear()
        logger.info("Cleared all caches")

    # ── Sessions / Workflow execution ───────────────────────────

    async def list_user_sessions(
        self, user_key: str | None = None
    ) -> list[dict[str, Any]]:
        """List the current user's workflow sessions (most recent first).

        If *user_key* is provided, the session-ownership cache is updated.
        """
        resp = await self._http.get("/sessions/session.user.list")
        resp.raise_for_status()
        data = self._parse_json(resp)
        sessions = data if isinstance(data, list) else []
        if user_key:
            self._update_session_id_cache(user_key, sessions)
        return sessions

    async def create_session(
        self,
        blueprint_id: str,
    ) -> str:
        """Create a new workflow session and return its session ID."""
        resp = await self._http.post(
            "/sessions/user.session.create",
            json={"blueprintId": blueprint_id},
        )
        resp.raise_for_status()
        data = self._parse_json(resp)
        if isinstance(data, str):
            return data
        return data.get("sessionId") or data.get("session_id") or data.get("runId", "")

    async def submit_session(
        self,
        session_id: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit a workflow turn (fire-and-forget), then poll for results."""
        resp = await self._http.post(
            "/sessions/user.session.submit",
            json={
                "sessionId": session_id,
                "inputs": inputs,
                "scope": "public",
                "sessionType": "Personal",
            },
        )
        if resp.status_code >= 400:
            logger.error(
                "submit_session failed: status=%s body=%s",
                resp.status_code, resp.text[:500],
            )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def get_session_status(
        self,
        session_id: str,
    ) -> str:
        resp = await self._http.get(
            "/sessions/session.status.get",
            params={"sessionId": session_id},
        )
        resp.raise_for_status()
        data = self._parse_json(resp)
        return data.get("status", "UNKNOWN") if isinstance(data, dict) else str(data)

    async def get_stream_status(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Check if the session's execution stream is still active."""
        resp = await self._http.get(
            "/sessions/session.stream.status",
            params={"sessionId": session_id},
        )
        if resp.status_code == 404:
            return {"is_active": False, "not_found": True}
        resp.raise_for_status()
        return self._parse_json(resp)

    async def get_session_state(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        resp = await self._http.get(
            "/sessions/session.state.get",
            params={"sessionId": session_id},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def get_session_chat(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        resp = await self._http.get(
            "/sessions/session.chat.get",
            params={"sessionId": session_id},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    # ── Catalog ───────────────────────────────────────────────

    async def list_catalog_categories(self) -> list[str]:
        """List all available resource categories (llms, tools, providers, …)."""
        resp = await self._http.get("/catalog/categories.list.get")
        resp.raise_for_status()
        data = self._parse_json(resp)
        return data.get("categories", []) if isinstance(data, dict) else []

    async def list_catalog_elements(self) -> dict[str, list[dict[str, Any]]]:
        """List all element types grouped by category."""
        resp = await self._http.get("/catalog/elements.list.get")
        resp.raise_for_status()
        data = self._parse_json(resp)
        return data.get("elements", {}) if isinstance(data, dict) else {}

    async def get_element_spec(
        self, category: str, element_type: str,
    ) -> dict[str, Any]:
        """Get the config schema and details for a specific element type."""
        resp = await self._http.get(
            "/catalog/element.spec.get",
            params={"category": category, "type": element_type},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    # ── Resources (inventory items: tools, LLMs, providers, …) ─

    async def create_resource(
        self,
        category: str,
        element_type: str,
        name: str,
        config: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        """Create a new resource in the user's library."""
        resp = await self._http.post(
            "/resources/resource.save",
            json={
                "category": category,
                "type": element_type,
                "name": name,
                "config": config,
            },
            params={"userId": user_id, "identityType": "user"},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def list_resources(
        self,
        user_id: str,
        category: str | None = None,
        element_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List resources in the user's library with optional filtering."""
        params: dict[str, Any] = {
            "userId": user_id,
            "identityType": "user",
            "limit": limit,
            "offset": offset,
        }
        if category:
            params["category"] = category
        if element_type:
            params["type"] = element_type
        resp = await self._http.get("/resources/resources.list", params=params)
        resp.raise_for_status()
        return self._parse_json(resp)

    async def get_resource(self, resource_id: str) -> dict[str, Any]:
        """Get a single resource by ID."""
        resp = await self._http.get(
            "/resources/resource.get",
            params={"resourceId": resource_id},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def update_resource(
        self,
        resource_id: str,
        config: dict[str, Any],
        name: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing resource's config and/or name."""
        body: dict[str, Any] = {"resourceId": resource_id, "config": config}
        if name is not None:
            body["name"] = name
        resp = await self._http.put("/resources/resource.update", json=body)
        resp.raise_for_status()
        return self._parse_json(resp)

    async def delete_resource(self, resource_id: str) -> dict[str, Any]:
        """Delete a resource from the user's library."""
        resp = await self._http.delete(
            "/resources/resource.delete",
            params={"resourceId": resource_id},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def validate_resource_config(
        self,
        category: str,
        element_type: str,
        config: dict[str, Any],
        name: str | None = None,
    ) -> dict[str, Any]:
        """Validate a resource config before saving."""
        body: dict[str, Any] = {
            "category": category,
            "type": element_type,
            "config": config,
        }
        if name:
            body["name"] = name
        resp = await self._http.post("/resources/config.validate", json=body)
        resp.raise_for_status()
        return self._parse_json(resp)

    # ── Blueprint management (create / update / delete) ────────

    async def save_blueprint(
        self,
        draft_dict: dict[str, Any],
        user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save a new blueprint draft."""
        import json as _json
        body: dict[str, Any] = {
            "blueprintRaw": _json.dumps(draft_dict),
        }
        if metadata:
            body["metadata"] = metadata
        resp = await self._http.post(
            "/blueprints/blueprint.save",
            json=body,
            params={"userId": user_id, "identityType": "user"},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def update_blueprint(
        self,
        blueprint_id: str,
        draft_dict: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        """Update an existing blueprint in-place."""
        import json as _json
        resp = await self._http.put(
            "/blueprints/blueprint.update",
            json={
                "blueprintId": blueprint_id,
                "blueprintRaw": _json.dumps(draft_dict),
            },
            params={"userId": user_id, "identityType": "user"},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def get_blueprint_info(self, blueprint_id: str) -> dict[str, Any]:
        """Get blueprint details by ID."""
        resp = await self._http.get(
            "/blueprints/blueprint.info.get",
            params={"blueprintId": blueprint_id},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def delete_blueprint(self, blueprint_id: str) -> dict[str, Any]:
        """Delete a blueprint by ID."""
        resp = await self._http.delete(
            "/blueprints/remove.blueprint",
            params={"blueprintId": blueprint_id},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    async def get_blueprint_draft_schema(self) -> dict[str, Any]:
        """Get the JSON schema for blueprint drafts."""
        resp = await self._http.get("/blueprints/blueprint.draft.schema.get")
        resp.raise_for_status()
        return self._parse_json(resp)

    async def validate_blueprint_draft(
        self,
        draft_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate a blueprint draft without saving it."""
        import json as _json
        resp = await self._http.post(
            "/blueprints/draft.validate",
            json={"draft": _json.dumps(draft_dict)},
        )
        resp.raise_for_status()
        return self._parse_json(resp)

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _parse_json(resp: httpx.Response) -> Any:
        """Parse response as JSON, raising a clear error if it isn't."""
        ct = resp.headers.get("content-type", "")
        if "json" not in ct and "text/html" in ct:
            raise ValueError(
                f"Expected JSON but got HTML from {resp.url} "
                f"(status {resp.status_code}). Check the API endpoint path."
            )
        return resp.json()

    async def close(self) -> None:
        await self._http.aclose()
