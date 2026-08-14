"""Issue sinks: stateful work items in external trackers.

Unlike event sinks these carry a lifecycle. One problem gets one issue, updated
as it recurs and resolved when it stops. Deduplication lives here, in the base
class, rather than in each tracker — reimplementing it per tracker is exactly
where duplicate-ticket bugs come from.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from .event_sinks import SinkError, _scrub
from .records import AI_DISCLAIMER

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class IssueSink:
    """Base class for trackers. Subclasses implement the four REST operations."""

    kind = "issue"
    supports_reopen = True

    def __init__(
        self,
        name: str,
        timeout: float = DEFAULT_TIMEOUT,
        dry_run: bool = False,
        **_: Any,
    ):
        self.name = name
        self.timeout = timeout
        self.dry_run = dry_run
        self._secrets: list = []

    # ---------------- operations ----------------

    def create(self, title: str, body: str, record: Dict[str, Any]) -> str:
        """Create an issue and return its external reference."""
        raise NotImplementedError

    def comment(self, reference: str, body: str) -> None:
        """Add a comment to an existing issue."""
        raise NotImplementedError

    def resolve(self, reference: str, body: str) -> None:
        """Close an issue with a comment."""
        raise NotImplementedError

    def reopen(self, reference: str, body: str) -> None:
        """Reopen a closed issue."""
        raise NotImplementedError

    def is_open(self, reference: str) -> Optional[bool]:
        """True/False if known, None if the tracker cannot say."""
        return None

    def check(self) -> bool:
        return True

    def scrub(self, text: Any) -> str:
        return _scrub(text, self._secrets)

    # ---------------- content ----------------

    def build_title(self, record: Dict[str, Any]) -> str:
        service = record.get("service_name") or record.get("workload_id") or "workload"
        summary = record.get("error_summary") or "Recurring error detected"
        return f"[{record.get('severity', 'unknown')}] {service}: {summary}"[:200]

    def build_body(self, record: Dict[str, Any]) -> str:
        """Enough context for someone who cannot reach the node to act."""
        lines = [
            f"**Service:** {record.get('service_name')} {record.get('service_version') or ''}".rstrip(),
            f"**Organization:** {record.get('organization')}",
        ]
        node = record.get("node") or {}
        if node:
            lines.append(f"**Node:** {node.get('id')} ({node.get('organization', '')})".rstrip())
        lines += [
            f"**Severity:** {record.get('severity')}  |  "
            f"**Confidence:** {record.get('confidence')}",
            f"**First seen:** {record.get('first_seen')}",
            f"**Last seen:** {record.get('last_seen')}",
            f"**Occurrences:** {record.get('total_occurrences')}",
            "",
        ]

        if record.get("root_cause"):
            lines += ["## Likely cause", str(record["root_cause"]), ""]

        steps = record.get("remediation_steps") or []
        if steps:
            lines.append("## Suggested remediation")
            lines += [f"{i}. {s}" for i, s in enumerate(steps, start=1)]
            lines.append("")

        if record.get("log_path"):
            location = f"`{record['log_path']}`"
            if record.get("line_number"):
                location += f" line {record['line_number']}"
            lines += ["## Location", location, ""]

        lines += ["---", f"_{AI_DISCLAIMER}_", f"_Problem key: `{record.get('problem_key')}`_"]
        return "\n".join(lines)

    def build_update(self, record: Dict[str, Any]) -> str:
        return (
            f"Still occurring: **{record.get('total_occurrences')}** occurrence(s) "
            f"between {record.get('first_seen')} and {record.get('last_seen')}.\n\n"
            f"Current severity: {record.get('severity')} "
            f"(confidence {record.get('confidence')})."
        )

    def build_revised_analysis(self, record: Dict[str, Any]) -> str:
        steps = record.get("remediation_steps") or []
        body = [
            "Re-analysis produced a different explanation.",
            "",
            f"**Revised cause:** {record.get('root_cause')}",
        ]
        if steps:
            body += ["", "**Revised remediation:**"]
            body += [f"{i}. {s}" for i, s in enumerate(steps, start=1)]
        body += ["", f"_{AI_DISCLAIMER}_"]
        return "\n".join(body)

    def build_resolution(self, record: Dict[str, Any], window_hours: float) -> str:
        # The monitor knows recurrence stopped, not that anything was repaired.
        return (
            f"This problem has not recurred in {window_hours:g} hours "
            f"(last seen {record.get('last_seen')}), so it is being closed "
            f"automatically.\n\n"
            f"Note that this reflects the absence of further occurrences, **not** "
            f"confirmation that the underlying cause was fixed. It totalled "
            f"{record.get('total_occurrences')} occurrence(s). If it recurs, this "
            f"issue will be reopened."
        )


class GitHubIssueSink(IssueSink):
    """GitHub Issues. Reference format: the issue number as a string."""

    def __init__(
        self,
        name: str,
        repository: str,
        token: str,
        api_url: str = "https://api.github.com",
        labels: Optional[list] = None,
        timeout: float = DEFAULT_TIMEOUT,
        dry_run: bool = False,
        **_: Any,
    ):
        super().__init__(name, timeout, dry_run)
        self.repository = repository
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.labels = list(labels or ["edge-ai-monitor"])
        self._secrets = [token]

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        import requests

        try:
            response = requests.request(
                method,
                f"{self.api_url}{path}",
                headers=self._headers(),
                timeout=self.timeout,
                **kwargs,
            )
            if response.status_code == 403 and "rate limit" in response.text.lower():
                raise SinkError("GitHub rate limit reached")
            response.raise_for_status()
            return response.json() if response.content else {}
        except requests.RequestException as exc:
            raise SinkError(self.scrub(exc)) from None

    def create(self, title: str, body: str, record: Dict[str, Any]) -> str:
        payload = {"title": title, "body": body, "labels": self.labels}
        result = self._request("POST", f"/repos/{self.repository}/issues", json=payload)
        return str(result.get("number", ""))

    def comment(self, reference: str, body: str) -> None:
        self._request(
            "POST",
            f"/repos/{self.repository}/issues/{reference}/comments",
            json={"body": body},
        )

    def resolve(self, reference: str, body: str) -> None:
        self.comment(reference, body)
        self._request(
            "PATCH",
            f"/repos/{self.repository}/issues/{reference}",
            json={"state": "closed", "state_reason": "completed"},
        )

    def reopen(self, reference: str, body: str) -> None:
        self._request(
            "PATCH",
            f"/repos/{self.repository}/issues/{reference}",
            json={"state": "open"},
        )
        self.comment(reference, body)

    def is_open(self, reference: str) -> Optional[bool]:
        try:
            result = self._request("GET", f"/repos/{self.repository}/issues/{reference}")
        except SinkError:
            return None
        return result.get("state") == "open"

    def check(self) -> bool:
        try:
            self._request("GET", f"/repos/{self.repository}")
            return True
        except SinkError as exc:
            logger.error("GitHub sink %s unreachable: %s", self.name, exc)
            return False


class JiraIssueSink(IssueSink):
    """Jira Cloud. Reference format: the issue key, e.g. OPS-1234."""

    def __init__(
        self,
        name: str,
        base_url: str,
        project: str,
        email: str,
        token: str,
        issue_type: str = "Bug",
        resolve_transition: str = "Done",
        reopen_transition: str = "Reopen",
        timeout: float = DEFAULT_TIMEOUT,
        dry_run: bool = False,
        **_: Any,
    ):
        super().__init__(name, timeout, dry_run)
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.email = email
        self.token = token
        self.issue_type = issue_type
        self.resolve_transition = resolve_transition
        self.reopen_transition = reopen_transition
        self._secrets = [token]

    def _auth(self) -> Tuple[str, str]:
        return (self.email, self.token)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        import requests

        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                auth=self._auth(),
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
                **kwargs,
            )
            if response.status_code == 429:
                raise SinkError("Jira rate limit reached")
            response.raise_for_status()
            return response.json() if response.content else {}
        except requests.RequestException as exc:
            raise SinkError(self.scrub(exc)) from None

    def create(self, title: str, body: str, record: Dict[str, Any]) -> str:
        payload = {
            "fields": {
                "project": {"key": self.project},
                "summary": title,
                "description": body,
                "issuetype": {"name": self.issue_type},
            }
        }
        result = self._request("POST", "/rest/api/2/issue", json=payload)
        return str(result.get("key", ""))

    def comment(self, reference: str, body: str) -> None:
        self._request("POST", f"/rest/api/2/issue/{reference}/comment", json={"body": body})

    def _transition(self, reference: str, name: str) -> None:
        transitions = self._request("GET", f"/rest/api/2/issue/{reference}/transitions")
        for transition in transitions.get("transitions", []):
            if str(transition.get("name", "")).lower() == name.lower():
                self._request(
                    "POST",
                    f"/rest/api/2/issue/{reference}/transitions",
                    json={"transition": {"id": transition["id"]}},
                )
                return
        raise SinkError(f"no transition named {name!r} available on {reference}")

    def resolve(self, reference: str, body: str) -> None:
        self.comment(reference, body)
        self._transition(reference, self.resolve_transition)

    def reopen(self, reference: str, body: str) -> None:
        self._transition(reference, self.reopen_transition)
        self.comment(reference, body)

    def check(self) -> bool:
        try:
            self._request("GET", f"/rest/api/2/project/{self.project}")
            return True
        except SinkError as exc:
            logger.error("Jira sink %s unreachable: %s", self.name, exc)
            return False


class ServiceNowIssueSink(IssueSink):
    """ServiceNow table API. Reference format: the record sys_id."""

    # ServiceNow incidents are typically closed rather than reopened.
    supports_reopen = False

    def __init__(
        self,
        name: str,
        instance_url: str,
        username: str,
        password: str,
        table: str = "incident",
        timeout: float = DEFAULT_TIMEOUT,
        dry_run: bool = False,
        **_: Any,
    ):
        super().__init__(name, timeout, dry_run)
        self.instance_url = instance_url.rstrip("/")
        self.username = username
        self.password = password
        self.table = table
        self._secrets = [password]

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        import requests

        try:
            response = requests.request(
                method,
                f"{self.instance_url}{path}",
                auth=(self.username, self.password),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=self.timeout,
                **kwargs,
            )
            response.raise_for_status()
            return response.json() if response.content else {}
        except requests.RequestException as exc:
            raise SinkError(self.scrub(exc)) from None

    @staticmethod
    def _urgency(severity: str) -> str:
        return {"critical": "1", "high": "2", "medium": "3", "low": "3"}.get(severity, "3")

    def create(self, title: str, body: str, record: Dict[str, Any]) -> str:
        payload = {
            "short_description": title,
            "description": body,
            "urgency": self._urgency(str(record.get("severity", "medium"))),
            "correlation_id": str(record.get("problem_key", "")),
        }
        result = self._request("POST", f"/api/now/table/{self.table}", json=payload)
        return str(result.get("result", {}).get("sys_id", ""))

    def comment(self, reference: str, body: str) -> None:
        self._request(
            "PATCH", f"/api/now/table/{self.table}/{reference}", json={"work_notes": body}
        )

    def resolve(self, reference: str, body: str) -> None:
        self._request(
            "PATCH",
            f"/api/now/table/{self.table}/{reference}",
            json={"state": "6", "close_notes": body},
        )

    def reopen(self, reference: str, body: str) -> None:
        raise SinkError("ServiceNow records are not reopened; a new record is created")

    def check(self) -> bool:
        try:
            self._request("GET", f"/api/now/table/{self.table}?sysparm_limit=1")
            return True
        except SinkError as exc:
            logger.error("ServiceNow sink %s unreachable: %s", self.name, exc)
            return False


ISSUE_SINK_TYPES = {
    "github": GitHubIssueSink,
    "jira": JiraIssueSink,
    "servicenow": ServiceNowIssueSink,
}


def build_issue_sink(name: str, settings: Dict[str, Any]) -> IssueSink:
    """Construct an issue sink from its configuration block."""
    kind = str(settings.get("type", "")).lower()
    if kind not in ISSUE_SINK_TYPES:
        raise ValueError(
            f"unknown issue sink type {kind!r}; expected one of "
            f"{', '.join(sorted(ISSUE_SINK_TYPES))}"
        )
    options = {
        k: v
        for k, v in settings.items()
        if k not in ("type", "level", "enabled", "min_severity", "update_interval", "resolve_after")
    }
    return ISSUE_SINK_TYPES[kind](name=name, **options)
