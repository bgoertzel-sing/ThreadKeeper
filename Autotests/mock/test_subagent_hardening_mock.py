"""Unit checks for ThreadKeeper subagent hardening primitives."""
import builtins
import hashlib
import importlib
import json
import multiprocessing
import os
import subprocess
import sys
import time
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
        "OMEGACLAW_SUBAGENT_MAX_TOOL_CALLS_PER_TURN": "0",
        "OMEGACLAW_SUBAGENT_MAX_PATH_ARG_CHARS": "0",
        "OMEGACLAW_SUBAGENT_MAX_TOOL_ARG_CHARS": "0",
        "OMEGACLAW_SUBAGENT_SHELL_MAX_ARGV": "0",
        "OMEGACLAW_SUBAGENT_SHELL_OUTPUT_CAP": "0",
        "OMEGACLAW_SUBAGENT_SHELL_TIMEOUT_S": "bad-float",
        "OMEGACLAW_SUBAGENT_MAX_READ_FILE_CHARS": "0",
        "OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEMS": "-2",
        "OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEM_CHARS": "0",
        "OMEGACLAW_SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS": "0",
        "OMEGACLAW_SUBAGENT_MAX_QUEUED_DISPATCHES": "-4",
        "OMEGACLAW_SUBAGENT_ASYNC_WORKER_MAX_TASKS": "-4",
        "OMEGACLAW_SUBAGENT_ASYNC_WORKER_MAX_IDLE_POLLS": "-1",
        "OMEGACLAW_SUBAGENT_ASYNC_WORKER_POLL_INTERVAL_S": "bad-float",
        "OMEGACLAW_SUBAGENT_ASYNC_WORKER_MAX_RUNTIME_S": "bad-float",
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
    assert reloaded._SUBAGENT_MAX_TOOL_CALLS_PER_TURN == 1
    assert reloaded._SUBAGENT_MAX_PATH_ARG_CHARS == 1
    assert reloaded._SUBAGENT_MAX_TOOL_ARG_CHARS == 1
    assert reloaded._SHELL_MAX_ARGV == 1
    assert reloaded._SHELL_OUTPUT_CAP == 1
    assert reloaded._SHELL_TIMEOUT_S == 30.0
    assert reloaded._SUBAGENT_MAX_READ_FILE_CHARS == 1
    assert reloaded._SUBAGENT_MAX_CONTRACT_ITEMS == 0
    assert reloaded._SUBAGENT_MAX_CONTRACT_ITEM_CHARS == 1
    assert reloaded._SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS == 1
    assert reloaded._SUBAGENT_MAX_QUEUED_DISPATCHES == 0
    assert reloaded._SUBAGENT_ASYNC_WORKER_MAX_TASKS == 0
    assert reloaded._SUBAGENT_ASYNC_WORKER_MAX_IDLE_POLLS == 0
    assert reloaded._SUBAGENT_ASYNC_WORKER_POLL_INTERVAL_S == 2.0
    assert reloaded._SUBAGENT_ASYNC_WORKER_MAX_RUNTIME_S == 600.0

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


def test_read_file_is_bounded_before_return_to_worker_context(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_READ_FILE_CHARS", 5)
    target = tmp_path / "large.txt"
    target.write_text("abcdefghij")

    result = subagent._tool_read_file("large.txt")

    assert result.startswith("abcde\n...(read-file truncated at 5 chars)...")
    assert "fghij" not in result


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


def test_shell_tool_runs_from_subagent_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_ENABLE_SHELL", "1")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_SHELL_ALLOWLIST", "pwd")

    result = subagent._tool_shell("pwd")

    assert result.strip() == str(tmp_path)


def test_shell_tool_rejects_explicit_executable_paths(tmp_path, monkeypatch):
    python_exe = sys.executable
    python_name = Path(python_exe).name
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_ENABLE_SHELL", "1")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_SHELL_ALLOWLIST", python_name)

    result = subagent._tool_shell(f'{python_exe} -c "print(1)"')

    assert "executable must be an allowlisted command name" in result


def test_shell_tool_rejects_too_many_argv_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_ENABLE_SHELL", "1")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_SHELL_ALLOWLIST", "printf")
    monkeypatch.setattr(subagent, "_SHELL_MAX_ARGV", 2)

    result = subagent._tool_shell("printf one two")

    assert "too many arguments" in result


