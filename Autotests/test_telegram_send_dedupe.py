import time

import channels.telegram as telegram


def _reset_send_state(monkeypatch):
    monkeypatch.setenv("TG_SEND_DEDUPE_WINDOW_S", "10")
    telegram._connected = True
    telegram._last_sent_key = ""
    telegram._last_sent_time = 0.0


def test_send_message_to_suppresses_immediate_duplicate(monkeypatch):
    _reset_send_state(monkeypatch)
    calls = []

    def fake_api_call(method, payload, timeout=None, use_post=False):
        calls.append((method, payload, timeout, use_post))
        return {"ok": True}

    monkeypatch.setattr(telegram, "_api_call", fake_api_call)

    telegram._send_message_to("same message", "123")
    telegram._send_message_to("same message", "123")

    assert len(calls) == 1
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
