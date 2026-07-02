"""Unit checks for ThreadKeeper subagent hardening primitives."""
import builtins
import hashlib
import importlib
import json
import multiprocessing
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import subagent  # noqa: E402


def test_env_numeric_knobs_fallback_and_clamp_on_reload(monkeypatch):
    bad_values = {
        "OMEGACLAW_SUBAGENT_MAX_TURNS": "not-int",
        "OMEGACLAW_SUBAGENT_MAX_DIGEST_CHARS": "1",
        "OMEGACLAW_SUBAGENT_HISTORY_MAX_TURNS": "0",
        "OMEGACLAW_SUBAGENT_LLM_TIMEOUT_S": "-5",
        "OMEGACLAW_SUBAGENT_LLM_RETRIES": "-1",
        "OMEGACLAW_SUBAGENT_LLM_BACKOFF_S": "bad-float",
        "OMEGACLAW_SUBAGENT_LLM_CALLS_PER_MINUTE": "-7",
        "OMEGACLAW_SUBAGENT_MAX_CONCURRENT_LLM_CALLS": "-8",
        "OMEGACLAW_SUBAGENT_MAX_TOOL_CALLS": "-9",
        "OMEGACLAW_SUBAGENT_MAX_PATH_ARG_CHARS": "0",
        "OMEGACLAW_SUBAGENT_MAX_TOOL_ARG_CHARS": "0",
        "OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEMS": "-2",
        "OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEM_CHARS": "0",
        "OMEGACLAW_SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS": "0",
    }
    for name, value in bad_values.items():
        monkeypatch.setenv(name, value)

    reloaded = importlib.reload(subagent)
    assert reloaded.SUBAGENT_MAX_TURNS_HARD_CAP == 8
    assert reloaded.SUBAGENT_MAX_DIGEST_CHARS == 100
    assert reloaded._SUBAGENT_HISTORY_MAX_TURNS == 1
    assert reloaded._SUBAGENT_LLM_TIMEOUT_S == 1
    assert reloaded._SUBAGENT_LLM_RETRIES == 0
    assert reloaded._SUBAGENT_LLM_BACKOFF_S == 1.0
    assert reloaded._SUBAGENT_LLM_CALLS_PER_MINUTE == 0
    assert reloaded._SUBAGENT_MAX_CONCURRENT_LLM_CALLS == 0
    assert reloaded._SUBAGENT_MAX_TOOL_CALLS == 0
    assert reloaded._SUBAGENT_MAX_PATH_ARG_CHARS == 1
    assert reloaded._SUBAGENT_MAX_TOOL_ARG_CHARS == 1
    assert reloaded._SUBAGENT_MAX_CONTRACT_ITEMS == 0
    assert reloaded._SUBAGENT_MAX_CONTRACT_ITEM_CHARS == 1
    assert reloaded._SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS == 1

    monkeypatch.undo()
    importlib.reload(subagent)


def _append_many_worker(workspace, worker_id, count):
    os.environ["OMEGACLAW_SUBAGENT_WORKSPACE"] = str(workspace)
    for i in range(count):
        result = subagent._tool_append_file("shared/log.txt", f"{worker_id}-{i}")
        if result != "APPEND-FILE-SUCCESS":
            raise RuntimeError(result)


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


