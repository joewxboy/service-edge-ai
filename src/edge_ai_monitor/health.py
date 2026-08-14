"""Health check HTTP endpoint for container liveness probes."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    """Serves /health and /status from a supplied status callable."""

    status_provider: Callable[[], Dict[str, Any]] = staticmethod(dict)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in ("/", "/health", "/status"):
            self.send_error(404, "not found")
            return

        try:
            status = self.status_provider()
        except Exception:  # pragma: no cover - defensive
            logger.exception("health status provider failed")
            status = {"healthy": False, "error": "status unavailable"}

        body = json.dumps(status, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(200 if status.get("healthy", False) else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Route access logs through the service logger at debug level.
        logger.debug("health: " + fmt, *args)


class HealthServer:
    """Background HTTP server exposing service health."""

    def __init__(
        self,
        status_provider: Callable[[], Dict[str, Any]],
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.host = host
        self.port = port
        self.status_provider = status_provider
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start serving health checks; failure to bind is not fatal."""
        if self._server is not None:
            return

        handler = type(
            "BoundHealthHandler",
            (_HealthHandler,),
            {"status_provider": staticmethod(self.status_provider)},
        )
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            logger.error("could not bind health endpoint to %s:%s: %s", self.host, self.port, exc)
            self._server = None
            return

        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health-server", daemon=True
        )
        self._thread.start()
        logger.info("health endpoint listening on %s:%s", self.host, self.port)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the health server."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._server = None
        logger.info("health endpoint stopped")
