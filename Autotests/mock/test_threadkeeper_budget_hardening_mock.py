"""Focused checks for ThreadKeeper budget/accounting file hardening."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import threadkeeper_budget as tb  # noqa: E402


def test_budget_usage_log_rejects_symlink_target(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable on this platform")
    usage_log = tmp_path / "memory" / "usage.jsonl"
    usage_log.parent.mkdir()
    outside = tmp_path / "outside-usage.jsonl"
    outside.write_text("", encoding="utf-8")
    usage_log.symlink_to(outside)

    tracker = tb.BudgetTracker(
        usage_log=str(usage_log),
        escalation_log=str(tmp_path / "memory" / "escalations.jsonl"),
    )
    tracker.record("worker_loop", "unit", 3, 5)

    assert outside.read_text(encoding="utf-8") == ""
    assert usage_log.is_symlink()


def test_budget_escalation_log_rejects_symlink_target(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable on this platform")
    escalation_log = tmp_path / "memory" / "escalations.jsonl"
    escalation_log.parent.mkdir()
    outside = tmp_path / "outside-escalations.jsonl"
    outside.write_text("", encoding="utf-8")
    escalation_log.symlink_to(outside)

    tracker = tb.BudgetTracker(
        usage_log=str(tmp_path / "memory" / "usage.jsonl"),
        escalation_log=str(escalation_log),
    )
    tracker.should_escalate(thread_id="default", subproblem_is_hard=False)

    assert outside.read_text(encoding="utf-8") == ""
    assert escalation_log.is_symlink()


def test_budget_usage_log_read_rejects_symlink_source(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable on this platform")
    usage_log = tmp_path / "memory" / "usage.jsonl"
    usage_log.parent.mkdir()
    outside = tmp_path / "outside-usage.jsonl"
    outside.write_text(
        json.dumps({"thread_id": "default", "input_tokens": 100, "output_tokens": 23}) + "\n",
        encoding="utf-8",
    )
    usage_log.symlink_to(outside)

    tracker = tb.BudgetTracker(
        usage_log=str(usage_log),
        escalation_log=str(tmp_path / "memory" / "escalations.jsonl"),
    )

    assert tracker.spent_tokens("default") == 0


def test_budget_usage_log_read_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "_MAX_BUDGET_LOG_BYTES", 64)
    usage_log = tmp_path / "memory" / "usage.jsonl"
    usage_log.parent.mkdir()
    usage_log.write_text(
        json.dumps({"thread_id": "default", "input_tokens": 100, "output_tokens": 23})
        + "\n"
        + ("x" * 128),
        encoding="utf-8",
    )

    tracker = tb.BudgetTracker(
        usage_log=str(usage_log),
        escalation_log=str(tmp_path / "memory" / "escalations.jsonl"),
    )

    assert tracker.spent_tokens("default") == 0