def test_llm_calls_per_minute_rate_limit_is_atomic_state(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(subagent, "_SUBAGENT_LLM_CALLS_PER_MINUTE", 1)
    monkeypatch.setattr(subagent, "_SUBAGENT_LLM_RETRIES", 0)

    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return '(emit "ok")'

    assert subagent._call_with_retries(counted, "unit-rate") == '(emit "ok")'
    blocked = subagent._call_with_retries(counted, "unit-rate")
    assert blocked.startswith("(subagent LLM call rate-limited via unit-rate")
    assert calls["n"] == 1


def test_llm_concurrency_limit_blocks_when_endpoint_slots_are_full(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_CONCURRENT_LLM_CALLS", 1)
    monkeypatch.setattr(subagent, "_SUBAGENT_LLM_CALLS_PER_MINUTE", 0)
    monkeypatch.setattr(subagent, "_SUBAGENT_LLM_RETRIES", 0)

    ok, _reason, token = subagent._subagent_llm_concurrency_acquire("unit-concurrency")
    assert ok
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return '(emit "ok")'

    blocked = subagent._call_with_retries(counted, "unit-concurrency")
    assert blocked.startswith("(subagent LLM call concurrency-limited via unit-concurrency")
    assert calls["n"] == 0
    subagent._subagent_llm_concurrency_release("unit-concurrency", token)
    assert subagent._call_with_retries(counted, "unit-concurrency") == '(emit "ok")'
    assert calls["n"] == 1


def test_run_tools_rejects_bad_arg_counts_before_dispatch():
    result = subagent.run_tools([("write-file", ["only-path"])], ["write-file"])
    assert "SKILL_ARG_ERROR: write-file" in result
    assert "expected 2 arg" in result


def test_run_tools_rejects_oversized_tool_arguments(monkeypatch):
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_PATH_ARG_CHARS", 8)
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_TOOL_ARG_CHARS", 12)

    long_path = "a" * 9
    long_content = "b" * 13
    path_result = subagent.run_tools([("write-file", [long_path, "ok"])], ["write-file"])
    content_result = subagent.run_tools([("write-file", ["ok.txt", long_content])], ["write-file"])

    assert "SKILL_ARG_ERROR: write-file" in path_result
    assert "path argument exceeds" in path_result
    assert "SKILL_ARG_ERROR: write-file" in content_result
    assert "argument(s) [2] exceed" in content_result


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


def test_append_file_lock_prevents_concurrent_lost_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    worker_count = 4
    per_worker = 12
    procs = [
        multiprocessing.Process(target=_append_many_worker, args=(str(tmp_path), w, per_worker))
        for w in range(worker_count)
    ]

    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(10)

    assert all(proc.exitcode == 0 for proc in procs)
    lines = (tmp_path / "shared" / "log.txt").read_text().splitlines()
    assert len(lines) == worker_count * per_worker
    assert set(lines) == {f"{w}-{i}" for w in range(worker_count) for i in range(per_worker)}
    assert not list((tmp_path / "shared").glob(".*.tmp"))


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
        "endpoint_kind": "ollama_native" if node_role == "local" else "openai_compatible",
        "default_tool_subset": ["write-file"],
    }))
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(persona_dir))
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("UNIT_API_KEY", "dummy")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path / "workspace"))
    return persona_dir


def test_persona_config_requires_explicit_node_role(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "unit.txt").write_text("You are a unit-test subagent.")
    (persona_dir / "unit.json").write_text(json.dumps({
        "persona_file": "unit.txt",
        "provider": "ollama",
        "model": "unit-model",
        "api_key_env": "UNIT_API_KEY",
        "base_url": "http://localhost:11434",
    }))
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(persona_dir))

    try:
        subagent.load_persona_config("unit")
        assert False, "missing node_role should be rejected"
    except ValueError as e:
        assert "node_role" in str(e)


def test_persona_key_rejects_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(tmp_path))

    try:
        subagent.load_persona_config("../secrets")
        assert False, "path-like persona key should be rejected"
    except ValueError as e:
        assert "persona key" in str(e)


def test_persona_prompt_sha256_pin_fails_closed(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    prompt = persona_dir / "unit.txt"
    prompt.write_text("trusted prompt")
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(persona_dir))

    good_hash = hashlib.sha256(b"trusted prompt").hexdigest()
    assert subagent.load_persona_prompt("unit.txt", "unit", good_hash) == "trusted prompt"
    assert subagent.load_persona_prompt(str(prompt), "unit", good_hash) == "trusted prompt"
    try:
        subagent.load_persona_prompt("unit.txt", "unit", "0" * 64)
        assert False, "persona hash mismatch should fail closed"
    except ValueError as e:
        assert "sha256" in str(e)


def test_persona_prompt_rejects_path_escape(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(persona_dir))

    for path in ("../outside.txt", str(outside)):
        try:
            subagent.load_persona_prompt(path, "unit")
            assert False, f"persona prompt escape should be rejected: {path}"
        except ValueError as e:
            assert "escapes persona directory" in str(e)


