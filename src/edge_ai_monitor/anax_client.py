"""HTTP client for the Open Horizon anax API.

Only read-only endpoints are used: the monitor never mutates node state.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8510"


class AnaxError(Exception):
    """Raised when the anax API cannot be queried successfully."""


class AnaxClient:
    """Read-only HTTP client for the local anax agent API.

    Retries are bounded and use exponential backoff so that a temporarily
    unavailable agent does not stall the discovery loop indefinitely.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.session = session or requests.Session()

    def _get(self, path: str) -> Dict[str, Any]:
        """GET a JSON document from anax, retrying transient failures."""
        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                # Don't sleep after the final attempt.
                if attempt < self.max_retries - 1:
                    delay = self.backoff_factor * (2**attempt)
                    logger.warning(
                        "anax GET %s failed (attempt %d/%d): %s; retrying in %.1fs",
                        path,
                        attempt + 1,
                        self.max_retries,
                        exc,
                        delay,
                    )
                    time.sleep(delay)

        raise AnaxError(f"GET {url} failed after {self.max_retries} attempts: {last_error}")

    def get_node_status(self) -> Dict[str, Any]:
        """Return the node document from ``GET /node``."""
        return self._get("/node")

    def get_service_configs(self) -> List[Dict[str, Any]]:
        """Return service configurations from ``GET /service/config``.

        Anax wraps the list in a ``config`` key; a bare list is also accepted
        so the client works against both agent versions and test fixtures.
        """
        payload = self._get("/service/config")
        if isinstance(payload, list):
            return payload
        configs = payload.get("config", [])
        return configs if isinstance(configs, list) else []

    def get_service_definitions(self) -> List[Dict[str, Any]]:
        """Return active local service definitions from ``GET /service``.

        These carry the signed deployment string, which is where a workload's
        ``MONITORING_*`` opt-in variables live. Unlike ``/service/config``, this
        is populated on a policy-registered node.
        """
        payload = self._get("/service")
        if isinstance(payload, list):
            return payload
        definitions = payload.get("definitions", {})
        if isinstance(definitions, list):
            return definitions
        active = definitions.get("active", [])
        return active if isinstance(active, list) else []

    def get_agreements(self) -> List[Dict[str, Any]]:
        """Return active agreements from ``GET /agreement``.

        Anax returns ``{"agreements": {"active": [...], "archived": [...]}}``.
        Only active agreements indicate a currently running workload.
        """
        payload = self._get("/agreement")
        if isinstance(payload, list):
            return payload
        agreements = payload.get("agreements", {})
        if isinstance(agreements, list):
            return agreements
        active = agreements.get("active", [])
        return active if isinstance(active, list) else []

    def is_reachable(self) -> bool:
        """Return True when the anax API answers a node query."""
        try:
            self.get_node_status()
            return True
        except AnaxError:
            return False