def test_shell_tool_does_not_resolve_allowlisted_executable_from_workspace_path(tmp_path, monkeypatch):
    fake_pwd = tmp_path / "pwd"
    fake_pwd.write_text("#!/bin/sh\necho MALICIOUS\n")
    fake_pwd.chmod(0o755)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_ENABLE_SHELL", "1")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_SHELL_ALLOWLIST", "pwd")
    monkeypatch.setenv("PATH", f".:{tmp_path}:{os.environ.get('PATH', '')}")

    result = subagent._tool_shell("pwd")

    assert result.strip() == str(tmp_path)
    assert "MALICIOUS" not in result


def test_shell_tool_does_not_inherit_secret_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_ENABLE_SHELL", "1")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_SHELL_ALLOWLIST", "env")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    monkeypatch.setenv("UNIT_API_KEY", "should-not-leak")

    result = subagent._tool_shell("env")

    assert "OPENAI_API_KEY" not in result
    assert "UNIT_API_KEY" not in result
    assert f"HOME={tmp_path}" in result
    assert "PATH=" in result


def test_shell_tool_output_is_bounded_with_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_ENABLE_SHELL", "1")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_SHELL_ALLOWLIST", "printf")
    monkeypatch.setattr(subagent, "_SHELL_OUTPUT_CAP", 5)

    result = subagent._tool_shell("printf abcdefghij")

    assert result.startswith("abcde\n...(shell output truncated at 5 chars)...")
    assert "fghij" not in result


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
    assert subagent._call_subagent_llm(handle, "prompt", 12) == ('(emit "cloud")', 0, 0)
    assert seen["called"]["timeout"] == subagent._SUBAGENT_LLM_TIMEOUT_S


def test_dispatch_returns_structured_digest_and_persists_transcript(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)

    responses = iter(['(write-file "out.txt" "hello")', '(emit "done")'])
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_a: (next(responses), 10, 5))

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
    assert index_entries[-1]["previous_entry_sha256"] == ""
    assert len(index_entries[-1]["entry_sha256"]) == 64
    assert index_entries[-1]["entry_sha256"] == subagent._index_entry_hash(index_entries[-1])
    assert saved["status"] == "ok"
    assert len(saved["turns"]) == 2
    assert (tmp_path / "workspace" / "out.txt").read_text() == "hello"


