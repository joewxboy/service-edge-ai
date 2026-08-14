"""Unit tests for domain-knowledge injection."""

import pytest

from edge_ai_monitor.context import (
    TRUNCATION_NOTICE,
    ContextLibrary,
)
from edge_ai_monitor.log_monitor import ErrorEvent
from edge_ai_monitor.ollama_client import build_analysis_prompt

METADATA = {"service_name": "sensor", "version": "1.2.0"}


def make_event():
    return ErrorEvent(
        workload_id="examples/sensor_1.2.0_amd64",
        log_path="/var/log/app.log",
        line_number=10,
        line="ERROR could not connect",
        matched_pattern="ERROR",
    )


def write(directory, name, text):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------- node-wide context ----------------


def test_missing_context_dir_is_not_an_error(tmp_path):
    library = ContextLibrary(context_dir=str(tmp_path / "absent"))
    assert library.build_context_block() == ""
    assert library.describe()["context_dir_exists"] is False


def test_markdown_files_are_loaded(tmp_path):
    write(tmp_path, "runbook.md", "Restart with `systemctl restart sensor`.")
    block = ContextLibrary(context_dir=str(tmp_path)).build_context_block()
    assert "systemctl restart sensor" in block
    assert "### runbook.md" in block


def test_non_context_extensions_are_ignored(tmp_path):
    write(tmp_path, "notes.md", "included")
    write(tmp_path, "data.json", "excluded")
    write(tmp_path, "script.sh", "excluded")
    block = ContextLibrary(context_dir=str(tmp_path)).build_context_block()
    assert "included" in block
    assert "excluded" not in block


def test_files_are_ordered_by_name(tmp_path):
    write(tmp_path, "20-second.md", "SECOND")
    write(tmp_path, "10-first.md", "FIRST")
    block = ContextLibrary(context_dir=str(tmp_path)).build_context_block()
    assert block.index("FIRST") < block.index("SECOND")


def test_empty_files_are_skipped(tmp_path):
    write(tmp_path, "empty.md", "   \n\n")
    write(tmp_path, "real.md", "content")
    block = ContextLibrary(context_dir=str(tmp_path)).build_context_block()
    assert "empty.md" not in block
    assert "content" in block


def test_subdirectories_are_ignored(tmp_path):
    (tmp_path / "nested").mkdir()
    write(tmp_path / "nested", "deep.md", "nested content")
    write(tmp_path, "top.md", "top content")
    block = ContextLibrary(context_dir=str(tmp_path)).build_context_block()
    assert "top content" in block
    assert "nested content" not in block


# ---------------- per-workload context ----------------


def test_workload_paths_are_loaded(tmp_path):
    skill = write(tmp_path, "SKILL.md", "This service talks to PostgreSQL on 5432.")
    library = ContextLibrary(context_dir=str(tmp_path / "absent"))
    block = library.build_context_block([str(skill)])
    assert "PostgreSQL on 5432" in block


def test_unreadable_workload_path_is_skipped(tmp_path):
    good = write(tmp_path, "good.md", "good content")
    library = ContextLibrary(context_dir=str(tmp_path / "absent"))
    block = library.build_context_block([str(tmp_path / "missing.md"), str(good)])
    assert "good content" in block


def test_workload_context_precedes_node_context(tmp_path):
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    write(node_dir, "node.md", "NODEWIDE")
    skill = write(tmp_path, "SKILL.md", "WORKLOAD")

    library = ContextLibrary(context_dir=str(node_dir))
    block = library.build_context_block([str(skill)])
    assert block.index("WORKLOAD") < block.index("NODEWIDE")


# ---------------- budgets ----------------


def test_oversized_file_is_truncated(tmp_path):
    write(tmp_path, "big.md", "x" * 5000)
    library = ContextLibrary(context_dir=str(tmp_path), max_file_bytes=1000)
    block = library.build_context_block()
    assert TRUNCATION_NOTICE.strip() in block
    assert len(block) < 2000


def test_total_budget_stops_adding_documents(tmp_path):
    for i in range(10):
        write(tmp_path, f"{i:02d}.md", "y" * 500)
    library = ContextLibrary(
        context_dir=str(tmp_path), max_total_bytes=1200, max_file_bytes=500
    )
    block = library.build_context_block()
    assert len(block) <= 1400  # a couple of documents, not ten


def test_workload_context_survives_a_tight_budget(tmp_path):
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    for i in range(5):
        write(node_dir, f"{i}.md", "z" * 400)
    skill = write(tmp_path, "SKILL.md", "CRITICAL WORKLOAD KNOWLEDGE")

    library = ContextLibrary(context_dir=str(node_dir), max_total_bytes=600)
    block = library.build_context_block([str(skill)])
    assert "CRITICAL WORKLOAD KNOWLEDGE" in block


# ---------------- freshness ----------------


def test_edited_file_is_picked_up_without_restart(tmp_path):
    path = write(tmp_path, "runbook.md", "original guidance")
    library = ContextLibrary(context_dir=str(tmp_path))
    assert "original guidance" in library.build_context_block()

    # Change size as well as content so the mtime/size check trips reliably.
    path.write_text("updated guidance with more text", encoding="utf-8")
    assert "updated guidance" in library.build_context_block()


def test_describe_reports_loaded_documents(tmp_path):
    write(tmp_path, "a.md", "first")
    write(tmp_path, "b.md", "second")
    described = ContextLibrary(context_dir=str(tmp_path)).describe()
    assert described["node_context_documents"] == 2
    assert described["node_context_bytes"] > 0
    assert described["context_dir_exists"] is True


# ---------------- prompt integration ----------------


def test_prompt_includes_knowledge_when_supplied():
    prompt = build_analysis_prompt(
        make_event(), METADATA, knowledge="### SKILL.md\nUses PostgreSQL."
    )
    assert "Service knowledge" in prompt
    assert "Uses PostgreSQL." in prompt


def test_prompt_omits_knowledge_section_when_empty():
    prompt = build_analysis_prompt(make_event(), METADATA, knowledge="")
    assert "Service knowledge" not in prompt


def test_prompt_omits_knowledge_section_when_whitespace():
    prompt = build_analysis_prompt(make_event(), METADATA, knowledge="   \n  ")
    assert "Service knowledge" not in prompt


def test_knowledge_precedes_the_error_in_the_prompt():
    prompt = build_analysis_prompt(
        make_event(), METADATA, knowledge="DOMAIN RULES HERE"
    )
    assert prompt.index("DOMAIN RULES HERE") < prompt.index("## Error")


def test_prompt_is_unchanged_without_knowledge():
    """Existing behaviour must be untouched when no context is configured."""
    prompt = build_analysis_prompt(make_event(), METADATA)
    assert "## Workload" in prompt
    assert "## Error" in prompt
    assert "Service knowledge" not in prompt
