"""Discovery and state tracking for Open Horizon workloads on the local node."""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .anax_client import AnaxClient, AnaxError

logger = logging.getLogger(__name__)

DEFAULT_ERROR_PATTERNS = ["ERROR", "FATAL", "Exception", "CRITICAL"]


class WorkloadState(str, Enum):
    """Lifecycle state of a discovered workload."""

    REGISTERED = "registered"  # known to anax, no active agreement
    RUNNING = "running"  # has at least one active agreement
    STOPPED = "stopped"  # was running, agreement went away
    REMOVED = "removed"  # no longer present in anax responses


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


ENABLED_VAR = "MONITORING_ENABLED"
LOG_PATHS_VAR = "MONITORING_LOG_PATHS"
ERROR_PATTERNS_VAR = "MONITORING_ERROR_PATTERNS"
CONTEXT_PATHS_VAR = "MONITORING_CONTEXT_PATHS"
EXPORT_VAR = "MONITORING_EXPORT"

TRUE_VALUES = ("true", "1", "yes")


def _parse_list(value: str, variable: str) -> List[str]:
    """Parse a comma-separated list, or a JSON array when it starts with '['.

    The JSON form exists so patterns containing commas (``a{1,3}``) stay
    expressible in a flat environment variable.
    """
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError(f"{variable} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError(f"{variable} JSON value must be an array")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_environment(entries: Any) -> Dict[str, str]:
    """Turn a deployment ``environment`` list of "KEY=VALUE" into a dict."""
    env: Dict[str, str] = {}
    if isinstance(entries, dict):
        return {str(k): str(v) for k, v in entries.items()}
    if not isinstance(entries, list):
        return env
    for entry in entries:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        env[key.strip()] = value
    return env


@dataclass
class MonitoringConfig:
    """A workload's opt-in, read from its MONITORING_* deployment variables."""

    enabled: bool = False
    log_paths: List[str] = field(default_factory=list)
    error_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_ERROR_PATTERNS))
    # Files of domain knowledge (SKILL.md, runbooks) injected into this
    # workload's analysis prompts.
    context_paths: List[str] = field(default_factory=list)
    # Consent to export log-derived content off the node. Metadata-level export
    # is permitted regardless; analysis and full levels require this.
    export_content: bool = False

    @classmethod
    def from_env(cls, env: Dict[str, str]) -> "MonitoringConfig":
        """Build from deployment environment variables.

        An absent MONITORING_ENABLED means the workload never opted in. A
        malformed value disables monitoring rather than guessing at intent.
        """
        if ENABLED_VAR not in env:
            return cls(enabled=False)

        enabled = str(env[ENABLED_VAR]).strip().lower() in TRUE_VALUES
        if not enabled:
            return cls(enabled=False)

        try:
            log_paths = _parse_list(env.get(LOG_PATHS_VAR, ""), LOG_PATHS_VAR)
            patterns = _parse_list(env.get(ERROR_PATTERNS_VAR, ""), ERROR_PATTERNS_VAR)
            context_paths = _parse_list(env.get(CONTEXT_PATHS_VAR, ""), CONTEXT_PATHS_VAR)
        except ValueError as exc:
            logger.warning("ignoring monitoring opt-in: %s", exc)
            return cls(enabled=False)

        return cls(
            enabled=True,
            log_paths=log_paths,
            # An unset or empty pattern list falls back to the defaults.
            error_patterns=patterns or list(DEFAULT_ERROR_PATTERNS),
            context_paths=context_paths,
            export_content=str(env.get(EXPORT_VAR, "")).strip().lower() in TRUE_VALUES,
        )

    @classmethod
    def from_deployment(cls, deployment: Any) -> "MonitoringConfig":
        """Build from a service definition's deployment string or dict.

        Variables are combined across every container the deployment defines,
        so a multi-container service can declare them on any of them.
        """
        if isinstance(deployment, str):
            try:
                deployment = json.loads(deployment)
            except ValueError:
                logger.warning("service deployment string is not valid JSON")
                return cls(enabled=False)
        if not isinstance(deployment, dict):
            return cls(enabled=False)

        services = deployment.get("services")
        if not isinstance(services, dict):
            return cls(enabled=False)

        envs = [
            parse_environment(container.get("environment"))
            for container in services.values()
            if isinstance(container, dict)
        ]
        if not any(ENABLED_VAR in env for env in envs):
            return cls(enabled=False)

        # Any container may carry the opt-in; a single false value does not veto
        # a sibling's true, but an explicit opt-in with no paths is caught below.
        enabled = any(
            str(env.get(ENABLED_VAR, "")).strip().lower() in TRUE_VALUES for env in envs
        )
        if not enabled:
            return cls(enabled=False)

        log_paths: List[str] = []
        patterns: List[str] = []
        context_paths: List[str] = []
        try:
            for env in envs:
                # Union across containers, preserving order and dropping dupes.
                for path in _parse_list(env.get(LOG_PATHS_VAR, ""), LOG_PATHS_VAR):
                    if path not in log_paths:
                        log_paths.append(path)
                for pattern in _parse_list(
                    env.get(ERROR_PATTERNS_VAR, ""), ERROR_PATTERNS_VAR
                ):
                    if pattern not in patterns:
                        patterns.append(pattern)
                for path in _parse_list(env.get(CONTEXT_PATHS_VAR, ""), CONTEXT_PATHS_VAR):
                    if path not in context_paths:
                        context_paths.append(path)
        except ValueError as exc:
            logger.warning("ignoring monitoring opt-in: %s", exc)
            return cls(enabled=False)

        return cls(
            enabled=True,
            log_paths=log_paths,
            error_patterns=patterns or list(DEFAULT_ERROR_PATTERNS),
            context_paths=context_paths,
            export_content=any(
                str(env.get(EXPORT_VAR, "")).strip().lower() in TRUE_VALUES for env in envs
            ),
        )


