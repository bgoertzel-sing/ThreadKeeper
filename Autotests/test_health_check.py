"""Step 10 tests: liveness, readiness, and end-to-end health layers."""
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys_path_inserted = str(ROOT / "channels")
if sys_path_inserted not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_inserted)

import health_check as hc


def test_liveness_current_process_is_alive():
    result = hc.check_liveness(os.getpid())
    assert result.alive
    assert "not restart-looping" in result.reason


def test_liveness_dead_pid():
    result = hc.check_liveness(999999)
    assert not result.alive
    assert "does not exist" in result.reason


def test_liveness_no_pid():
    result = hc.check_liveness(None)
    assert not result.alive
    assert "no owner PID" in result.reason


def test_liveness_pid_1_rejected():
    result = hc.check_liveness(1)
    assert not result.alive


def test_restart_loop_detection():
    # Reset the internal state
    hc._restart_timestamps.clear()
    for _ in range(hc.RESTART_LOOP_THRESHOLD):
        hc.record_restart()
    result = hc.check_liveness(os.getpid())
    assert not result.alive
    assert "restart loop" in result.reason


def test_readiness_all_conditions_met():
    result = hc.check_readiness(
        swi_initialized=True,
        bridge_authenticated=True,
        bridge_handler_installed=True,
        ingress_connected=True,
        outbound_probe=True,
    )
    assert result.ready
    assert "all readiness conditions met" in result.reason


def test_readiness_missing_bridge():
    result = hc.check_readiness(
        swi_initialized=True,
        bridge_authenticated=False,
        bridge_handler_installed=True,
        ingress_connected=True,
        outbound_probe=True,
    )
    assert not result.ready
    assert "bridge_authenticated" in result.reason


def test_readiness_nothing_ready():
    result = hc.check_readiness(
        swi_initialized=False,
        bridge_authenticated=False,
        bridge_handler_installed=False,
        ingress_connected=False,
        outbound_probe=False,
    )
    assert not result.ready
    assert "swi_initialized" in result.reason


def test_e2e_health_full_success():
    ok, reason = hc.check_e2e_health(True, True, True, True)
    assert ok
    assert "completed end-to-end" in reason


def test_e2e_health_not_received():
    ok, reason = hc.check_e2e_health(True, False, False, False)
    assert not ok
    assert "not received" in reason


def test_e2e_health_wrong_chat():
    ok, reason = hc.check_e2e_health(True, True, True, False)
    assert not ok
    assert "wrong chat" in reason


def test_health_full_success():
    hc._restart_timestamps.clear()
    result = hc.check_health(
        owner_pid=os.getpid(),
        swi_initialized=True,
        bridge_authenticated=True,
        bridge_handler_installed=True,
        ingress_connected=True,
        outbound_probe=True,
        e2e_canary=True,
    )
    assert result.healthy
    assert result.liveness.alive
    assert result.readiness.ready
    assert result.e2e_canary


def test_health_liveness_failure_short_circuits():
    hc._restart_timestamps.clear()
    result = hc.check_health(
        owner_pid=999999,
        swi_initialized=True,
        bridge_authenticated=True,
        bridge_handler_installed=True,
        ingress_connected=True,
        outbound_probe=True,
        e2e_canary=True,
    )
    assert not result.healthy
    assert "liveness failed" in result.reason
    assert result.readiness is None  # short-circuited


def test_health_readiness_failure():
    hc._restart_timestamps.clear()
    result = hc.check_health(
        owner_pid=os.getpid(),
        swi_initialized=True,
        bridge_authenticated=False,
        bridge_handler_installed=True,
        ingress_connected=True,
        outbound_probe=True,
        e2e_canary=True,
    )
    assert not result.healthy
    assert "readiness failed" in result.reason
    assert result.liveness.alive  # liveness passed


def test_health_no_e2e_canary():
    hc._restart_timestamps.clear()
    result = hc.check_health(
        owner_pid=os.getpid(),
        swi_initialized=True,
        bridge_authenticated=True,
        bridge_handler_installed=True,
        ingress_connected=True,
        outbound_probe=True,
        e2e_canary=False,
    )
    assert not result.healthy
    assert "canary not proven" in result.reason


def test_parse_supervisor_status():
    text = """owner-active pid 12345
topology workers=1 owned_mtproto_bridges=1 global_mtproto_bridges=1
readiness=process-topology-ok (end-to-end Telegram delivery not proven)
Log: /some/path"""
    d = hc.parse_supervisor_status(text)
    assert d["owner_pid"] == 12345
    assert d["workers"] == 1
    assert d["owned_mtproto_bridges"] == 1
    assert "process-topology-ok" in d["readiness"]


def test_each_broken_state_fails_correct_layer():
    """Verify that each deliberately broken state fails the correct layer."""
    hc._restart_timestamps.clear()

    # Broken liveness (dead PID)
    r1 = hc.check_health(999999, True, True, True, True, True, True)
    assert not r1.healthy and "liveness" in r1.reason

    # Broken readiness (no bridge)
    r2 = hc.check_health(os.getpid(), True, False, True, True, True, True)
    assert not r2.healthy and "readiness" in r2.reason

    # Broken e2e (no canary)
    r3 = hc.check_health(os.getpid(), True, True, True, True, True, False)
    assert not r3.healthy and "canary" in r3.reason

    # All good
    r4 = hc.check_health(os.getpid(), True, True, True, True, True, True)
    assert r4.healthy