def test_run_index_entries_are_hash_chained(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    first = {"run_id": "one", "status": "ok", "transcript_path": "one.json", "transcript_sha256": "a" * 64}
    second = {"run_id": "two", "status": "error", "transcript_path": "two.json", "transcript_sha256": "b" * 64}

    subagent._append_run_index(first)
    subagent._append_run_index(second)

    entries = [json.loads(line) for line in (tmp_path / "runs" / "index.jsonl").read_text().splitlines()]
    assert entries[0]["previous_entry_sha256"] == ""
    assert entries[0]["entry_sha256"] == subagent._index_entry_hash(entries[0])
    assert entries[1]["previous_entry_sha256"] == entries[0]["entry_sha256"]
    assert entries[1]["entry_sha256"] == subagent._index_entry_hash(entries[1])


def test_verify_subagent_run_index_checks_chain_and_transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    runs = Path(subagent.SUBAGENT_RUN_DIR)
    runs.mkdir(parents=True)
    first_transcript = runs / "one.json"
    second_transcript = runs / "two.json"
    first_digest = subagent._json_atomic_write(str(first_transcript), {"status": "ok", "run_id": "one"})
    second_digest = subagent._json_atomic_write(str(second_transcript), {"status": "error", "run_id": "two"})

    subagent._append_run_index({
        "run_id": "one", "status": "ok",
        "transcript_path": str(first_transcript), "transcript_sha256": first_digest,
    })
    subagent._append_run_index({
        "run_id": "two", "status": "error",
        "transcript_path": str(second_transcript), "transcript_sha256": second_digest,
    })

    audit = json.loads(subagent.verify_subagent_run_index())

    assert audit["status"] == "index_verified"
    assert audit["entries_checked"] == 2
    assert audit["transcripts_checked"] == 2
    assert audit["issue_count"] == 0


def test_verify_subagent_run_index_detects_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    runs = Path(subagent.SUBAGENT_RUN_DIR)
    runs.mkdir(parents=True)
    transcript = runs / "one.json"
    digest = subagent._json_atomic_write(str(transcript), {"status": "ok", "run_id": "one"})
    subagent._append_run_index({
        "run_id": "one", "status": "ok",
        "transcript_path": str(transcript), "transcript_sha256": digest,
    })
    transcript.write_text(json.dumps({"status": "changed", "run_id": "one"}), encoding="utf-8")

    audit = json.loads(subagent.verify_subagent_run_index())

    assert audit["status"] == "index_tampered"
    assert audit["issue_count"] == 1
    assert audit["issues"][0]["issue"] == "transcript_hash_mismatch"


def test_dispatch_rejects_mixed_emit_and_tool_response(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_a: ('(emit "done")\n(write-file "hidden.txt" "nope")', 0, 0),
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
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_a: ('(write-file "a.txt" "a")\n(write-file "b.txt" "b")', 0, 0))

    payload = json.loads(subagent.dispatch("write too much", "write-file", "unit", max_turns=2))

    assert payload["status"] == "error"
    assert "QUOTA_EXCEEDED" in payload["summary"]
    assert payload["files_changed"] == ["a.txt"]
    assert (tmp_path / "workspace" / "a.txt").read_text() == "a"
    assert not (tmp_path / "workspace" / "b.txt").exists()


def test_per_turn_tool_quota_limits_multi_call_worker_response(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_TOOL_CALLS", 8)
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_TOOL_CALLS_PER_TURN", 1)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_a: ('(write-file "a.txt" "a")\n(write-file "b.txt" "b")', 0, 0),
    )

    payload = json.loads(subagent.dispatch("write too much at once", "write-file", "unit", max_turns=2))

    assert payload["status"] == "error"
    assert "TURN_QUOTA_EXCEEDED" in payload["summary"]
    assert payload["files_changed"] == ["a.txt"]
    assert (tmp_path / "workspace" / "a.txt").read_text() == "a"
    assert not (tmp_path / "workspace" / "b.txt").exists()
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "turn_quota_exceeded"


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
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_a: (next(responses), 0, 0))

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
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_a: ('(write-file "out.txt" "nope")', 0, 0))

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
        lambda *_a: ('(write-file "a.txt" "a")\n(write-file "b.txt" "b")', 0, 0),
    )

    payload = json.loads(subagent.dispatch(contract_goal, "write-file", "unit", max_turns=2))

    assert payload["status"] == "error"
    assert "QUOTA_EXCEEDED" in payload["summary"]
    assert payload["files_changed"] == ["a.txt"]
    assert (tmp_path / "workspace" / "a.txt").read_text() == "a"
    assert not (tmp_path / "workspace" / "b.txt").exists()
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["task_contract"]["max_tool_calls"] == 1


def test_task_contract_patch_proposal_only_records_without_writing(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    contract_goal = json.dumps({
        "objective": "propose a patch but do not apply it",
        "patch_proposal_only": True,
    })
    responses = iter([
        '(write-file "proposed.txt" "candidate")',
        '(emit "patch proposed")',
    ])
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_a: (next(responses), 0, 0))

    payload = json.loads(subagent.dispatch(contract_goal, "write-file", "unit", max_turns=3))

    assert payload["status"] == "ok"
    assert payload["files_changed"] == []
    assert payload["patch_proposals"] == [{"action": "write-file", "path": "proposed.txt"}]
    assert not (tmp_path / "workspace" / "proposed.txt").exists()
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["task_contract"]["patch_proposal_only"] is True
    assert saved["patch_proposals"] == [{
        "action": "write-file",
        "path": "proposed.txt",
        "content": "candidate",
    }]
    assert "PATCH_PROPOSAL_RECORDED" in saved["turns"][0]["tool_results"]


def test_task_contract_patch_proposal_only_must_be_boolean_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = subagent.dispatch(json.dumps({
        "objective": "bad patch mode",
        "patch_proposal_only": "yes",
    }), "write-file", "unit", max_turns=1)
    payload = json.loads(result)

    assert payload["status"] == "error"
    assert "patch_proposal_only" in payload["summary"]
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "contract_invalid"


def test_task_contract_requires_adjudication_marks_candidate_not_final(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    contract_goal = json.dumps({
        "objective": "produce high stakes output for review",
        "requires_adjudication": True,
    })
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_a: ('(emit "candidate answer")', 12, 5),
    )

    payload = json.loads(subagent.dispatch(contract_goal, "write-file", "unit", max_turns=1))

    assert payload["status"] == "needs_adjudication"
    assert payload["adjudication"]["required"] is True
    assert payload["adjudication"]["status"] == "pending"
    assert payload["adjudication"]["candidate_summary"] == "candidate answer"
    assert "requires adjudication" in payload["summary"]
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "adjudication_required"
    assert saved["task_contract"]["requires_adjudication"] is True
    assert saved["adjudication"]["candidate_summary"] == "candidate answer"
    assert saved["worker_token_usage"]["total_tokens"] == 17


