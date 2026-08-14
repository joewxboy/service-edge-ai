"""Domain knowledge injected into analysis prompts.

The LLM knows nothing about the services it watches. Left with only log lines it
guesses generically ("check the database is running"). Given a runbook, a
SKILL.md, or an architecture note it can name the actual dependency, the actual
config file, and the actual recovery procedure.

Two sources feed the prompt:

* **Node-wide context** — files in the context directory, applied to every
  analysis. Owned by the node operator.
* **Per-workload context** — files named by a workload's
  ``MONITORING_CONTEXT_PATHS`` variable, applied only to that workload. Owned by
  the service developer.

Both are read from disk at analysis time, so editing a runbook takes effect on
the next error without restarting the service.
"""

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_DIR = "/etc/edge-ai-monitor/context"

# A 3B model has a small context window, and every character of guidance
# competes with the log lines that describe the actual error. These ceilings
# keep guidance useful without crowding out the evidence.
DEFAULT_MAX_TOTAL_BYTES = 8000
DEFAULT_MAX_FILE_BYTES = 4000

CONTEXT_EXTENSIONS = (".md", ".txt", ".markdown")

TRUNCATION_NOTICE = "\n\n[... truncated: file exceeds the per-file context budget ...]"


@dataclass
class ContextDocument:
    """One piece of guidance destined for the prompt."""

    name: str
    content: str
    source: str  # "node" or "workload"

    def rendered(self) -> str:
        return f"### {self.name}\n{self.content.strip()}"


def _read_text(path: Path, max_bytes: int) -> Optional[str]:
    """Read a context file, truncating it at the per-file budget."""
    try:
        # Read one byte past the budget so oversize files are detectable.
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_bytes + 1)
    except OSError as exc:
        logger.warning("could not read context file %s: %s", path, exc)
        return None

    if not text.strip():
        return None
    if len(text) > max_bytes:
        logger.info(
            "context file %s exceeds %d bytes; truncating for the prompt",
            path,
            max_bytes,
        )
        text = text[:max_bytes] + TRUNCATION_NOTICE
    return text


class ContextLibrary:
    """Collects node-wide and per-workload guidance for prompt injection."""

    def __init__(
        self,
        context_dir: str = DEFAULT_CONTEXT_DIR,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ):
        self.context_dir = Path(context_dir)
        self.max_total_bytes = max_total_bytes
        self.max_file_bytes = max_file_bytes
        self._lock = threading.RLock()
        # path -> (mtime, size, content); avoids re-reading unchanged files.
        self._cache: Dict[str, Tuple[float, int, Optional[str]]] = {}

    # ---------------- loading ----------------

    def _load_file(self, path: Path, source: str) -> Optional[ContextDocument]:
        """Read one file, serving from cache when it has not changed."""
        try:
            stat = os.stat(path)
        except OSError as exc:
            logger.warning("context file %s is not accessible: %s", path, exc)
            return None

        key = str(path)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                content = cached[2]
            else:
                content = _read_text(path, self.max_file_bytes)
                self._cache[key] = (stat.st_mtime, stat.st_size, content)

        if content is None:
            return None
        return ContextDocument(name=path.name, content=content, source=source)

    def node_documents(self) -> List[ContextDocument]:
        """Guidance applied to every analysis, from the context directory."""
        if not self.context_dir.is_dir():
            return []

        documents = []
        # Sorted so operators can order files by name (00-overview.md, ...).
        for path in sorted(self.context_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in CONTEXT_EXTENSIONS:
                continue
            document = self._load_file(path, "node")
            if document is not None:
                documents.append(document)
        return documents

    def workload_documents(self, paths: List[str]) -> List[ContextDocument]:
        """Guidance for one workload, from its declared context paths."""
        documents = []
        for raw in paths:
            document = self._load_file(Path(raw), "workload")
            if document is not None:
                documents.append(document)
        return documents

    # ---------------- prompt rendering ----------------

    def build_context_block(self, workload_paths: Optional[List[str]] = None) -> str:
        """Render the guidance section for a prompt, within the total budget.

        Workload-specific documents come first: they are the most relevant to
        the error being analysed, so they survive when the budget is tight.
        """
        documents = self.workload_documents(workload_paths or [])
        documents.extend(self.node_documents())
        if not documents:
            return ""

        blocks: List[str] = []
        used = 0
        for document in documents:
            rendered = document.rendered()
            if used + len(rendered) > self.max_total_bytes:
                logger.info(
                    "context budget (%d bytes) reached; omitting %s and any further documents",
                    self.max_total_bytes,
                    document.name,
                )
                break
            blocks.append(rendered)
            used += len(rendered)

        return "\n\n".join(blocks)

    def describe(self) -> Dict[str, object]:
        """Summary for the health endpoint and startup logging."""
        documents = self.node_documents()
        return {
            "context_dir": str(self.context_dir),
            "context_dir_exists": self.context_dir.is_dir(),
            "node_context_documents": len(documents),
            "node_context_bytes": sum(len(d.content) for d in documents),
        }
