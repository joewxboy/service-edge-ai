"""Local LLM inference against an Ollama runtime.

The ``ollama`` package is imported lazily so that the rest of the service (and
its tests) run on images where the client library is absent.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "llama3.2:3b-instruct-q4_K_M"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT = 30.0
# Pulling a multi-gigabyte model has nothing to do with the inference budget,
# so it gets its own, much longer allowance.
DEFAULT_PULL_TIMEOUT = 1800.0

VALID_SEVERITIES = ("low", "medium", "high", "critical")

ANALYSIS_SYSTEM_PROMPT = (
    "You are an edge-computing site reliability engineer analysing logs from an "
    "Open Horizon workload. Identify the root cause of the reported error and "
    "propose concrete remediation steps.\n"
    "Respond with a single JSON object and nothing else. Use exactly these keys:\n"
    '  "error_summary": string, one sentence describing the error\n'
    '  "root_cause": string, the most likely cause\n'
    '  "severity": one of "low", "medium", "high", "critical"\n'
    '  "remediation_steps": array of strings, ordered actions to resolve it\n'
    '  "confidence": number between 0.0 and 1.0\n'
    "Do not wrap the JSON in markdown fences or add commentary."
)

ANALYSIS_PROMPT_TEMPLATE = """\
## Workload
{workload_context}
{knowledge_section}
## Error
Log file: {log_path}
Line number: {line_number}
Matched pattern: {matched_pattern}
Occurrences: {occurrences}

## Log context
```
{log_context}
```

Analyse the error above and respond with the JSON object described in your instructions."""

RETRY_SUFFIX = (
    "\n\nYour previous response could not be parsed as JSON. "
    "Respond with ONLY the raw JSON object, starting with { and ending with }."
)


class LLMError(Exception):
    """Raised when the LLM cannot produce a usable analysis."""


class LLMTimeout(LLMError):
    """Raised when inference exceeds the configured time limit."""


def _extract_json(text: str) -> Dict[str, Any]:
    """Pull a JSON object out of a model response.

    Small models often wrap output in prose or markdown fences, so fall back to
    the outermost brace-delimited span before giving up.
    """
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except ValueError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass

    raise LLMError("response did not contain a JSON object")


def validate_analysis(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalise an analysis payload from the model.

    Raises LLMError when a required field is missing or unusable so the caller
    can retry with a clarified prompt.
    """
    required = ("error_summary", "root_cause", "severity", "remediation_steps", "confidence")
    missing = [field for field in required if field not in payload]
    if missing:
        raise LLMError(f"analysis missing required field(s): {', '.join(missing)}")

    severity = str(payload["severity"]).strip().lower()
    if severity not in VALID_SEVERITIES:
        raise LLMError(f"invalid severity {payload['severity']!r}")

    steps = payload["remediation_steps"]
    if isinstance(steps, str):
        steps = [steps]
    if not isinstance(steps, list) or not steps:
        raise LLMError("remediation_steps must be a non-empty list")

    try:
        confidence = float(payload["confidence"])
    except (TypeError, ValueError):
        raise LLMError(f"confidence {payload['confidence']!r} is not a number")
    # Clamp rather than reject: models routinely emit 1.2 or -0.1.
    confidence = max(0.0, min(1.0, confidence))

    return {
        "error_summary": str(payload["error_summary"]).strip(),
        "root_cause": str(payload["root_cause"]).strip(),
        "severity": severity,
        "remediation_steps": [str(s).strip() for s in steps if str(s).strip()],
        "confidence": confidence,
    }


KNOWLEDGE_HEADER = (
    "\n## Service knowledge\n"
    "The following is operator-supplied documentation about this deployment "
    "(runbooks, architecture notes, service guides). Prefer it over general "
    "assumptions, and reference specific files, commands, and dependencies it "
    "names.\n\n"
)


def build_analysis_prompt(
    error_event,
    workload_metadata: Dict[str, Any],
    occurrences: int = 1,
    knowledge: str = "",
) -> str:
    """Render the analysis prompt for one error.

    ``knowledge`` is domain guidance (SKILL.md files, runbooks, wiki exports)
    assembled by ContextLibrary. It is placed before the error so the model
    reads the domain rules before the evidence.
    """
    workload_context = "\n".join(
        f"- {key}: {value}" for key, value in sorted(workload_metadata.items())
    )
    knowledge_section = f"{KNOWLEDGE_HEADER}{knowledge.strip()}\n" if knowledge.strip() else ""
    return ANALYSIS_PROMPT_TEMPLATE.format(
        workload_context=workload_context or "- (no metadata available)",
        knowledge_section=knowledge_section,
        log_path=error_event.log_path,
        line_number=error_event.line_number,
        matched_pattern=error_event.matched_pattern,
        occurrences=occurrences,
        log_context=error_event.context_text(),
    )


