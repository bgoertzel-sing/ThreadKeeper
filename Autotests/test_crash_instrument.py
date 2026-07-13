"""Step 12 tests: native crash instrumentation."""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys_path_inserted = str(ROOT / "channels")
if sys_path_inserted not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_inserted)

import crash_instrument as ci


def test_enable_core_dumps(tmp_path):
    core_dir = str(tmp_path / "cores")
    result = ci.enable_core_dumps(limit_mb=50, core_dir=core_dir)
    assert result["core_dir"] == core_dir
    assert result["limit_mb"] == 50
    assert os.path.isdir(core_dir)


def test_record_runtime_versions(tmp_path):
    path = str(tmp_path / "versions.json")
    versions = ci.record_runtime_versions(path=path)
    assert "timestamp" in versions
    assert "python" in versions
    assert "kernel" in versions
    assert "swipl" in versions
    assert "telethon" in versions

    # Verify file was written
    with open(path) as f:
        saved = json.load(f)
    assert saved["python"]["version"] == versions["python"]["version"]


def test_capture_crash_sigsegv(tmp_path):
    crash_log = str(tmp_path / "crashes.jsonl")
    record = ci.capture_crash(
        pid=999999, signum=11, exit_code=-11, crash_log=crash_log
    )
    assert record["classification"] == "SIGSEGV"
    assert record["signal"] == 11
    assert "timestamp" in record
    # backtrace may be None if gdb not available / process gone
    assert "backtrace" in record

    # Verify it was logged
    with open(crash_log) as f:
        logged = json.loads(f.readline())
    assert logged["classification"] == "SIGSEGV"


def test_capture_crash_oom(tmp_path):
    crash_log = str(tmp_path / "crashes.jsonl")
    record = ci.capture_crash(
        pid=999999, signum=9, exit_code=137, crash_log=crash_log
    )
    assert record["classification"] == "OOM_KILL"
    assert "oom_evidence" in record


def test_capture_crash_normal_exit(tmp_path):
    crash_log = str(tmp_path / "crashes.jsonl")
    record = ci.capture_crash(
        pid=os.getpid(), exit_code=0, crash_log=crash_log
    )
    assert record["classification"] == "normal_exit"


def test_capture_crash_timeout(tmp_path):
    crash_log = str(tmp_path / "crashes.jsonl")
    record = ci.capture_crash(
        pid=12345, exit_code=124, crash_log=crash_log
    )
    assert record["classification"] == "timeout"


def test_get_loaded_libraries():
    libs = ci.get_loaded_libraries(os.getpid())
    assert isinstance(libs, list)
    # Current process should have at least libc
    assert len(libs) > 0


def test_crash_log_is_jsonl(tmp_path):
    crash_log = str(tmp_path / "crashes.jsonl")
    ci.capture_crash(pid=1, signum=11, exit_code=-11, crash_log=crash_log)
    ci.capture_crash(pid=2, signum=9, exit_code=137, crash_log=crash_log)
    ci.capture_crash(pid=3, exit_code=0, crash_log=crash_log)

    with open(crash_log) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 3
    assert lines[0]["classification"] == "SIGSEGV"
    assert lines[1]["classification"] == "OOM_KILL"
    assert lines[2]["classification"] == "normal_exit"
