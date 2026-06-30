"""Unit checks for ThreadKeeper subagent hardening primitives."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import subagent  # noqa: E402


def test_llm_retry_backoff_returns_success_after_transient_failure(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(subagent, "_SUBAGENT_LLM_RETRIES", 1)
    monkeypatch.setattr(subagent, "_SUBAGENT_LLM_BACKOFF_S", 0.0)

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("slow worker")
        return '(emit "ok")'

    assert subagent._call_with_retries(flaky, "unit") == '(emit "ok")'
    assert calls["n"] == 2


def test_llm_retry_backoff_returns_structured_failure(monkeypatch):
    monkeypatch.setattr(subagent, "_SUBAGENT_LLM_RETRIES", 1)
    monkeypatch.setattr(subagent, "_SUBAGENT_LLM_BACKOFF_S", 0.0)

    def always_fails():
        raise TimeoutError("still slow")

    result = subagent._call_with_retries(always_fails, "unit")
    assert result.startswith("(subagent LLM call failed after 2 attempt(s) via unit")
    assert "TimeoutError" in result


def test_run_tools_rejects_bad_arg_counts_before_dispatch():
    result = subagent.run_tools([("write-file", ["only-path"])], ["write-file"])
    assert "SKILL_ARG_ERROR: write-file" in result
    assert "expected 2 arg" in result


def test_write_file_uses_atomic_replace_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "nested" / "artifact.txt"

    assert subagent._tool_write_file("nested/artifact.txt", "first") == "WRITE-FILE-SUCCESS"
    assert target.read_text() == "first"
    assert subagent._tool_write_file("nested/artifact.txt", "second") == "WRITE-FILE-SUCCESS"
    assert target.read_text() == "second"
    assert not list((tmp_path / "nested").glob(".*.tmp"))
