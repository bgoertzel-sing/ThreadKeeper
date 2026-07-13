import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "channels"))
sys.path.insert(0, str(ROOT))


def _telegram():
    return importlib.import_module("channels.telegram")


def test_mtproto_mode_never_sync_polls(monkeypatch):
    telegram = _telegram()
    called = []

    monkeypatch.setattr(telegram, "_receive_transport", "mtproto")
    monkeypatch.setattr(telegram, "_sync_poll", True)
    monkeypatch.setattr(telegram, "_running", True)
    monkeypatch.setattr(telegram, "_pending_messages", [])
    monkeypatch.setattr(telegram, "_last_message", "")
    monkeypatch.setattr(telegram, "_poll_once", lambda *args, **kwargs: called.append(True))

    assert telegram.getLastMessage() == ""
    assert called == []


def test_poll_once_fails_closed_outside_bot_api(monkeypatch):
    telegram = _telegram()
    monkeypatch.setattr(telegram, "_receive_transport", "mtproto")

    with pytest.raises(RuntimeError, match="getUpdates is disabled"):
        telegram._poll_once()


def test_explicit_receive_transport_rejects_legacy_conflict(monkeypatch):
    telegram = _telegram()
    monkeypatch.setenv("TG_RECEIVE_TRANSPORT", "mtproto")
    monkeypatch.setenv("TG_USE_MTPROTO", "false")

    with pytest.raises(ValueError, match="conflicts"):
        telegram._configured_receive_transport()


def test_receive_transport_aliases_and_legacy_compatibility(monkeypatch):
    telegram = _telegram()
    monkeypatch.setenv("TG_RECEIVE_TRANSPORT", "telethon")
    monkeypatch.setenv("TG_USE_MTPROTO", "true")
    assert telegram._configured_receive_transport() == "mtproto"

    monkeypatch.delenv("TG_RECEIVE_TRANSPORT")
    monkeypatch.setenv("TG_USE_MTPROTO", "false")
    assert telegram._configured_receive_transport() == "bot_api"


def test_bridge_command_is_owned_not_setsid(monkeypatch, tmp_path):
    mtproto = importlib.import_module("channels.telegram_mtproto")
    calls = []

    class FakeProc:
        pid = 12345
        stdout = None

        def poll(self):
            return None

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            mtproto._mtproto_running = True
            mtproto._mtproto_ready.set()

    python_bin = tmp_path / "python"
    python_bin.write_text("", encoding="utf-8")
    env_file = tmp_path / "telegram.env"
    env_file.write_text(
        "TELEGRAM_API_ID=1\n"
        "TELEGRAM_API_HASH=redacted-test-value\n"
        "TG_BOT_TOKEN=redacted-test-value\n"
        f"OPENCLAW_SUBPROCESS_PYTHON={python_bin}\n"
        f"TG_MTPROTO_FIFO={tmp_path / 'fifo'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMEGACLAW_TELEGRAM_ENV", str(env_file))
    monkeypatch.setattr(mtproto, "_mtproto_proc", None)
    monkeypatch.setattr(mtproto.threading, "Thread", FakeThread)

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProc()

    monkeypatch.setattr(mtproto.subprocess, "Popen", fake_popen)

    mtproto.start_mtproto()

    command, kwargs = calls[0]
    assert command == [str(python_bin), "-u", mtproto._BRIDGE_SCRIPT]
    assert "setsid" not in command
    assert kwargs["env"]["TG_MTPROTO_PARENT_PID"] == str(os.getpid())


def test_duplicate_live_bridge_is_rejected(monkeypatch):
    mtproto = importlib.import_module("channels.telegram_mtproto")

    class ExistingProc:
        pid = 777

        def poll(self):
            return None

    monkeypatch.setattr(mtproto, "_mtproto_proc", ExistingProc())

    with pytest.raises(RuntimeError, match="already running"):
        mtproto.start_mtproto()


def test_mtproto_peer_ids_convert_to_bot_api_chat_ids():
    bridge = importlib.import_module("channels.telegram_mtproto_bridge")

    PeerUser = type("PeerUser", (), {"__init__": lambda self, value: setattr(self, "user_id", value)})
    PeerChat = type("PeerChat", (), {"__init__": lambda self, value: setattr(self, "chat_id", value)})
    PeerChannel = type("PeerChannel", (), {"__init__": lambda self, value: setattr(self, "channel_id", value)})

    assert bridge._bot_api_chat_id(PeerUser(402314199)) == 402314199
    assert bridge._bot_api_chat_id(PeerChat(5459676079)) == -5459676079
    assert bridge._bot_api_chat_id(PeerChannel(3983157420)) == -1003983157420


def test_group_chat_id_matches_botbot_allowlist():
    bridge = importlib.import_module("channels.telegram_mtproto_bridge")
    PeerChat = type("PeerChat", (), {"__init__": lambda self, value: setattr(self, "chat_id", value)})

    configured = {"402314199", "-1003983157420", "-5459676079", "-5437945421"}
    converted = str(bridge._bot_api_chat_id(PeerChat(5459676079)))
    assert converted in configured


def test_self_message_filter_rejects_outgoing_and_own_sender():
    bridge = importlib.import_module("channels.telegram_mtproto_bridge")

    class Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    assert bridge._is_self_message(Obj(out=True, message=Obj(out=True)), 42)
    assert bridge._is_self_message(
        Obj(out=False, sender_id=42, message=Obj(out=False, sender_id=42)), 42
    )
    assert not bridge._is_self_message(
        Obj(out=False, sender_id=7, message=Obj(out=False, sender_id=7)), 42
    )


def test_bridge_handler_is_incoming_only():
    bridge_source = (ROOT / "channels" / "telegram_mtproto_bridge.py").read_text(
        encoding="utf-8"
    )
    assert "@client.on(events.NewMessage(incoming=True))" in bridge_source
    assert "if _is_self_message(event, me.id):" in bridge_source


def test_singleton_lock_rejects_second_owner(tmp_path):
    bridge = importlib.import_module("channels.telegram_mtproto_bridge")
    session = str(tmp_path / "session")
    bridge._lock_fd = None
    bridge._acquire_instance_lock(session)

    lock_fd = os.open(f"{session}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)
        os.close(bridge._lock_fd)
        bridge._lock_fd = None

    assert (tmp_path / "session.lock").stat().st_mode & 0o777 == 0o600
