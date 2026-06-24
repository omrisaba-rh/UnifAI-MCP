"""Async HTTP client for the UnifAI Multi-Agent System API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class UnifAIClient:
    """Thin wrapper around the UnifAI MAS REST API.

    Authenticates via a pre-signed session cookie from the Identity Service,
    matching the pattern used by the UnifAI CLI.
    """

    def __init__(self, base_url: str, timeout: float = 120, verify_ssl: bool = True):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=True,
            verify=verify_ssl,
            headers={"Accept": "application/json"},
        )

    def set_session_cookie(self, session_cookie: str) -> None:
        """Install the pre-signed session cookie for API authentication."""
        self._http.cookies.set("session", session_cookie)

    # ── Blueprints ──────────────────────────────────────────────

    async def list_blueprints(
        self,
        user_id: str,
    ) -> list[dict[str, Any]]:
        resp = await self._http.get(
            "/blueprints/available.blueprints.resolved.get",
            params={"userId": user_id, "identityType": "user"},
        )
        resp.raise_for_status()
        data = self._parse_json(resp)
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        return data if isinstance(data, list) else []

    async def find_blueprint_by_name(
        self,
        name: str,
        user_id: str,
    ) -> str | None:
        """Return the blueprint ID whose name matches *name* (case-insensitive)."""
        blueprints = await self.list_blueprints(user_id)
        for bp in blueprints:
            bp_name = bp.get("spec_dict", {}).get("name", "")
            if bp_name.lower() == name.lower():
                return bp.get("blueprint_id", "")
        return None

    # ── Sessions / Workflow execution ───────────────────────────

    async def list_user_sessions(self) -> list[dict[str, Any]]:
        """List the current user's workflow sessions (most recent first)."""
        resp = await self._http.get("/sessions/session.user.list")
        resp.raise_for_status()
        data = self._parse_json(resp)
        return data if isinstance(data, list) else []

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