@dataclass
class Workload:
    """A service known to the local anax agent."""

    workload_id: str
    url: str
    org: str
    version: str
    arch: str
    name: str
    monitoring: MonitoringConfig
    state: WorkloadState = WorkloadState.REGISTERED
    first_seen: str = field(default_factory=_utcnow)
    last_seen: str = field(default_factory=_utcnow)
    agreement_ids: List[str] = field(default_factory=list)

    @property
    def context_paths(self) -> List[str]:
        """Domain-knowledge files declared by this workload."""
        return list(self.monitoring.context_paths)

    @property
    def permits_content_export(self) -> bool:
        """Whether this workload consented to content leaving the node."""
        return self.monitoring.export_content

    @property
    def is_monitorable(self) -> bool:
        """Monitoring requires opt-in, a running workload, and log paths."""
        return (
            self.monitoring.enabled
            and self.state == WorkloadState.RUNNING
            and bool(self.monitoring.log_paths)
        )

    def metadata(self) -> Dict[str, Any]:
        """Context handed to the LLM alongside error text."""
        return {
            "workload_id": self.workload_id,
            "service_name": self.name,
            "service_url": self.url,
            "organization": self.org,
            "version": self.version,
            "architecture": self.arch,
            "state": self.state.value,
            "agreement_ids": list(self.agreement_ids),
        }


def make_workload_id(org: str, url: str, version: str, arch: str) -> str:
    """Stable identity for a workload across polls."""
    return f"{org}/{url}_{version}_{arch}"