def test_committed_persona_examples_use_explicit_metadata_and_valid_prompt_pin(monkeypatch):
    persona_dir = ROOT / "memory" / "personas-subagent"
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(persona_dir))

    examples = sorted(persona_dir.glob("*.json.example"))
    assert examples, "expected committed persona examples"
    for example in examples:
        key = example.name.removesuffix(".json.example")
        cfg = json.loads(example.read_text())
        assert cfg.get("node_role") in (subagent._LOCAL_NODE_ROLES | subagent._CLOUD_NODE_ROLES)
        assert subagent._endpoint_kind(cfg) in {"ollama_native", "openai_compatible"}
        assert cfg.get("persona_sha256"), f"{example.name} should pin its example prompt"
        assert subagent.load_persona_prompt(
            cfg["persona_file"], key, cfg["persona_sha256"]
        )


def test_openai_compatible_provider_init_fails_closed_without_client(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch, node_role="cloud")
    monkeypatch.setattr(subagent, "_escalation_gate", lambda _cfg: (True, "unit budget ok"))

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("openai sdk unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    payload = json.loads(subagent.dispatch("cloud setup", "write-file", "unit", max_turns=1))

    assert payload["status"] == "error"
    assert "OpenAI-compatible provider" in payload["summary"]
    assert "openai sdk unavailable" in payload["summary"]
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "provider_invalid"
    assert saved["turns"] == []


def test_endpoint_kind_controls_llm_transport_without_base_url_heuristic(monkeypatch):
    seen = {}

    class FakeClient:
        class Chat:
            class Completions:
                def create(self, **kwargs):
                    seen["called"] = kwargs
                    return type("Resp", (), {
                        "usage": None,
                        "choices": [type("Choice", (), {
                            "message": type("Msg", (), {"content": '(emit "cloud")'})()
                        })()],
                    })()
            completions = Completions()
        chat = Chat()

    handle = {
        "provider": FakeClient(),
        "model": "unit-model",
        "base_url": "http://localhost:11434/v1",  # intentionally misleading
        "endpoint_kind": "openai_compatible",
    }
    assert subagent._call_subagent_llm(handle, "prompt", 12) == '(emit "cloud")'
    assert seen["called"]["timeout"] == subagent._SUBAGENT_LLM_TIMEOUT_S


def test_dispatch_returns_structured_digest_and_persists_transcript(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)

    responses = iter(['(write-file "out.txt" "hello")', '(emit "done")'])
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_args: next(responses))

    result = subagent.dispatch("write a file", "write-file", "unit", max_turns=3)
    payload = json.loads(result)

    assert payload["summary"] == "done"
    assert payload["files_changed"] == ["out.txt"]
    assert payload["status"] == "ok"
    assert len(payload["transcript_sha256"]) == 64
    transcript = Path(payload["transcript_path"])
    assert transcript.exists()
    digest = hashlib.sha256(transcript.read_bytes()).hexdigest()
    assert payload["transcript_sha256"] == digest
    assert transcript.with_suffix(transcript.suffix + ".sha256").read_text().startswith(digest)
    saved = json.loads(transcript.read_text())
    index_path = transcript.parent / "index.jsonl"
    assert index_path.exists()
    index_entries = [json.loads(line) for line in index_path.read_text().splitlines()]
    assert index_entries[-1]["run_id"] == saved["run_id"]
    assert index_entries[-1]["status"] == "ok"
    assert index_entries[-1]["transcript_path"] == str(transcript)
    assert index_entries[-1]["transcript_sha256"] == digest
    assert saved["status"] == "ok"
    assert len(saved["turns"]) == 2
    assert (tmp_path / "workspace" / "out.txt").read_text() == "hello"


