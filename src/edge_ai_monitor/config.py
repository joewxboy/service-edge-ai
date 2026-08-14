"""Configuration loading, environment overrides, and validation."""

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/edge-ai-monitor/config.yaml"

# Environment variable -> (section, key, coercion). Every setting in
# config.yaml can be overridden at deploy time without editing the file.
ENV_OVERRIDES = {
    "MONITOR_ANAX_URL": ("anax", "base_url", str),
    "MONITOR_ANAX_TIMEOUT": ("anax", "timeout", float),
    "MONITOR_POLL_INTERVAL": ("discovery", "poll_interval", float),
    "MONITOR_LOG_POLL_INTERVAL": ("logs", "poll_interval", float),
    "MONITOR_MAX_BUFFER_MB": ("logs", "max_buffer_mb", int),
    "MONITOR_LLM_MODEL": ("llm", "model", str),
    "MONITOR_LLM_HOST": ("llm", "host", str),
    "MONITOR_LLM_TIMEOUT": ("llm", "timeout", float),
    "MONITOR_LLM_AUTO_PULL": ("llm", "auto_pull", lambda v: str(v).lower() in ("1", "true", "yes")),
    "MONITOR_MAX_QUEUE_SIZE": ("analysis", "max_queue_size", int),
    "MONITOR_CACHE_TTL": ("analysis", "cache_ttl", float),
    "MONITOR_CONTEXT_DIR": ("context", "directory", str),
    "MONITOR_CONTEXT_ENABLED": ("context", "enabled", lambda v: str(v).lower() in ("1", "true", "yes")),
    "MONITOR_CONTEXT_MAX_BYTES": ("context", "max_total_bytes", int),
    "MONITOR_CONTEXT_MAX_FILE_BYTES": ("context", "max_file_bytes", int),
    "MONITOR_PROPOSAL_DIR": ("proposals", "directory", str),
    "MONITOR_LOG_LEVEL": ("logging", "level", str),
    "MONITOR_LOG_FORMAT": ("logging", "format", str),
    "MONITOR_HEALTH_PORT": ("health", "port", int),
    "MONITOR_HEALTH_ENABLED": ("health", "enabled", lambda v: str(v).lower() in ("1", "true", "yes")),
}

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class AnaxConfig:
    base_url: str = "http://localhost:8510"
    timeout: float = 10.0
    max_retries: int = 3


@dataclass
class DiscoveryConfig:
    poll_interval: float = 60.0


@dataclass
class LogsConfig:
    poll_interval: float = 1.0
    max_buffer_mb: int = 100


@dataclass
class LLMConfig:
    model: str = "llama3.2:3b-instruct-q4_K_M"
    host: str = "http://localhost:11434"
    timeout: float = 30.0
    max_retries: int = 2
    auto_pull: bool = True


@dataclass
class AnalysisConfig:
    max_queue_size: int = 50
    cache_ttl: float = 900.0


@dataclass
class ContextConfig:
    """Domain knowledge injected into analysis prompts."""

    directory: str = "/etc/edge-ai-monitor/context"
    max_total_bytes: int = 8000
    max_file_bytes: int = 4000
    enabled: bool = True


@dataclass
class ProposalsConfig:
    directory: str = "/var/lib/monitor/proposals"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    output: str = "stdout"


@dataclass
class HealthConfig:
    enabled: bool = True
    port: int = 8080
    host: str = "0.0.0.0"


