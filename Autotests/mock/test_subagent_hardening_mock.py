"""Unit checks for ThreadKeeper subagent hardening primitives."""
import json
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


def test_history_is_bounded_and_evicted_turns_are_digested(monkeypatch):
    monkeypatch.setattr(subagent, "_SUBAGENT_HISTORY_MAX_TURNS", 2)
    history = []
    digest = []

    for i in range(4):
        subagent._append_bounded_history(history, (i + 1, f"raw-{i}", f"res-{i}"), digest)

    assert [entry[0] for entry in history] == [3, 4]
    assert len(digest) == 2
    assert digest[0].startswith("turn 1:")
    assert digest[1].startswith("turn 2:")


def test_dispatch_returns_structured_digest_and_persists_transcript(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "unit.txt").write_text("You are a unit-test subagent.")
    (persona_dir / "unit.json").write_text(json.dumps({
        "persona_file": "unit.txt",
        "provider": "ollama",
        "model": "unit-model",
        "api_key_env": "UNIT_API_KEY",
        "base_url": "http://localhost:11434",
        "node_role": "local",
        "default_tool_subset": ["write-file"],
    }))
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(persona_dir))
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("UNIT_API_KEY", "dummy")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path / "workspace"))

    responses = iter(['(write-file "out.txt" "hello")', '(emit "done")'])
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_args: next(responses))

    result = subagent.dispatch("write a file", "write-file", "unit", max_turns=3)
    payload = json.loads(result)

    assert payload["summary"] == "done"
    assert payload["files_changed"] == ["out.txt"]
    assert payload["status"] == "ok"
    transcript = Path(payload["transcript_path"])
    assert transcript.exists()
    saved = json.loads(transcript.read_text())
    assert saved["status"] == "ok"
    assert len(saved["turns"]) == 2
    assert (tmp_path / "workspace" / "out.txt").read_text() == "hello"
