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


def test_append_file_uses_atomic_replace_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    target = tmp_path / "nested" / "artifact.txt"

    assert subagent._tool_append_file("nested/artifact.txt", "first") == "APPEND-FILE-SUCCESS"
    assert subagent._tool_append_file("nested/artifact.txt", "second") == "APPEND-FILE-SUCCESS"
    assert target.read_text() == "first\nsecond\n"
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


def _write_unit_persona(tmp_path, monkeypatch, node_role="local"):
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "unit.txt").write_text("You are a unit-test subagent.")
    (persona_dir / "unit.json").write_text(json.dumps({
        "persona_file": "unit.txt",
        "provider": "ollama",
        "model": "unit-model",
        "api_key_env": "UNIT_API_KEY",
        "base_url": "http://localhost:11434" if node_role == "local" else "https://example.invalid/v1",
        "node_role": node_role,
        "default_tool_subset": ["write-file"],
    }))
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(persona_dir))
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("UNIT_API_KEY", "dummy")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path / "workspace"))
    return persona_dir


def test_dispatch_returns_structured_digest_and_persists_transcript(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)

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


def test_tool_quota_stops_dispatch_with_structured_error(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_TOOL_CALLS", 1)
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_args: '(write-file "a.txt" "a")\n(write-file "b.txt" "b")')

    payload = json.loads(subagent.dispatch("write too much", "write-file", "unit", max_turns=2))

    assert payload["status"] == "error"
    assert "QUOTA_EXCEEDED" in payload["summary"]
    assert payload["files_changed"] == ["a.txt"]
    assert (tmp_path / "workspace" / "a.txt").read_text() == "a"
    assert not (tmp_path / "workspace" / "b.txt").exists()


def test_cancel_file_stops_dispatch_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    cancel = tmp_path / "cancel.token"
    cancel.write_text("stop")
    monkeypatch.setattr(subagent, "_SUBAGENT_CANCEL_FILE", str(cancel))
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")))

    payload = json.loads(subagent.dispatch("cancel me", "write-file", "unit", max_turns=2))

    assert payload["status"] == "cancelled"
    assert "cancellation token" in payload["summary"]


def test_escalation_policy_hash_mismatch_denies_cloud_dispatch(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch, node_role="cloud")
    policy = tmp_path / "escalation.metta"
    policy.write_text("trusted policy")
    monkeypatch.setenv("OMEGACLAW_ESCALATION_METTA_PATH", str(policy))
    monkeypatch.setenv("OMEGACLAW_ESCALATION_METTA_SHA256", "0" * 64)
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")))

    result = subagent.dispatch("cloud task", "write-file", "unit", max_turns=1)

    assert "escalation denied" in result
    assert "integrity mismatch" in result


def test_task_contract_limits_file_paths_and_persists_contract(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    contract_goal = json.dumps({
        "objective": "write only inside safe output",
        "allowed_paths": ["safe"],
        "done_criteria": ["safe/out.txt exists"],
    })
    responses = iter([
        '(write-file "unsafe.txt" "nope")\n(write-file "safe/out.txt" "ok")',
        '(emit "contract respected")',
    ])
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_args: next(responses))

    payload = json.loads(subagent.dispatch(contract_goal, "write-file", "unit", max_turns=3))

    assert payload["status"] == "ok"
    assert payload["files_changed"] == ["safe/out.txt"]
    assert not (tmp_path / "workspace" / "unsafe.txt").exists()
    assert (tmp_path / "workspace" / "safe" / "out.txt").read_text() == "ok"
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["goal"] == "write only inside safe output"
    assert saved["task_contract"]["allowed_paths"] == ["safe"]
    assert saved["task_contract"]["done_criteria"] == ["safe/out.txt exists"]
    assert "CONTRACT_VIOLATION" in saved["turns"][0]["tool_results"]


def test_task_contract_forbidden_action_blocks_tool(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    contract_goal = json.dumps({
        "objective": "do not modify files",
        "forbidden_actions": ["write-file"],
    })
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_args: '(write-file "out.txt" "nope")')

    payload = json.loads(subagent.dispatch(contract_goal, "write-file", "unit", max_turns=1))

    assert payload["status"] == "incomplete"
    assert payload["files_changed"] == []
    assert not (tmp_path / "workspace" / "out.txt").exists()
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert "forbidden by task contract" in saved["turns"][0]["tool_results"]
