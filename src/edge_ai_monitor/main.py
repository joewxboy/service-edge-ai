"""Service entry point: wires the components together and runs the monitor."""

import argparse
import logging
import signal
import sys
import threading
from typing import Any, Dict, List, Optional

from .anax_client import AnaxClient
from .config import Config, ConfigError
from .context import ContextLibrary
from .error_analyzer import AnalysisResult, ErrorAnalyzer
from .health import HealthServer
from .log_monitor import ErrorEvent, LogMonitor
from .ollama_client import OllamaClient
from .remediation import RemediationGenerator
from .workload_registry import Workload, WorkloadRegistry, WorkloadState

logger = logging.getLogger(__name__)


class MonitorService:
    """Owns the component graph and the service lifecycle.

    Data flows one way: discovery -> log monitoring -> analysis queue ->
    proposal generation. Each stage runs on its own thread so a slow LLM never
    stalls log reading.
    """

    def __init__(self, config: Config):
        self.config = config
        self._shutdown = threading.Event()
        self.startup_errors: List[str] = []

        self.anax_client = AnaxClient(
            base_url=config.anax.base_url,
            timeout=config.anax.timeout,
            max_retries=config.anax.max_retries,
        )
        self.llm_client = OllamaClient(
            model=config.llm.model,
            host=config.llm.host,
            timeout=config.llm.timeout,
            max_retries=config.llm.max_retries,
        )
        self.registry = WorkloadRegistry(
            self.anax_client,
            poll_interval=config.discovery.poll_interval,
            on_change=self._on_workload_change,
        )
        self.generator = RemediationGenerator(proposal_dir=config.proposals.directory)
        self.context_library = ContextLibrary(
            context_dir=config.context.directory,
            max_total_bytes=config.context.max_total_bytes,
            max_file_bytes=config.context.max_file_bytes,
        )
        self.analyzer = ErrorAnalyzer(
            llm_client=self.llm_client,
            metadata_provider=self._workload_metadata,
            on_result=self._on_analysis_result,
            max_queue_size=config.analysis.max_queue_size,
            cache_ttl=config.analysis.cache_ttl,
            knowledge_provider=self._workload_knowledge if config.context.enabled else None,
        )
        self.log_monitor = LogMonitor(
            on_error=self._on_error_detected,
            poll_interval=config.logs.poll_interval,
            max_buffer_bytes=config.max_buffer_bytes,
        )
        self.health_server = (
            HealthServer(self.status, host=config.health.host, port=config.health.port)
            if config.health.enabled
            else None
        )

    # ---------------- component wiring ----------------

    def _workload_metadata(self, workload_id: str) -> Dict[str, Any]:
        workload = self.registry.get(workload_id)
        return workload.metadata() if workload else {"workload_id": workload_id}

    def _workload_knowledge(self, workload_id: str) -> str:
        """Domain guidance for a workload: its own context files plus node-wide."""
        workload = self.registry.get(workload_id)
        paths = workload.context_paths if workload else []
        return self.context_library.build_context_block(paths)

    def _on_workload_change(self, workload: Workload, previous: WorkloadState) -> None:
        """Attach or detach log monitoring as workloads come and go."""
        if workload.is_monitorable:
            self.log_monitor.watch_workload(workload)
        elif previous == WorkloadState.RUNNING:
            self.log_monitor.unwatch_workload(workload.workload_id)

    def _on_error_detected(self, event: ErrorEvent) -> None:
        """Hand a detected error to the analysis queue (never blocks)."""
        self.analyzer.submit(event)

    def _on_analysis_result(self, result: AnalysisResult) -> None:
        """Turn a completed analysis into a persisted proposal."""
        self.generator.handle_result(result)

    # ---------------- startup validation ----------------

    def validate_startup(self) -> bool:
        """Check external dependencies. Returns True when all are reachable.

        Failures are recorded and reported but do not abort startup: both anax
        and Ollama may come up after the monitor does, and the polling loops
        recover on their own.
        """
        self.startup_errors = []

        if not self.anax_client.is_reachable():
            message = f"anax API not reachable at {self.config.anax.base_url}"
            logger.error("%s; discovery will retry", message)
            self.startup_errors.append(message)

        if not self.llm_client.is_available():
            message = f"Ollama runtime not available at {self.config.llm.host}"
            logger.error("%s; analysis will fail until it starts", message)
            self.startup_errors.append(message)
        elif self.config.llm.auto_pull and not self.llm_client.ensure_model():
            message = f"LLM model {self.config.llm.model} could not be prepared"
            logger.error(message)
            self.startup_errors.append(message)

        return not self.startup_errors

    def status(self) -> Dict[str, Any]:
        """Health/status payload served by the health endpoint."""
        workloads = self.registry.all_workloads()
        return {
            # Healthy means the service loop is running; a temporarily
            # unreachable dependency is reported but not fatal.
            "healthy": not self._shutdown.is_set(),
            "startup_errors": list(self.startup_errors),
            "workloads_discovered": len(workloads),
            "workloads_monitored": len(self.registry.monitorable_workloads()),
            "log_paths_watched": len(self.log_monitor.watched_paths),
            "analysis_queue_depth": self.analyzer.queue_depth,
            "analyses_completed": self.analyzer.analyzed_count,
            "analyses_failed": self.analyzer.failed_count,
            "analyses_dropped": self.analyzer.dropped_count,
            "proposals_written": self.generator.written_count,
            "proposals_updated": self.generator.updated_count,
            "proposal_storage_failures": self.generator.storage_failures,
            **(
                self.context_library.describe()
                if self.config.context.enabled
                else {"context_enabled": False}
            ),
        }

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        """Start every background loop."""
        logger.info("starting edge-ai monitor")
        self.validate_startup()

        # Seed the registry before the loops start so the first errors have
        # workload metadata available.
        self.registry.poll_once()
        for workload in self.registry.monitorable_workloads():
            self.log_monitor.watch_workload(workload)

        self.analyzer.start()
        self.log_monitor.start()
        self.registry.start()
        if self.health_server is not None:
            self.health_server.start()

        if self.config.context.enabled:
            described = self.context_library.describe()
            logger.info(
                "context: %d node document(s), %d bytes from %s",
                described["node_context_documents"],
                described["node_context_bytes"],
                described["context_dir"],
            )

        logger.info(
            "monitor running: %d workload(s) discovered, %d monitored",
            len(self.registry.all_workloads()),
            len(self.registry.monitorable_workloads()),
        )

    def stop(self) -> None:
        """Stop every background loop in reverse dependency order."""
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        logger.info("shutting down edge-ai monitor")

        if self.health_server is not None:
            self.health_server.stop()
        self.registry.stop()
        self.log_monitor.stop()
        # Stopped last so queued analyses can drain into proposals.
        self.analyzer.stop()
        logger.info("shutdown complete")

    def run_forever(self) -> None:
        """Start the service and block until a shutdown signal arrives."""
        self.start()
        try:
            self._shutdown.wait()
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            pass
        finally:
            self.stop()

    def request_shutdown(self) -> None:
        """Signal run_forever to return."""
        self._shutdown.set()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="edge-ai-monitor",
        description="LLM-based workload monitoring for Open Horizon edge nodes",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and dependencies, then exit",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        # Logging is not configured yet, so report to stderr directly.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    config.configure_logging()
    service = MonitorService(config)

    if args.check:
        ok = service.validate_startup()
        for message in service.startup_errors:
            print(f"FAIL: {message}", file=sys.stderr)
        if ok:
            print("OK: configuration valid and dependencies reachable")
        return 0 if ok else 1

    def handle_signal(signum, _frame):
        logger.info("received %s; shutting down", signal.Signals(signum).name)
        service.request_shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    service.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