@dataclass
class Config:
    """Full service configuration."""

    anax: AnaxConfig = field(default_factory=AnaxConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    proposals: ProposalsConfig = field(default_factory=ProposalsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    health: HealthConfig = field(default_factory=HealthConfig)

    @property
    def max_buffer_bytes(self) -> int:
        return self.logs.max_buffer_mb * 1024 * 1024

    def to_dict(self) -> Dict[str, Any]:
        return {
            section.name: dict(vars(getattr(self, section.name)))
            for section in fields(self)
        }

    # ---------------- construction ----------------

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Config":
        """Build a Config from a nested dict, ignoring unknown keys."""
        data = data or {}
        if not isinstance(data, dict):
            raise ConfigError("configuration root must be a mapping")

        kwargs = {}
        for section in fields(cls):
            section_type = section.default_factory  # type: ignore[misc]
            values = data.get(section.name) or {}
            if not isinstance(values, dict):
                raise ConfigError(f"config section '{section.name}' must be a mapping")

            known = {f.name for f in fields(section_type())}
            unknown = set(values) - known
            for key in sorted(unknown):
                logger.warning(
                    "ignoring unknown config key '%s.%s'", section.name, key
                )
            kwargs[section.name] = section_type(
                **{k: v for k, v in values.items() if k in known}
            )
        return cls(**kwargs)

    @classmethod
    def load(
        cls,
        path: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> "Config":
        """Load config.yaml, apply environment overrides, and validate.

        A missing config file is not an error: the built-in defaults are a
        usable configuration on a standard edge node.
        """
        path = path or os.environ.get("MONITOR_CONFIG", DEFAULT_CONFIG_PATH)
        data: Dict[str, Any] = {}

        config_file = Path(path)
        if config_file.is_file():
            try:
                data = yaml.safe_load(config_file.read_text()) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ConfigError(f"could not read config file {path}: {exc}") from exc
            logger.info("loaded configuration from %s", path)
        else:
            logger.info("config file %s not found; using defaults", path)

        config = cls.from_dict(data)
        config.apply_env_overrides(env if env is not None else os.environ)
        config.validate()
        return config

    def apply_env_overrides(self, env: Dict[str, str]) -> None:
        """Override settings from MONITOR_* environment variables."""
        for var, (section_name, key, coerce) in ENV_OVERRIDES.items():
            if var not in env:
                continue
            raw = env[var]
            try:
                value = coerce(raw)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"invalid value for {var}: {raw!r} ({exc})") from exc
            setattr(getattr(self, section_name), key, value)
            logger.debug("applied override %s=%s", var, value)

    # ---------------- validation ----------------

    def validate(self) -> None:
        """Reject configurations that cannot produce a working service."""
        errors = []

        if self.discovery.poll_interval <= 0:
            errors.append("discovery.poll_interval must be greater than 0")
        if self.logs.poll_interval <= 0:
            errors.append("logs.poll_interval must be greater than 0")
        if self.logs.max_buffer_mb <= 0:
            errors.append("logs.max_buffer_mb must be greater than 0")
        if self.llm.timeout <= 0:
            errors.append("llm.timeout must be greater than 0")
        if not self.llm.model:
            errors.append("llm.model must not be empty")
        if self.analysis.max_queue_size <= 0:
            errors.append("analysis.max_queue_size must be greater than 0")
        if self.analysis.cache_ttl < 0:
            errors.append("analysis.cache_ttl must not be negative")
        if not 1 <= self.health.port <= 65535:
            errors.append("health.port must be between 1 and 65535")
        if not self.proposals.directory:
            errors.append("proposals.directory must not be empty")
        if self.context.max_total_bytes <= 0:
            errors.append("context.max_total_bytes must be greater than 0")
        if self.context.max_file_bytes <= 0:
            errors.append("context.max_file_bytes must be greater than 0")
        if self.context.max_file_bytes > self.context.max_total_bytes:
            errors.append(
                "context.max_file_bytes must not exceed context.max_total_bytes"
            )

        level = self.logging.level.upper()
        if level not in VALID_LOG_LEVELS:
            errors.append(
                f"logging.level must be one of {', '.join(VALID_LOG_LEVELS)}"
            )
        else:
            self.logging.level = level

        for url_field, value in (
            ("anax.base_url", self.anax.base_url),
            ("llm.host", self.llm.host),
        ):
            if not str(value).startswith(("http://", "https://")):
                errors.append(f"{url_field} must be an http(s) URL")

        if errors:
            raise ConfigError("invalid configuration: " + "; ".join(errors))

    def configure_logging(self) -> None:
        """Apply the logging section to the root logger."""
        logging.basicConfig(
            level=getattr(logging, self.logging.level, logging.INFO),
            format=self.logging.format,
            force=True,
        )
