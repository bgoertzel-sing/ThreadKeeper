"""Step 11 tests: structured privacy-safe event journaling."""
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

import event_journal as ej


@pytest.fixture
def journal_path(tmp_path):
    path = str(tmp_path / "events.jsonl")
    ej.configure(path, component_id="test_telegram", generation_id="gen-1")
    return path


def test_message_received_event(journal_path):
    ej.log_message_received("corr-1", "chat-42", 100)
    events = ej.read_journal(journal_path)
    assert len(events) == 1
    assert events[0]["event"] == "message_received"
    assert events[0]["correlation_id"] == "corr-1"
    assert events[0]["source_chat_id"] == "chat-42"
    assert events[0]["component"] == "test_telegram"
    assert events[0]["generation_id"] == "gen-1"
    assert "timestamp" in events[0]


def test_send_attempt_success(journal_path):
    ej.log_send_attempt("corr-1", "chat-42", True)
    events = ej.read_journal(journal_path)
    assert events[0]["success"] is True
    assert events[0]["target_chat_id"] == "chat-42"


def test_send_attempt_failure(journal_path):
    ej.log_send_attempt("corr-1", "chat-42", False, error="HTTP 429")
    events = ej.read_journal(journal_path)
    assert events[0]["success"] is False
    assert events[0]["error"] == "HTTP 429"


def test_queue_overflow_event(journal_path):
    ej.log_queue_overflow(64, 64, 5)
    events = ej.read_journal(journal_path)
    assert events[0]["event"] == "queue_overflow"
    assert events[0]["depth"] == 64


def test_state_transition(journal_path):
    ej.log_state_transition("idle", "processing", "message dequeued")
    events = ej.read_journal(journal_path)
    assert events[0]["from_state"] == "idle"
    assert events[0]["to_state"] == "processing"


def test_exit_classification(journal_path):
    ej.log_exit(signum=11, exit_code=-11, classification="SIGSEGV")
    events = ej.read_journal(journal_path)
    assert events[0]["classification"] == "SIGSEGV"
    assert events[0]["signal"] == 11


def test_classify_exit():
    assert ej.classify_exit(0) == "normal_exit"
    assert ej.classify_exit(-11, 11) == "SIGSEGV"
    assert ej.classify_exit(137, 9) == "OOM_KILL"
    assert ej.classify_exit(137) == "OOM_KILL"
    assert ej.classify_exit(-15, 15) == "SIGTERM_operator_stop"
    assert ej.classify_exit(124) == "timeout"
    assert ej.classify_exit(1) == "nonzero_exit_1"


def test_secret_safe():
    safe = {"event": "send", "correlation_id": "abc", "target_chat_id": "42"}
    assert ej.is_secret_safe(safe)

    unsafe = {"event": "send", "token": "secret-value"}
    assert not ej.is_secret_safe(unsafe)

    unsafe2 = {"event": "send", "api_key": "key-value"}
    assert not ej.is_secret_safe(unsafe2)


def test_no_message_body_in_events(journal_path):
    """Verify that event journaling never records message bodies."""
    ej.log_message_received("corr-1", "chat-42", 100)
    ej.log_send_attempt("corr-1", "chat-42", True)
    ej.log_canary_result("corr-1", "receive", True, "ok")
    events = ej.read_journal(journal_path)
    for e in events:
        assert "text" not in e
        assert "message_body" not in e
        assert "content" not in e
        assert ej.is_secret_safe(e)


def test_canary_events(journal_path):
    ej.log_canary_result("corr-1", "send", True, "delivered")
    events = ej.read_journal(journal_path)
    assert events[0]["phase"] == "send"
    assert events[0]["success"] is True


def test_bridge_events(journal_path):
    ej.log_bridge_started(pid=12345, parent_pid=12340)
    ej.log_bridge_authenticated("Protomegabot", 8562797306)
    events = ej.read_journal(journal_path)
    assert events[0]["pid"] == 12345
    assert events[1]["username"] == "Protomegabot"


def test_canary_reconstructable(journal_path):
    """One canary can be reconstructed end-to-end from metadata alone."""
    ej.log_message_received("corr-canary", "chat-42", 777)
    ej.log_message_dequeued("corr-canary")
    ej.log_send_attempt("corr-canary", "chat-42", True)
    events = ej.read_journal(journal_path)
    assert len(events) == 3
    # All events share the same correlation_id
    assert all(e["correlation_id"] == "corr-canary" for e in events)
    # Events are in chronological order
    assert events[0]["timestamp"] <= events[1]["timestamp"] <= events[2]["timestamp"]
    # No message bodies
    for e in events:
        assert ej.is_secret_safe(e)
        assert "text" not in e
