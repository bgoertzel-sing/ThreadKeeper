import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "channels"))
sys.path.insert(0, str(ROOT))

import helper


def test_compact_skill_results_deduplicates_identical_command_returns():
    repeated = (
        '(RESULTS: ('
        '(COMMAND_RETURN: ((send "hello") None)) '
        '(COMMAND_RETURN: ((send "hello") None)) '
        '(COMMAND_RETURN: ((continue-thinking "more") CONTINUE-REQUESTED)) '
        '(COMMAND_RETURN: ((continue-thinking "more") CONTINUE-REQUESTED))'
        '))'
    )

    compact = helper.compact_skill_results(repeated)

    assert compact.count('(COMMAND_RETURN: ((send "hello") None))') == 1
    assert compact.count('(COMMAND_RETURN: ((continue-thinking "more") CONTINUE-REQUESTED))') == 1
    assert compact.startswith('(RESULTS: (')


def test_compact_skill_results_preserves_distinct_diagnostics():
    value = (
        '(RESULTS: ('
        '(COMMAND_RETURN: ((send "hello") None)) '
        '(COMMAND_RETURN: ((send "different") None))'
        '))'
    )

    compact = helper.compact_skill_results(value)

    assert '(send "hello")' in compact
    assert '(send "different")' in compact


def test_telegram_send_uses_active_dequeued_chat(monkeypatch):
    telegram = importlib.import_module("channels.telegram")
    sent = []

    def fake_api_call(method, params=None, **kwargs):
        sent.append((method, dict(params or {})))
        return []

    monkeypatch.setattr(telegram, "_api_call", fake_api_call)
    monkeypatch.setattr(telegram, "_connected", True)
    monkeypatch.setattr(telegram, "_chat_id", "fallback")
    monkeypatch.setattr(telegram, "_reply_chat_id", "later-chat")
    monkeypatch.setattr(telegram, "_active_chat_id", "dequeued-chat")

    telegram.send_message("bound reply")

    assert sent == [("sendMessage", {"chat_id": "dequeued-chat", "text": "bound reply"})]


def test_telegram_preack_targets_message_chat_without_rebinding_active(monkeypatch):
    telegram = importlib.import_module("channels.telegram")
    sent = []

    def fake_api_call(method, params=None, **kwargs):
        sent.append((method, dict(params or {})))
        return []

    monkeypatch.setattr(telegram, "_api_call", fake_api_call)
    monkeypatch.setattr(telegram, "_connected", True)
    monkeypatch.setattr(telegram, "_active_chat_id", "existing-active")
    monkeypatch.setattr(telegram, "_last_preack_key", "")
    monkeypatch.setattr(telegram, "_last_preack_time", 0.0)
    monkeypatch.setenv("TG_PREACK_LONG_REQUESTS", "true")

    telegram._maybe_send_preack("Ben", "please " + "formalize " * 60, chat_id="incoming-chat")

    assert sent[0][1]["chat_id"] == "incoming-chat"
    assert telegram._active_chat_id == "existing-active"
