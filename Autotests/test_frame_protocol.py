"""Step 9 tests: bounded framed IPC and backpressure."""
import io
import os
import struct
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys_path_inserted = str(ROOT / "channels")
if sys_path_inserted not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_inserted)

import frame_protocol as fp


def test_encode_decode_roundtrip():
    payload = {"type": "message", "text": "hello", "chat_id": "42"}
    frame = fp.encode_frame(payload)
    buf = bytearray(frame)
    result = fp.decode_frame(buf)
    assert result is not None
    decoded, consumed = result
    assert decoded == payload
    assert consumed == len(frame)


def test_decode_partial_returns_none():
    payload = {"type": "message", "text": "hello"}
    frame = fp.encode_frame(payload)
    # Only first 5 bytes available
    buf = bytearray(frame[:5])
    assert fp.decode_frame(buf) is None


def test_decode_bad_magic_raises():
    header = struct.pack(fp.HEADER_FORMAT, b"XXXX", 1, 0, 0)
    buf = bytearray(header)
    with pytest.raises(fp.FrameError, match="bad magic"):
        fp.decode_frame(buf)


def test_decode_wrong_version_raises():
    header = struct.pack(fp.HEADER_FORMAT, fp.MAGIC, 99, 0, 0)
    buf = bytearray(header)
    with pytest.raises(fp.FrameError, match="unsupported version"):
        fp.decode_frame(buf)


def test_oversized_payload_raises():
    big = {"data": "x" * (fp.DEFAULT_MAX_PAYLOAD + 1)}
    with pytest.raises(fp.FrameError, match="exceeds max"):
        fp.encode_frame(big)


def test_bounded_queue_rejects_overflow():
    q = fp.BoundedQueue(max_depth=3, overflow_policy="reject")
    assert q.push("a")
    assert q.push("b")
    assert q.push("c")
    assert not q.push("d")  # rejected
    assert q.peek_depth() == 3
    stats = q.stats()
    assert stats["overflow_count"] == 1
    assert stats["total_accepted"] == 3


def test_bounded_queue_drop_oldest():
    q = fp.BoundedQueue(max_depth=2, overflow_policy="drop_oldest")
    q.push("a")
    q.push("b")
    assert q.push("c")  # drops "a"
    assert q.pop() == "b"
    assert q.pop() == "c"
    assert q.pop() is None
    stats = q.stats()
    assert stats["overflow_count"] == 1


def test_bounded_queue_fifo_order():
    q = fp.BoundedQueue(max_depth=10)
    for i in range(5):
        q.push(i)
    for i in range(5):
        assert q.pop() == i


def test_ack_frame_structure():
    ack = fp.make_ack_frame("corr-123", True)
    assert ack["type"] == "ack"
    assert ack["correlation_id"] == "corr-123"
    assert ack["accepted"] is True


def test_overflow_frame_structure():
    err = fp.make_overflow_frame(64, 64)
    assert err["type"] == "error"
    assert err["error"] == "queue_overflow"
    assert err["depth"] == 64


def test_generation_frame_structure():
    gen = fp.make_generation_frame("gen-42")
    assert gen["type"] == "generation"
    assert gen["generation_id"] == "gen-42"
    assert gen["protocol_version"] == fp.PROTOCOL_VERSION


def test_write_frame_to_fd(tmp_path):
    r, w = os.pipe()
    try:
        payload = {"type": "status", "message": "ok"}
        n = fp.write_frame(w, payload)
        os.close(w)
        w = -1
        buf = bytearray()
        result = fp.read_frame(r, buf)
        assert result == payload
        assert n > 0
    finally:
        if w >= 0:
            os.close(w)
        os.close(r)


def test_read_frame_handles_partial_reads(tmp_path):
    r, w = os.pipe()
    try:
        payload1 = {"type": "message", "n": 1}
        payload2 = {"type": "message", "n": 2}
        fp.write_frame(w, payload1)
        fp.write_frame(w, payload2)
        os.close(w)
        w = -1
        buf = bytearray()
        assert fp.read_frame(r, buf) == payload1
        assert fp.read_frame(r, buf) == payload2
        assert fp.read_frame(r, buf) is None  # EOF
    finally:
        if w >= 0:
            os.close(w)
        os.close(r)


def test_concurrent_push_pop_thread_safety():
    q = fp.BoundedQueue(max_depth=1000, overflow_policy="drop_oldest")
    errors = []

    def producer():
        for i in range(500):
            if not q.push(i):
                pass

    def consumer():
        for _ in range(500):
            item = q.pop()
            if item is None:
                time.sleep(0.001)

    t1 = threading.Thread(target=producer)
    t2 = threading.Thread(target=consumer)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive()
    assert not t2.is_alive()
