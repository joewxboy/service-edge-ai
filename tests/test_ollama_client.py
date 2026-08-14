"""Unit tests for OllamaClient with a mocked Ollama runtime."""

import json

import pytest

from edge_ai_monitor.log_monitor import ErrorEvent
from edge_ai_monitor.ollama_client import (
    LLMError,
    LLMTimeout,
    OllamaClient,
    build_analysis_prompt,
    validate_analysis,
)

VALID_ANALYSIS = {
    "error_summary": "Database connection refused",
    "root_cause": "The postgres container is not listening on port 5432",
    "severity": "high",
    "remediation_steps": ["Check the database container", "Restart the service"],
    "confidence": 0.87,
}


class FakeOllama:
    """Replays queued chat responses; records the prompts it received."""

    def __init__(self, *responses, models=None):
        self.responses = list(responses)
        self.prompts = []
        self.pulled = []
        self._models = models if models is not None else [{"name": "llama3.2:3b-instruct-q4_K_M"}]

    def chat(self, model, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        result = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(result, Exception):
            raise result
        return {"message": {"content": result}}

    def list(self):
        if isinstance(self._models, Exception):
            raise self._models
        return {"models": self._models}

    def pull(self, model):
        self.pulled.append(model)


def make_event(**kwargs):
    defaults = dict(
        workload_id="examples/example.com.sensor_1.2.0_amd64",
        log_path="/var/log/workloads/sensor/app.log",
        line_number=42,
        line="ERROR could not connect to database",
        matched_pattern="ERROR",
        context_before=["INFO starting up"],
        context_after=["INFO retrying"],
    )
    defaults.update(kwargs)
    return ErrorEvent(**defaults)


METADATA = {"service_name": "sensor", "version": "1.2.0", "organization": "examples"}


# ---------------- prompt construction ----------------


def test_prompt_includes_workload_context():
    prompt = build_analysis_prompt(make_event(), METADATA)
    assert "sensor" in prompt
    assert "examples" in prompt
    assert "1.2.0" in prompt


def test_prompt_includes_error_location_and_context():
    prompt = build_analysis_prompt(make_event(), METADATA)
    assert "/var/log/workloads/sensor/app.log" in prompt
    assert "42" in prompt
    assert "ERROR could not connect to database" in prompt
    assert "INFO starting up" in prompt


def test_prompt_reports_occurrence_count():
    prompt = build_analysis_prompt(make_event(), METADATA, occurrences=7)
    assert "Occurrences: 7" in prompt


def test_prompt_handles_empty_metadata():
    prompt = build_analysis_prompt(make_event(), {})
    assert "no metadata available" in prompt


# ---------------- response validation ----------------


def test_validate_accepts_well_formed_analysis():
    result = validate_analysis(dict(VALID_ANALYSIS))
    assert result["severity"] == "high"
    assert result["confidence"] == 0.87
    assert len(result["remediation_steps"]) == 2


@pytest.mark.parametrize(
    "field", ["error_summary", "root_cause", "severity", "remediation_steps", "confidence"]
)
def test_validate_rejects_missing_field(field):
    payload = dict(VALID_ANALYSIS)
    del payload[field]
    with pytest.raises(LLMError):
        validate_analysis(payload)


def test_validate_rejects_unknown_severity():
    payload = dict(VALID_ANALYSIS, severity="catastrophic")
    with pytest.raises(LLMError):
        validate_analysis(payload)


def test_validate_normalises_severity_case():
    assert validate_analysis(dict(VALID_ANALYSIS, severity="CRITICAL"))["severity"] == "critical"


def test_validate_clamps_out_of_range_confidence():
    assert validate_analysis(dict(VALID_ANALYSIS, confidence=1.7))["confidence"] == 1.0
    assert validate_analysis(dict(VALID_ANALYSIS, confidence=-0.4))["confidence"] == 0.0


def test_validate_rejects_non_numeric_confidence():
    with pytest.raises(LLMError):
        validate_analysis(dict(VALID_ANALYSIS, confidence="very sure"))


def test_validate_wraps_single_string_step():
    result = validate_analysis(dict(VALID_ANALYSIS, remediation_steps="Restart it"))
    assert result["remediation_steps"] == ["Restart it"]


def test_validate_rejects_empty_steps():
    with pytest.raises(LLMError):
        validate_analysis(dict(VALID_ANALYSIS, remediation_steps=[]))


# ---------------- inference ----------------


def test_analyze_parses_valid_json():
    client = OllamaClient(client=FakeOllama(json.dumps(VALID_ANALYSIS)))
    result = client.analyze(make_event(), METADATA)
    assert result["error_summary"] == "Database connection refused"


def test_analyze_extracts_json_from_markdown_fence():
    fenced = f"Here you go:\n```json\n{json.dumps(VALID_ANALYSIS)}\n```"
    client = OllamaClient(client=FakeOllama(fenced))
    assert client.analyze(make_event(), METADATA)["severity"] == "high"


def test_analyze_extracts_json_surrounded_by_prose():
    noisy = f"Sure! {json.dumps(VALID_ANALYSIS)} Hope that helps."
    client = OllamaClient(client=FakeOllama(noisy))
    assert client.analyze(make_event(), METADATA)["confidence"] == 0.87


def test_analyze_retries_once_on_malformed_response():
    fake = FakeOllama("not json at all", json.dumps(VALID_ANALYSIS))
    client = OllamaClient(client=fake)
    assert client.analyze(make_event(), METADATA)["severity"] == "high"
    assert len(fake.prompts) == 2
    # The retry prompt is clarified to demand raw JSON.
    assert "ONLY the raw JSON" in fake.prompts[1]


def test_analyze_raises_after_retries_exhausted():
    client = OllamaClient(client=FakeOllama("still not json"))
    with pytest.raises(LLMError):
        client.analyze(make_event(), METADATA)


def test_analyze_retries_on_missing_required_field():
    incomplete = json.dumps({"error_summary": "x", "severity": "low"})
    fake = FakeOllama(incomplete, json.dumps(VALID_ANALYSIS))
    assert OllamaClient(client=fake).analyze(make_event(), METADATA)["severity"] == "high"


def test_timeout_surfaces_as_llm_timeout():
    client = OllamaClient(client=FakeOllama(TimeoutError("read timed out")))
    with pytest.raises(LLMTimeout):
        client.analyze(make_event(), METADATA)


def test_timeout_is_not_retried():
    fake = FakeOllama(TimeoutError("timed out"))
    client = OllamaClient(client=fake)
    with pytest.raises(LLMTimeout):
        client.analyze(make_event(), METADATA)
    assert len(fake.prompts) == 1


def test_empty_response_is_an_error():
    client = OllamaClient(client=FakeOllama(""))
    with pytest.raises(LLMError):
        client.analyze(make_event(), METADATA)


# ---------------- model lifecycle ----------------


def test_ensure_model_noop_when_present():
    fake = FakeOllama(json.dumps(VALID_ANALYSIS))
    client = OllamaClient(client=fake)
    assert client.ensure_model() is True
    assert fake.pulled == []


def test_ensure_model_pulls_when_absent():
    fake = FakeOllama(json.dumps(VALID_ANALYSIS), models=[])
    client = OllamaClient(client=fake)
    assert client.ensure_model() is True
    assert fake.pulled == ["llama3.2:3b-instruct-q4_K_M"]


class ObjectResponseOllama:
    """Mimics the ollama package, which returns a ChatResponse model."""

    class _Message:
        def __init__(self, content):
            self.content = content

    class _Response:
        def __init__(self, content):
            self.message = ObjectResponseOllama._Message(content)

    def __init__(self, content):
        self.content = content

    def chat(self, model, messages, **kwargs):
        return self._Response(self.content)

    def list(self):
        return {"models": []}

    def pull(self, model):
        pass


def test_object_style_chat_response_is_parsed():
    """The ollama package returns ChatResponse, not a dict."""
    client = OllamaClient(client=ObjectResponseOllama(json.dumps(VALID_ANALYSIS)))
    assert client.analyze(make_event(), METADATA)["severity"] == "high"


def test_object_style_empty_content_is_an_error():
    client = OllamaClient(client=ObjectResponseOllama(""))
    with pytest.raises(LLMError):
        client.analyze(make_event(), METADATA)


def test_pull_does_not_inherit_the_short_inference_timeout():
    """A 2GB model pull must not be cut off by the 30s analysis budget."""
    client = OllamaClient(timeout=30.0)
    assert client.pull_timeout >= 600.0
    assert client.pull_timeout != client.timeout


def test_http_transport_pull_uses_pull_timeout():
    from edge_ai_monitor.ollama_client import _HttpOllama

    client = OllamaClient(timeout=30.0, pull_timeout=1800.0)
    client._client = _HttpOllama(client.host, client.timeout)
    client._injected = False
    assert client._pull_client().timeout == 1800.0


def test_injected_client_is_used_for_pull():
    fake = FakeOllama(json.dumps(VALID_ANALYSIS), models=[])
    client = OllamaClient(client=fake)
    client.ensure_model()
    assert fake.pulled == ["llama3.2:3b-instruct-q4_K_M"]


def test_is_available_reflects_runtime_state():
    assert OllamaClient(client=FakeOllama("{}")).is_available() is True
    down = OllamaClient(client=FakeOllama("{}", models=ConnectionError("refused")))
    assert down.is_available() is False