def test_task_contract_requires_adjudication_must_be_boolean_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    payload = json.loads(subagent.dispatch(json.dumps({
        "objective": "bad adjudicator flag",
        "requires_adjudication": "yes",
    }), "write-file", "unit", max_turns=1))

    assert payload["status"] == "error"
    assert "requires_adjudication" in payload["summary"]
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "contract_invalid"


def test_review_subagent_candidate_reports_proposals_and_adjudication(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    transcript = Path(subagent.SUBAGENT_RUN_DIR) / "reviewable.json"
    record = {
        "status": "adjudication_required",
        "summary": "candidate answer",
        "task_contract": {"requires_adjudication": True, "patch_proposal_only": True},
        "patch_proposals": [{"action": "write-file", "path": "candidate.txt", "content": "draft"}],
        "adjudication": {"required": True, "status": "pending", "candidate_summary": "candidate answer"},
    }
    digest = subagent._json_atomic_write(str(transcript), record)
    subagent._write_transcript_integrity_sidecar(str(transcript), digest)

    review = json.loads(subagent.review_subagent_candidate(str(transcript)))

    assert review["status"] == "candidate_review_ready"
    assert review["checksum"] == "verified"
    assert review["patch_proposals"] == [{"action": "write-file", "path": "candidate.txt"}]
    assert review["adjudication"]["required"] is True
    assert set(review["gates"]) == {"patch_proposal_review", "adjudication_required"}
    assert not (tmp_path / "candidate.txt").exists()


def test_review_subagent_candidate_rejects_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    outside = tmp_path / "outside.json"
    outside.write_text("{}")

    review = json.loads(subagent.review_subagent_candidate(str(outside)))

    assert review["status"] == "candidate_review_error"
    assert "escapes run dir" in review["summary"]


def test_review_subagent_candidate_detects_checksum_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    transcript = Path(subagent.SUBAGENT_RUN_DIR) / "tampered.json"
    digest = subagent._json_atomic_write(str(transcript), {"status": "ok"})
    subagent._write_transcript_integrity_sidecar(str(transcript), digest)
    transcript.write_text(json.dumps({"status": "changed"}), encoding="utf-8")

    review = json.loads(subagent.review_subagent_candidate(str(transcript)))

    assert review["status"] == "transcript_tampered"
    assert review["expected_sha256"] == digest


def test_queue_only_dispatch_persists_task_without_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    payload = json.loads(subagent.dispatch("queue this safely", "write-file", "unit", max_turns=2))

    assert payload["status"] == "queued"
    assert payload["queue_path"].endswith(".json")
    queued = json.loads(Path(payload["queue_path"]).read_text())
    assert queued["status"] == "queued"
    assert queued["goal"] == "queue this safely"
    assert queued["tool_subset"] == ["write-file"]
    assert queued["max_turns"] == 2
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "queued"
    assert saved["queue_path"] == payload["queue_path"]
    assert len(payload["queue_sha256"]) == 64
    assert Path(payload["queue_sha256_path"]).exists()
    assert Path(payload["queue_sha256_path"]).read_text().startswith(payload["queue_sha256"])


def test_run_queued_dispatch_claims_task_and_runs_once(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)

    payload = json.loads(subagent.dispatch("queue and consume", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    calls = {"n": 0}

    def worker_response(*_args):
        calls["n"] += 1
        return ('(emit "worker done")', 3, 2)

    monkeypatch.setattr(subagent, "_call_subagent_llm", worker_response)

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert calls["n"] == 1
    assert result["status"] == "ok"
    assert result["task_sha256"] == payload["queue_sha256"]
    assert result["result"]["summary"] == "worker done"
    assert result["result"]["worker_token_usage"]["total_tokens"] == 5
    assert not queue_path.exists()
    assert Path(result["task_done_path"]).exists()
    assert Path(result["task_done_path"] + ".result.json").exists()
    assert Path(result["task_sha256_path"]).exists()
    assert Path(result["task_sha256_path"]).read_text().startswith(payload["queue_sha256"])
    assert not Path(str(queue_path) + ".sha256").exists()
    assert os.environ.get("OMEGACLAW_SUBAGENT_QUEUE_ONLY") == "1"


def test_run_queued_dispatch_preserves_task_contract_during_worker_run(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    contract_goal = json.dumps({
        "objective": "queued contract must still constrain writes",
        "allowed_paths": ["safe"],
    })
    payload = json.loads(subagent.dispatch(contract_goal, "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])

    responses = iter([
        ('(write-file "unsafe.txt" "nope")\n(write-file "safe/out.txt" "ok")', 4, 2),
        ('(emit "contract preserved")', 3, 1),
    ])
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_args: next(responses))

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "ok"
    assert result["result"]["summary"] == "contract preserved"
    assert result["result"]["files_changed"] == ["safe/out.txt"]
    assert not (tmp_path / "workspace" / "unsafe.txt").exists()
    assert (tmp_path / "workspace" / "safe" / "out.txt").read_text() == "ok"
    worker_transcript = json.loads(Path(result["result"]["transcript_path"]).read_text())
    assert worker_transcript["task_contract"]["allowed_paths"] == ["safe"]
    assert "CONTRACT_VIOLATION" in worker_transcript["turns"][0]["tool_results"]


def test_run_queued_dispatch_rejects_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    outside = tmp_path / "outside.json"
    outside.write_text("{}")

    result = json.loads(subagent.run_queued_dispatch(str(outside)))

    assert result["status"] == "queue_worker_error"
    assert "escapes queue dir" in result["summary"]


def test_run_queued_dispatch_rejects_result_sidecar_without_renaming(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs"
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(run_dir))
    queue_dir = run_dir / "queue"
    queue_dir.mkdir(parents=True)
    sidecar = queue_dir / "task.json.done.result.json"
    sidecar.write_text('{"status":"ok"}', encoding="utf-8")

    result = json.loads(subagent.run_queued_dispatch(str(sidecar)))

    assert result["status"] == "queue_worker_error"
    assert "pending queue/*.json task record" in result["summary"]
    assert sidecar.exists()
    assert not Path(str(sidecar) + ".claimed").exists()
    assert not Path(str(sidecar) + ".failed").exists()


def test_run_queued_dispatch_retains_failed_claim_for_audit(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    payload = json.loads(subagent.dispatch("queue invalid later", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    queued = json.loads(queue_path.read_text())
    queued["tool_subset"] = []
    digest = subagent._json_atomic_write(str(queue_path), queued)
    subagent._write_transcript_integrity_sidecar(str(queue_path), digest)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "queue_worker_error"
    assert "tool_subset" in result["summary"]
    assert not queue_path.exists()
    assert Path(str(queue_path) + ".failed").exists()
    assert Path(str(queue_path) + ".failed.result.json").exists()
    saved_result = json.loads(Path(str(queue_path) + ".failed.result.json").read_text())
    assert saved_result["status"] == "queue_worker_error"
    assert saved_result["queue_path"] == str(queue_path)
    assert len(result["result_sha256"]) == 64
    assert subagent._pending_queued_dispatch_paths() == []
    assert subagent._pending_dispatch_queue_count() == 0


def test_run_queued_dispatch_rejects_unexpected_queue_task_fields_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    payload = json.loads(subagent.dispatch("queue strict schema", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    queued = json.loads(queue_path.read_text())
    queued["worker_override"] = "unexpected mutable instruction"
    digest = subagent._json_atomic_write(str(queue_path), queued)
    subagent._write_transcript_integrity_sidecar(str(queue_path), digest)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "queue_worker_error"
    assert "unknown field" in result["summary"]
    assert not queue_path.exists()
    assert Path(str(queue_path) + ".failed").exists()


def test_run_queued_dispatch_rejects_strict_numeric_schema_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    payload = json.loads(subagent.dispatch("queue strict numeric schema", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    queued = json.loads(queue_path.read_text())
    queued["queued_at"] = float("nan")
    queued["max_turns"] = True
    digest = subagent._json_atomic_write(str(queue_path), queued)
    subagent._write_transcript_integrity_sidecar(str(queue_path), digest)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "queue_worker_error"
    assert "queued_at" in result["summary"]
    assert not queue_path.exists()
    assert Path(str(queue_path) + ".failed").exists()


def test_run_queued_dispatch_rejects_coerced_integer_metadata_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    payload = json.loads(subagent.dispatch("queue strict integer metadata", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    queued = json.loads(queue_path.read_text())
    queued["max_turns"] = 1.5
    queued["max_chars"] = "1000"
    digest = subagent._json_atomic_write(str(queue_path), queued)
    subagent._write_transcript_integrity_sidecar(str(queue_path), digest)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "queue_worker_error"
    assert "max_turns must be an integer" in result["summary"]
    assert not queue_path.exists()
    assert Path(str(queue_path) + ".failed").exists()


def test_run_queued_dispatch_rejects_task_contract_shape_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    payload = json.loads(subagent.dispatch("queue strict contract schema", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    queued = json.loads(queue_path.read_text())
    queued["task_contract"] = {"objective": "bad contract shape", "allowed_paths": "safe"}
    digest = subagent._json_atomic_write(str(queue_path), queued)
    subagent._write_transcript_integrity_sidecar(str(queue_path), digest)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "queue_worker_error"
    assert "allowed_paths must be a list" in result["summary"]
    assert not queue_path.exists()
    assert Path(str(queue_path) + ".failed").exists()


def test_run_queued_dispatch_rejects_checksum_mismatch_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    payload = json.loads(subagent.dispatch("queue then tamper", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    queued = json.loads(queue_path.read_text())
    queued["goal"] = "tampered before worker"
    queue_path.write_text(json.dumps(queued), encoding="utf-8")
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "queue_worker_error"
    assert "checksum mismatch" in result["summary"]
    assert result["expected_task_sha256"] == payload["queue_sha256"]
    assert result["task_sha256"] != payload["queue_sha256"]
    assert not queue_path.exists()
    failed_path = Path(str(queue_path) + ".failed")
    assert failed_path.exists()
    assert Path(str(failed_path) + ".sha256").exists()
    assert not Path(str(queue_path) + ".sha256").exists()


def test_run_queued_dispatch_rejects_missing_checksum_sidecar_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    payload = json.loads(subagent.dispatch("queue missing sidecar", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    Path(payload["queue_sha256_path"]).unlink()
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "queue_worker_error"
    assert "missing integrity sidecar" in result["summary"]
    assert not queue_path.exists()
    assert Path(str(queue_path) + ".failed").exists()


def test_run_queued_dispatch_preserves_task_cancel_file_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    cancel_file = tmp_path / "cancel.token"
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_CANCEL_FILE", str(cancel_file))
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    payload = json.loads(subagent.dispatch("queue then cancel", "write-file", "unit", max_turns=2))
    queue_path = Path(payload["queue_path"])
    cancel_file.write_text("cancel")
    monkeypatch.setattr(subagent, "_SUBAGENT_CANCEL_FILE", "")
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_dispatch(str(queue_path)))

    assert result["status"] == "cancelled"
    assert result["result"]["status"] == "cancelled"
    assert result["result"]["transcript_path"]
    saved = json.loads(Path(result["result"]["transcript_path"]).read_text())
    assert saved["status"] == "cancelled"
    assert subagent._SUBAGENT_CANCEL_FILE == ""
    assert not queue_path.exists()
    assert Path(str(queue_path) + ".done").exists()


def test_drain_queued_dispatches_is_bounded_and_preserves_queue_only_env(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)

    first = json.loads(subagent.dispatch("queue first", "write-file", "unit", max_turns=2))
    second = json.loads(subagent.dispatch("queue second", "write-file", "unit", max_turns=2))
    calls = {"n": 0}

    def worker_response(*_args):
        calls["n"] += 1
        return (f'(emit "worker done {calls["n"]}")', 1, 1)

    monkeypatch.setattr(subagent, "_call_subagent_llm", worker_response)

    drained = json.loads(subagent.drain_queued_dispatches(max_tasks=1))

    assert drained["status"] == "drained"
    assert drained["tasks_attempted"] == 1
    assert drained["tasks_completed"] == 1
    assert drained["remaining_queue_tasks"] == 1
    assert calls["n"] == 1
    assert not Path(first["queue_path"]).exists()
    assert Path(first["queue_path"] + ".done").exists()
    assert Path(second["queue_path"]).exists()
    assert os.environ.get("OMEGACLAW_SUBAGENT_QUEUE_ONLY") == "1"


def test_drain_queued_dispatches_reports_empty_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))

    drained = json.loads(subagent.drain_queued_dispatches(max_tasks=3))

    assert drained["status"] == "queue_empty"
    assert drained["tasks_attempted"] == 0
    assert drained["remaining_queue_tasks"] == 0
    assert drained["results"] == []


def test_run_queued_worker_loop_drains_until_idle_and_preserves_queue_only_env(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)

    first = json.loads(subagent.dispatch("queue async first", "write-file", "unit", max_turns=2))
    second = json.loads(subagent.dispatch("queue async second", "write-file", "unit", max_turns=2))
    calls = {"n": 0}

    def worker_response(*_args):
        calls["n"] += 1
        return (f'(emit "async worker done {calls["n"]}")', 1, 1)

    monkeypatch.setattr(subagent, "_call_subagent_llm", worker_response)
    monkeypatch.setattr(subagent.time, "sleep", lambda *_args: None)

    result = json.loads(subagent.run_queued_worker_loop(
        max_tasks=4, poll_interval_s=0, max_idle_polls=0, max_runtime_s=30,
    ))

    assert result["status"] == "worker_drained"
    assert result["stop_reason"] == "idle"
    assert result["tasks_attempted"] == 2
    assert result["tasks_completed"] == 2
    assert result["remaining_queue_tasks"] == 0
    assert calls["n"] == 2
    assert Path(first["queue_path"] + ".done").exists()
    assert Path(second["queue_path"] + ".done").exists()
    assert os.environ.get("OMEGACLAW_SUBAGENT_QUEUE_ONLY") == "1"


def test_run_queued_worker_loop_stops_on_max_runtime(tmp_path, monkeypatch):
    """The loop should exit with stop_reason=max_runtime when the wall-clock
    cap is reached between queued tasks, before claiming more work."""
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)

    first = json.loads(subagent.dispatch("runtime cap first", "write-file", "unit", max_turns=2))
    second = json.loads(subagent.dispatch("runtime cap second", "write-file", "unit", max_turns=2))
    calls = {"n": 0}

    def worker_response(*_args):
        calls["n"] += 1
        return (f'(emit "runtime cap done {calls["n"]}")', 1, 1)

    monkeypatch.setattr(subagent, "_call_subagent_llm", worker_response)
    monkeypatch.setattr(subagent.time, "sleep", lambda *_args: None)

    # Simulate: first task succeeds, then runtime cap is hit before second task.
    original_time = subagent.time.time
    fake_clock = {"t": 0.0}

    def fake_time():
        return fake_clock["t"]

    def fake_worker_loop_time(*_args):
        # After first task completes, jump clock past runtime cap
        if calls["n"] >= 1:
            fake_clock["t"] = 100.0
        return fake_time()

    monkeypatch.setattr(subagent.time, "time", fake_worker_loop_time)

    result = json.loads(subagent.run_queued_worker_loop(
        max_tasks=4, poll_interval_s=0, max_idle_polls=0, max_runtime_s=10.0,
    ))

    assert result["stop_reason"] == "max_runtime"
    assert result["tasks_attempted"] == 1
    assert calls["n"] == 1
    assert Path(first["queue_path"] + ".done").exists()
    assert Path(second["queue_path"]).exists()  # second still pending


def test_run_queued_worker_loop_records_worker_error_and_continues(tmp_path, monkeypatch):
    """A failing queued task should be recorded as queue_worker_error in
    results, but the loop should continue to the next pending task."""
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)

    first = json.loads(subagent.dispatch("error task", "write-file", "unit", max_turns=2))
    second = json.loads(subagent.dispatch("good task", "write-file", "unit", max_turns=2))
    calls = {"n": 0}

    def worker_response(*_args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated worker failure")
        return (f'(emit "recovered after error")', 1, 1)

    monkeypatch.setattr(subagent, "_call_subagent_llm", worker_response)
    monkeypatch.setattr(subagent.time, "sleep", lambda *_args: None)

    result = json.loads(subagent.run_queued_worker_loop(
        max_tasks=4, poll_interval_s=0, max_idle_polls=0, max_runtime_s=30,
    ))

    assert result["status"] == "worker_drained"
    assert result["tasks_attempted"] == 2
    assert result["tasks_completed"] == 1
    # First result should be an error; second should be a success
    statuses = [r.get("status") for r in result["results"]]
    assert "queue_worker_error" in statuses
    assert "ok" in statuses


def test_run_queued_worker_loop_honors_stop_file_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    stop_file = tmp_path / "stop.worker"
    stop_file.write_text("stop", encoding="utf-8")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 4)
    queued = json.loads(subagent.dispatch("queue but stop worker", "write-file", "unit", max_turns=2))
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    result = json.loads(subagent.run_queued_worker_loop(
        max_tasks=4, poll_interval_s=0, max_idle_polls=0, stop_file=str(stop_file),
    ))

    assert result["status"] == "worker_stopped"
    assert result["stop_reason"] == "stop_file"
    assert result["tasks_attempted"] == 0
    assert result["remaining_queue_tasks"] == 1
    assert Path(queued["queue_path"]).exists()


def test_run_queued_worker_loop_rejects_concurrent_local_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(subagent.time, "sleep", lambda *_args: None)
    lock_path = Path(subagent.SUBAGENT_RUN_DIR) / ".async-worker.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        if subagent.fcntl is None:
            return
        subagent.fcntl.flock(lock.fileno(), subagent.fcntl.LOCK_EX | subagent.fcntl.LOCK_NB)
        subagent._write_worker_loop_lock_metadata(lock, {
            "pid": 12345,
            "started_at": 111.0,
            "status": "running",
            "run_dir": subagent.SUBAGENT_RUN_DIR,
        })
        try:
            result = json.loads(subagent.run_queued_worker_loop(
                max_tasks=1, poll_interval_s=0, max_idle_polls=0,
            ))
        finally:
            subagent.fcntl.flock(lock.fileno(), subagent.fcntl.LOCK_UN)

    assert result["status"] == "worker_already_running"
    assert result["tasks_attempted"] == 0
    assert result["worker_lock"]["status"] == "running"
    assert result["worker_lock"]["pid"] == 12345


def test_run_queued_worker_loop_writes_finished_lock_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent, "SUBAGENT_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(subagent.time, "sleep", lambda *_args: None)

    result = json.loads(subagent.run_queued_worker_loop(
        max_tasks=1, poll_interval_s=0, max_idle_polls=0,
    ))
    lock_path = Path(result["lock_path"])
    metadata = json.loads(lock_path.read_text(encoding="utf-8"))

    assert result["status"] == "worker_idle"
    assert result["stop_reason"] == "idle"
    assert metadata["status"] == "finished"
    assert metadata["stop_reason"] == "idle"
    assert metadata["tasks_attempted"] == 0


def test_run_subagent_worker_loop_script_supports_no_claim_smoke(tmp_path):
    script = ROOT / "scripts" / "run-subagent-worker-loop"
    run_dir = tmp_path / "script-runs"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--run-dir",
            str(run_dir),
            "--max-tasks",
            "0",
            "--max-idle-polls",
            "0",
            "--poll-interval-s",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["status"] == "worker_idle"
    assert result["stop_reason"] == "max_tasks"
    assert result["tasks_attempted"] == 0
    assert result["remaining_queue_tasks"] == 0


def test_queue_only_dispatch_backpressure_fails_before_worker_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    queue_dir = tmp_path / "runs" / "queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "already.json").write_text("{}")
    monkeypatch.setenv("OMEGACLAW_SUBAGENT_QUEUE_ONLY", "1")
    monkeypatch.setattr(subagent, "_SUBAGENT_MAX_QUEUED_DISPATCHES", 1)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    payload = json.loads(subagent.dispatch("queue overflow", "write-file", "unit", max_turns=1))

    assert payload["status"] == "error"
    assert "queue backpressure" in payload["summary"]
    assert "queue_path" not in payload
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "queue_backpressure"


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


def test_dispatch_wall_clock_timeout_stops_before_llm(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)
    monkeypatch.setattr(subagent, "_SUBAGENT_DISPATCH_TIMEOUT_S", 0.01)
    monkeypatch.setattr(subagent, "_dispatch_timeout_exceeded", lambda start: True)
    monkeypatch.setattr(
        subagent,
        "_call_subagent_llm",
        lambda *_a: (_ for _ in ()).throw(AssertionError("should not call llm")),
    )

    payload = json.loads(subagent.dispatch("slow dispatch", "write-file", "unit", max_turns=2))

    assert payload["status"] == "error"
    assert "dispatch wall-clock timeout" in payload["summary"]
    assert payload.get("worker_token_usage", {}).get("total_tokens") == 0
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["status"] == "dispatch_timeout"


def test_worker_token_usage_aggregated_in_structured_return(tmp_path, monkeypatch):
    _write_unit_persona(tmp_path, monkeypatch)

    responses = iter([
        ('(write-file "out.txt" "hello")', 100, 50),
        ('(emit "done")', 80, 40),
    ])
    monkeypatch.setattr(subagent, "_call_subagent_llm", lambda *_a: next(responses))

    payload = json.loads(subagent.dispatch("write a file", "write-file", "unit", max_turns=3))

    assert payload["status"] == "ok"
    usage = payload.get("worker_token_usage", {})
    assert usage["input_tokens"] == 180
    assert usage["output_tokens"] == 90
    assert usage["total_tokens"] == 270
    saved = json.loads(Path(payload["transcript_path"]).read_text())
    assert saved["worker_token_usage"]["total_tokens"] == 270