class WorkloadRegistry:
    """Tracks discovered workloads and their state across polling cycles."""

    def __init__(
        self,
        anax_client: AnaxClient,
        poll_interval: float = 60.0,
        on_change: Optional[Callable[[Workload, WorkloadState], None]] = None,
    ):
        self.anax_client = anax_client
        self.poll_interval = poll_interval
        self.on_change = on_change
        self._workloads: Dict[str, Workload] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------- accessors ----------------

    def get(self, workload_id: str) -> Optional[Workload]:
        with self._lock:
            return self._workloads.get(workload_id)

    def all_workloads(self) -> List[Workload]:
        with self._lock:
            return list(self._workloads.values())

    def monitorable_workloads(self) -> List[Workload]:
        """Running workloads that opted into monitoring and declared log paths."""
        with self._lock:
            return [w for w in self._workloads.values() if w.is_monitorable]

    # ---------------- discovery ----------------

    def poll_once(self) -> List[Workload]:
        """Run a single discovery cycle; returns the current workload list.

        Anax being unreachable is logged and swallowed so the loop survives to
        the next interval.
        """
        try:
            configs = self.anax_client.get_service_configs()
            agreements = self.anax_client.get_agreements()
            definitions = self.anax_client.get_service_definitions()
        except AnaxError as exc:
            logger.error("workload discovery failed, will retry next interval: %s", exc)
            return self.all_workloads()

        agreements_by_workload = self._index_agreements(agreements)
        seen: set = set()

        # Discovery unions all three sources. /service/config only lists services
        # the operator supplied user input for and is empty on a
        # policy-registered node; agreements establish what is running; the
        # service definitions carry the MONITORING_* opt-in variables. No single
        # source sees every workload on the node.
        merged = self._merge_sources(configs, agreements, definitions)

        with self._lock:
            for config in merged:
                workload = self._upsert(config, agreements_by_workload)
                if workload is not None:
                    seen.add(workload.workload_id)

            # Anything no longer reported by anax is marked removed.
            for workload_id, workload in self._workloads.items():
                if workload_id not in seen and workload.state != WorkloadState.REMOVED:
                    self._transition(workload, WorkloadState.REMOVED)

            return list(self._workloads.values())

    @staticmethod
    def _merge_sources(
        configs: List[Dict[str, Any]],
        agreements: List[Dict[str, Any]],
        definitions: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Combine agreements, service definitions, and service configs.

        Later sources refine earlier ones for the same workload. Only the
        service definitions carry a ``deployment`` string, which is where the
        MONITORING_* opt-in variables live.
        """
        merged: Dict[str, Dict[str, Any]] = {}

        for agreement in agreements:
            if agreement.get("agreement_terminated_time"):
                continue
            workload = agreement.get("workload_to_run") or {}
            url = workload.get("url")
            if not url:
                continue
            workload_id = make_workload_id(
                workload.get("org", ""),
                url,
                workload.get("version", ""),
                workload.get("arch", ""),
            )
            merged[workload_id] = {
                "url": url,
                "org": workload.get("org", ""),
                "version": workload.get("version", ""),
                "arch": workload.get("arch", ""),
                # No deployment string here, so a workload known only through an
                # agreement is discovered but carries no opt-in.
            }

        for definition in definitions or []:
            # Local definitions name the service with specRef/organization
            # rather than the url/org used elsewhere.
            url = definition.get("specRef") or definition.get("url")
            if not url:
                continue
            org = definition.get("organization") or definition.get("org") or ""
            workload_id = make_workload_id(
                org,
                url,
                definition.get("version", ""),
                definition.get("arch", ""),
            )
            entry = merged.setdefault(
                workload_id,
                {"url": url, "org": org, "version": definition.get("version", "")},
            )
            entry.update(
                {
                    "url": url,
                    "org": org,
                    "version": definition.get("version", entry.get("version", "")),
                    "arch": definition.get("arch", entry.get("arch", "")),
                    # anax's "name" here is the full "<org>/<url>_<ver>_<arch>"
                    # id, not a service name, so let _upsert derive it from url.
                    "deployment": definition.get("deployment"),
                }
            )

        for config in configs:
            url = config.get("url")
            if not url:
                logger.warning("skipping service config without url: %s", config)
                continue
            workload_id = make_workload_id(
                config.get("org", ""),
                url,
                config.get("version", ""),
                config.get("arch", ""),
            )
            entry = merged.setdefault(workload_id, {})
            # Keep the deployment string a definition may have supplied.
            deployment = entry.get("deployment")
            entry.update(config)
            if deployment is not None and "deployment" not in config:
                entry["deployment"] = deployment

        return list(merged.values())

    @staticmethod
    def _index_agreements(agreements: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """Map workload id -> active agreement ids."""
        index: Dict[str, List[str]] = {}
        for agreement in agreements:
            # A terminated agreement no longer indicates a running workload.
            if agreement.get("agreement_terminated_time"):
                continue
            workload = agreement.get("workload_to_run") or {}
            url = workload.get("url")
            if not url:
                continue
            workload_id = make_workload_id(
                workload.get("org", ""),
                url,
                workload.get("version", ""),
                workload.get("arch", ""),
            )
            agreement_id = agreement.get("current_agreement_id", "")
            index.setdefault(workload_id, []).append(agreement_id)
        return index

    def _upsert(
        self, config: Dict[str, Any], agreements_by_workload: Dict[str, List[str]]
    ) -> Optional[Workload]:
        url = config.get("url")
        if not url:
            logger.warning("skipping service config without url: %s", config)
            return None

        org = config.get("org", "")
        version = config.get("version", "")
        arch = config.get("arch", "")
        workload_id = make_workload_id(org, url, version, arch)

        agreement_ids = agreements_by_workload.get(workload_id, [])
        new_state = WorkloadState.RUNNING if agreement_ids else WorkloadState.REGISTERED
        monitoring = MonitoringConfig.from_deployment(config.get("deployment"))
        if monitoring.enabled and not monitoring.log_paths:
            logger.warning(
                "workload %s set %s but declared no %s; not monitoring it",
                workload_id,
                ENABLED_VAR,
                LOG_PATHS_VAR,
            )

        existing = self._workloads.get(workload_id)
        if existing is None:
            workload = Workload(
                workload_id=workload_id,
                url=url,
                org=org,
                version=version,
                arch=arch,
                name=config.get("name") or url.rsplit(".", 1)[-1],
                monitoring=monitoring,
                state=new_state,
                agreement_ids=agreement_ids,
            )
            self._workloads[workload_id] = workload
            logger.info(
                "discovered workload %s (state=%s, monitoring=%s)",
                workload_id,
                new_state.value,
                monitoring.enabled,
            )
            if self.on_change:
                self.on_change(workload, WorkloadState.REGISTERED)
            return workload

        existing.last_seen = _utcnow()
        existing.monitoring = monitoring
        existing.agreement_ids = agreement_ids

        # A workload that was running and lost its agreements is stopped, not
        # merely registered.
        if new_state == WorkloadState.REGISTERED and existing.state == WorkloadState.RUNNING:
            new_state = WorkloadState.STOPPED

        if new_state != existing.state:
            self._transition(existing, new_state)
        return existing

    def _transition(self, workload: Workload, new_state: WorkloadState) -> None:
        previous = workload.state
        workload.state = new_state
        workload.last_seen = _utcnow()
        logger.info(
            "workload %s transitioned %s -> %s",
            workload.workload_id,
            previous.value,
            new_state.value,
        )
        if self.on_change:
            self.on_change(workload, previous)

    # ---------------- polling loop ----------------

    def start(self) -> None:
        """Start the background discovery loop."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="workload-discovery", daemon=True
        )
        self._thread.start()
        logger.info("workload discovery started (interval=%ss)", self.poll_interval)

    def _run(self) -> None:
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("unexpected error during workload discovery")
            # Subtract work time so the cadence stays close to the interval.
            elapsed = time.monotonic() - start
            self._stop.wait(max(0.0, self.poll_interval - elapsed))

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the discovery loop and wait for the thread to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("workload discovery stopped")
