import time

import channels.telegram as telegram


def _reset_send_state(monkeypatch):
    monkeypatch.setenv("TG_SEND_DEDUPE_WINDOW_S", "10")
    telegram._connected = True
    telegram._last_sent_key = ""
    telegram._last_sent_time = 0.0
    telegram._partial_send_chunks = {}


def test_send_message_to_suppresses_immediate_duplicate(monkeypatch):
    _reset_send_state(monkeypatch)
    calls = []

    def fake_api_call(method, payload, timeout=None, use_post=False):
        calls.append((method, payload, timeout, use_post))
        return {"ok": True}

    monkeypatch.setattr(telegram, "_api_call", fake_api_call)

    first = telegram._send_message_to("same message", "123")
    second = telegram._send_message_to("same message", "123")

    assert len(calls) == 1
    assert first == "TELEGRAM_SEND_OK chunks=1 chars=12"
    assert second == "TELEGRAM_SEND_DEDUPLICATED"
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["chat_id"] == "123"
    assert calls[0][1]["text"] == "same message"


def test_send_message_to_allows_distinct_or_later_repeats(monkeypatch):
    _reset_send_state(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram,
        "_api_call",
        lambda method, payload, timeout=None, use_post=False: calls.append(payload) or {"ok": True},
    )

    telegram._send_message_to("same message", "123")
    telegram._send_message_to("different message", "123")
    telegram._last_sent_time = time.time() - 11
    telegram._send_message_to("different message", "123")
    telegram._send_message_to("different message", "456")

    assert [call["text"] for call in calls] == [
        "same message",
        "different message",
        "different message",
        "different message",
    ]
    assert [call["chat_id"] for call in calls] == ["123", "123", "123", "456"]


def test_send_dedupe_can_be_disabled(monkeypatch):
    _reset_send_state(monkeypatch)
    monkeypatch.setenv("TG_SEND_DEDUPE_WINDOW_S", "0")
    calls = []
    monkeypatch.setattr(
        telegram,
        "_api_call",
        lambda method, payload, timeout=None, use_post=False: calls.append(payload) or {"ok": True},
    )

    telegram._send_message_to("same message", "123")
    telegram._send_message_to("same message", "123")

    assert len(calls) == 2


def test_failed_send_raises_and_does_not_poison_dedupe(monkeypatch):
    _reset_send_state(monkeypatch)
    attempts = []

    def flaky_api_call(method, payload, timeout=None, use_post=False):
        attempts.append(payload)
        if len(attempts) == 1:
            raise TimeoutError("simulated")
        return {"ok": True}

    monkeypatch.setattr(telegram, "_api_call", flaky_api_call)

    import pytest

    with pytest.raises(RuntimeError, match="Telegram send failed"):
        telegram._send_message_to("retry me", "123")

    status = telegram._send_message_to("retry me", "123")
    assert status == "TELEGRAM_SEND_OK chunks=1 chars=8"
    assert len(attempts) == 2


def test_poll_disconnected_does_not_block_outbound_send(monkeypatch):
    _reset_send_state(monkeypatch)
    monkeypatch.setattr(telegram, "_connected", False)
    calls = []
    monkeypatch.setattr(
        telegram,
        "_api_call",
        lambda method, payload, timeout=None, use_post=False: calls.append(payload) or {"ok": True},
    )

    status = telegram._send_message_to("hello", "123")
    assert status == "TELEGRAM_SEND_OK chunks=1 chars=5"
    assert calls[0]["chat_id"] == "123"


def test_missing_target_raises_instead_of_reporting_success(monkeypatch):
    _reset_send_state(monkeypatch)

    import pytest

    with pytest.raises(RuntimeError, match="Telegram send unavailable"):
        telegram._send_message_to("hello", "")


def test_partial_chunk_failure_resumes_from_first_undelivered_chunk(monkeypatch):
    _reset_send_state(monkeypatch)
    calls = []
    long_text = "a" * 3900 + "b" * 20

    def flaky_api_call(method, payload, timeout=None, use_post=False):
        calls.append(payload["text"])
        if len(calls) == 2:
            raise TimeoutError("simulated second chunk failure")
        return {"ok": True}

    monkeypatch.setattr(telegram, "_api_call", flaky_api_call)

    import pytest

    with pytest.raises(RuntimeError, match="Telegram send failed"):
        telegram._send_message_to(long_text, "123")

    status = telegram._send_message_to(long_text, "123")
    assert status == f"TELEGRAM_SEND_OK chunks=2 chars={len(long_text)}"
    assert [len(chunk) for chunk in calls] == [3900, 20, 20]