def test_dispatch_rejects_mixed_emit_and_tool_response(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: '(emit "done")\n(write-file "hidden.txt" "nope")',
    )

    payload = json.loads(subagent.dispatch("try mixed final", "write-file", "unit", max_turns=1))

    assert payload["status"] == "error"
    assert "EMIT_PROTOCOL_VIOLATION" in payload["summary"]
    assert not (tmp_path / "workspace" / "hidden.txt").exists()
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "emit_protocol_violation"


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
    payload = json.loads(result)

    assert payload["status"] == "error"
    assert "escalation denied" in payload["summary"]
    assert "integrity mismatch" in payload["summary"]
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "escalation_denied"


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
    assert saved["task_contract"]["objective"] == "write only inside safe output"
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


def test_task_contract_max_tool_calls_narrows_global_quota(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_TOOL_CALLS", 8)
    contract_goal = json.dumps({
        "objective": "write one file only",
        "max_tool_calls": 1,
    })
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: '(write-file "a.txt" "a")\n(write-file "b.txt" "b")',
    )

    payload = json.loads(subagent.dispatch(contract_goal, "write-file", "unit", max_turns=2))

    assert payload["status"] == "error"
    assert "QUOTA_EXCEEDED" in payload["summary"]
    assert payload["files_changed"] == ["a.txt"]
    assert (tmp_path / "workspace" / "a.txt").read_text() == "a"
    assert not (tmp_path / "workspace" / "b.txt").exists()
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["task_contract"]["max_tool_calls"] == 1


def test_task_contract_rejects_bad_max_tool_calls_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    negative = subagent.dispatch(json.dumps({
        "objective": "bad quota",
        "max_tool_calls": -1,
    }), "write-file", "unit", max_turns=1)
    fractional = subagent.dispatch(json.dumps({
        "objective": "bad quota",
        "max_tool_calls": 1.5,
    }), "write-file", "unit", max_turns=1)

    assert "subagent error" in negative
    assert "max_tool_calls" in negative
    assert "non-negative" in negative
    assert "subagent error" in fractional
    assert "max_tool_calls" in fractional
    assert "not an integer" in fractional


def test_task_contract_rejects_allowed_path_escape_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    contract_goal = json.dumps({
        "objective": "escape attempt",
        "allowed_paths": ["../outside"],
    })
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = subagent.dispatch(contract_goal, "write-file", "unit", max_turns=1)
    payload = json.loads(result)

    assert payload["status"] == "error"
    assert "subagent error" in payload["summary"]
    assert "allowed_paths" in payload["summary"]
    assert "outside workspace" in payload["summary"]
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "contract_invalid"
    assert saved["task_contract"]["allowed_paths"] == ["../outside"]


def test_task_contract_rejects_oversized_contract_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_CONTRACT_ITEMS", 1)
    contract_goal = json.dumps({
        "objective": "too broad",
        "done_criteria": ["one", "two"],
    })
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = subagent.dispatch(contract_goal, "write-file", "unit", max_turns=1)

    assert "subagent error" in result
    assert "done_criteria" in result
    assert "max 1" in result


def test_task_contract_rejects_unsafe_forbidden_action_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    contract_goal = json.dumps({
        "objective": "unsafe action name",
        "forbidden_actions": ["../write-file"],
    })
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = subagent.dispatch(contract_goal, "write-file", "unit", max_turns=1)

    assert "subagent error" in result
    assert "forbidden_actions" in result
    assert "safe action identifier" in result


def test_task_contract_rejects_oversized_objective_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS", 8)
    contract_goal = json.dumps({
        "objective": "x" * 9,
    })
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = subagent.dispatch(contract_goal, "write-file", "unit", max_turns=1)

    assert "subagent error" in result
    assert "objective exceeds 8 characters" in result


def test_dispatch_without_tool_subset_or_default_persists_structured_error(tmp_path, monkeypatch):
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
        "endpoint_kind": "ollama_native",
    }))
    monkeypatch.setattr(subagent, "PERSONA_DIR", str(persona_dir))
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("UNIT_API_KEY", "dummy")
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    payload = json.loads(subagent.dispatch("needs tools", "", "unit", max_turns=1))

    assert payload["status"] == "error"
    assert "no tool subset" in payload["summary"]
    transcript = Path(payload["transcript_path"])
    assert transcript.exists()
    saved = json.loads(transcript.read_text())
    assert saved["status"] == "tool_subset_invalid"
    assert saved["turns"] == []
