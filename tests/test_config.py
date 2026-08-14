"""Unit tests for configuration loading, overrides, and validation."""

from pathlib import Path

import pytest
import yaml

from edge_ai_monitor.config import Config, ConfigError

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


# ---------------- defaults ----------------


def test_defaults_are_usable():
    config = Config()
    config.validate()
    assert config.discovery.poll_interval == 60.0
    assert config.llm.timeout == 30.0
    assert config.analysis.max_queue_size == 50
    assert config.proposals.directory == "/var/lib/monitor/proposals"


def test_missing_config_file_falls_back_to_defaults(tmp_path):
    config = Config.load(str(tmp_path / "absent.yaml"), env={})
    assert config.discovery.poll_interval == 60.0


def test_shipped_config_file_is_valid():
    config = Config.load(str(REPO_CONFIG), env={})
    assert config.llm.model == "llama3.2:3b-instruct-q4_K_M"
    assert config.health.port == 8080


def test_max_buffer_bytes_derived_from_mb():
    assert Config().max_buffer_bytes == 100 * 1024 * 1024


# ---------------- file loading ----------------


def test_file_values_override_defaults(tmp_path):
    path = write_config(tmp_path, {"discovery": {"poll_interval": 15.0}})
    assert Config.load(path, env={}).discovery.poll_interval == 15.0


def test_partial_config_keeps_other_defaults(tmp_path):
    path = write_config(tmp_path, {"llm": {"model": "tinyllama"}})
    config = Config.load(path, env={})
    assert config.llm.model == "tinyllama"
    assert config.llm.timeout == 30.0


def test_unknown_keys_are_ignored(tmp_path):
    path = write_config(tmp_path, {"llm": {"model": "tinyllama", "nonsense": 1}})
    assert Config.load(path, env={}).llm.model == "tinyllama"


def test_malformed_yaml_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm: [unclosed\n")
    with pytest.raises(ConfigError):
        Config.load(str(path), env={})


def test_non_mapping_section_raises(tmp_path):
    path = write_config(tmp_path, {"llm": "not-a-mapping"})
    with pytest.raises(ConfigError):
        Config.load(path, env={})


# ---------------- environment overrides ----------------


def test_env_overrides_file_values(tmp_path):
    path = write_config(tmp_path, {"discovery": {"poll_interval": 15.0}})
    config = Config.load(path, env={"MONITOR_POLL_INTERVAL": "5"})
    assert config.discovery.poll_interval == 5.0


def test_env_override_coerces_types():
    config = Config.load(
        "/nonexistent",
        env={
            "MONITOR_MAX_QUEUE_SIZE": "10",
            "MONITOR_LLM_TIMEOUT": "12.5",
            "MONITOR_HEALTH_PORT": "9090",
        },
    )
    assert config.analysis.max_queue_size == 10
    assert config.llm.timeout == 12.5
    assert config.health.port == 9090


@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("false", False), ("no", False)])
def test_boolean_env_override(value, expected):
    config = Config.load("/nonexistent", env={"MONITOR_LLM_AUTO_PULL": value})
    assert config.llm.auto_pull is expected


def test_invalid_env_value_raises():
    with pytest.raises(ConfigError):
        Config.load("/nonexistent", env={"MONITOR_POLL_INTERVAL": "not-a-number"})


def test_all_documented_env_vars_apply():
    env = {
        "MONITOR_ANAX_URL": "http://anax:8510",
        "MONITOR_LLM_HOST": "http://ollama:11434",
        "MONITOR_PROPOSAL_DIR": "/tmp/proposals",
        "MONITOR_LOG_LEVEL": "DEBUG",
    }
    config = Config.load("/nonexistent", env=env)
    assert config.anax.base_url == "http://anax:8510"
    assert config.llm.host == "http://ollama:11434"
    assert config.proposals.directory == "/tmp/proposals"
    assert config.logging.level == "DEBUG"


# ---------------- validation ----------------


@pytest.mark.parametrize(
    "env",
    [
        {"MONITOR_POLL_INTERVAL": "0"},
        {"MONITOR_LOG_POLL_INTERVAL": "-1"},
        {"MONITOR_MAX_BUFFER_MB": "0"},
        {"MONITOR_LLM_TIMEOUT": "0"},
        {"MONITOR_MAX_QUEUE_SIZE": "0"},
        {"MONITOR_CACHE_TTL": "-5"},
        {"MONITOR_HEALTH_PORT": "70000"},
        {"MONITOR_LOG_LEVEL": "CHATTY"},
        {"MONITOR_ANAX_URL": "localhost:8510"},
        {"MONITOR_LLM_HOST": "ollama"},
        {"MONITOR_LLM_MODEL": ""},
        {"MONITOR_PROPOSAL_DIR": ""},
    ],
)
def test_invalid_settings_are_rejected(env):
    with pytest.raises(ConfigError):
        Config.load("/nonexistent", env=env)


def test_log_level_is_normalised_to_upper_case():
    assert Config.load("/nonexistent", env={"MONITOR_LOG_LEVEL": "debug"}).logging.level == "DEBUG"


def test_to_dict_round_trips_through_from_dict():
    original = Config.load("/nonexistent", env={"MONITOR_LLM_MODEL": "tinyllama"})
    restored = Config.from_dict(original.to_dict())
    assert restored.llm.model == "tinyllama"
    assert restored.to_dict() == original.to_dict()


# ---------------- export ----------------


def test_export_defaults_are_inert():
    config = Config()
    assert config.export.sinks == {}
    assert config.export.include_node_identity is True
    config.validate()


def test_export_sink_requires_a_type(tmp_path):
    path = write_config(tmp_path, {"export": {"sinks": {"bad": {"url": "https://x"}}}})
    with pytest.raises(ConfigError):
        Config.load(path, env={})


def test_export_sink_with_type_is_accepted(tmp_path):
    path = write_config(
        tmp_path,
        {"export": {"sinks": {"hook": {"type": "webhook", "url": "https://x", "level": "analysis"}}}},
    )
    config = Config.load(path, env={})
    assert config.export.sinks["hook"]["type"] == "webhook"


@pytest.mark.parametrize("env", [
    {"MONITOR_EXPORT_SPOOL_MAX_BYTES": "0"},
    {"MONITOR_EXPORT_SPOOL_MAX_BYTES": "-1"},
])
def test_invalid_spool_size_rejected(env):
    with pytest.raises(ConfigError):
        Config.load("/nonexistent", env=env)


def test_export_env_overrides():
    config = Config.load(
        "/nonexistent",
        env={"MONITOR_EXPORT_ENABLED": "false", "MONITOR_EXPORT_NODE_ID": "edge-7"},
    )
    assert config.export.enabled is False
    assert config.export.node_id == "edge-7"