class OllamaClient:
    """Thin wrapper around the Ollama chat API with JSON validation."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: float = DEFAULT_TIMEOUT,
        client: Optional[Any] = None,
        max_retries: int = 2,
        pull_timeout: float = DEFAULT_PULL_TIMEOUT,
    ):
        self.model = model
        self.host = host
        self.timeout = timeout
        self.max_retries = max_retries
        self.pull_timeout = pull_timeout
        self._client = client
        self._injected = client is not None

    @property
    def client(self) -> Any:
        """Lazily construct the underlying Ollama client.

        Prefers the official ``ollama`` package; falls back to a small
        requests-based transport so the service still runs on images where the
        optional package is unavailable.
        """
        if self._client is None:
            try:
                import ollama

                self._client = ollama.Client(host=self.host, timeout=self.timeout)
            except ImportError:
                logger.info(
                    "the 'ollama' package is not installed; using the HTTP API directly"
                )
                self._client = _HttpOllama(self.host, self.timeout)
        return self._client

    def ensure_model(self) -> bool:
        """Make sure the configured model is present locally, pulling if not."""
        try:
            listed = self.client.list()
        except Exception as exc:
            logger.error("could not list Ollama models: %s", exc)
            return False

        models = listed.get("models", []) if isinstance(listed, dict) else []
        names = {m.get("name") or m.get("model") for m in models}
        if self.model in names:
            logger.info("model %s is available", self.model)
            return True

        logger.info(
            "model %s not found locally; pulling (timeout %.0fs)",
            self.model,
            self.pull_timeout,
        )
        try:
            # A pull must not inherit the short inference timeout, so it runs on
            # a client of its own.
            self._pull_client().pull(self.model)
            return True
        except Exception as exc:
            logger.error("failed to pull model %s: %s", self.model, exc)
            return False

    def _pull_client(self) -> Any:
        """A client whose timeout is sized for downloading a model."""
        if self._injected:
            # Caller supplied the client; respect it rather than building one.
            return self._client
        if isinstance(self.client, _HttpOllama):
            return _HttpOllama(self.host, self.pull_timeout)
        import ollama

        return ollama.Client(host=self.host, timeout=self.pull_timeout)

    def is_available(self) -> bool:
        """Return True when the Ollama runtime answers a list request."""
        try:
            self.client.list()
            return True
        except Exception as exc:
            logger.warning("Ollama runtime unavailable: %s", exc)
            return False

    def _chat(self, prompt: str) -> str:
        """Send one chat completion request and return the raw text."""
        try:
            response = self.client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                # Ask Ollama itself to constrain output to JSON where supported.
                format="json",
                options={"temperature": 0.2},
            )
        except Exception as exc:
            if _is_timeout(exc):
                raise LLMTimeout(
                    f"LLM analysis exceeded {self.timeout}s limit"
                ) from exc
            raise LLMError(f"LLM request failed: {exc}") from exc

        content = _response_content(response)
        if not content:
            raise LLMError("LLM returned an empty response")
        return content

    def analyze(
        self,
        error_event,
        workload_metadata: Dict[str, Any],
        occurrences: int = 1,
        knowledge: str = "",
    ) -> Dict[str, Any]:
        """Analyse an error and return a validated analysis payload.

        A malformed response is retried once with a clarified prompt, per the
        error-analysis spec.
        """
        prompt = build_analysis_prompt(
            error_event, workload_metadata, occurrences, knowledge
        )
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                raw = self._chat(prompt if attempt == 0 else prompt + RETRY_SUFFIX)
                return validate_analysis(_extract_json(raw))
            except LLMTimeout:
                # Timeouts are not a prompting problem; surface immediately.
                raise
            except LLMError as exc:
                last_error = exc
                logger.warning(
                    "LLM analysis attempt %d/%d produced an unusable response: %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )

        raise LLMError(f"LLM analysis failed after {self.max_retries} attempts: {last_error}")


class _HttpOllama:
    """Minimal Ollama REST client mirroring the ollama package's surface."""

    def __init__(self, host: str, timeout: float):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        import requests

        response = requests.post(
            f"{self.host}{path}", json=payload, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def chat(self, model: str, messages: list, **kwargs: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            # The monitor consumes one complete JSON document, not a token stream.
            "stream": False,
        }
        if "format" in kwargs:
            payload["format"] = kwargs["format"]
        if "options" in kwargs:
            payload["options"] = kwargs["options"]
        return self._post("/api/chat", payload)

    def list(self) -> Dict[str, Any]:
        import requests

        response = requests.get(f"{self.host}/api/tags", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def pull(self, model: str) -> Dict[str, Any]:
        # Constructed with the caller's pull timeout, not the inference one.
        import requests

        response = requests.post(
            f"{self.host}/api/pull",
            json={"model": model, "stream": False},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


def _response_content(response: Any) -> str:
    """Pull the assistant text out of a chat response.

    The REST API returns plain dicts, while the ``ollama`` package returns a
    ``ChatResponse`` model, so both access styles must work.
    """
    message = (
        response.get("message")
        if isinstance(response, dict)
        else getattr(response, "message", None)
    )
    if message is None:
        return ""
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", None)
    )
    return content or ""


def _is_timeout(exc: Exception) -> bool:
    """Best-effort detection of timeout errors across client versions."""
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in str(exc).lower() or "timed out" in str(exc).lower()
