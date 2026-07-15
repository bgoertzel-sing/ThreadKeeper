import json

import channels.telegram as telegram


def _message(text="hello", entities=None, reply_from=None):
    message = {
        "message_id": 7,
        "text": text,
        "entities": entities or [],
        "chat": {"id": -100, "type": "supergroup", "title": "room"},
        "from": {"id": 1, "first_name": "Ben", "is_bot": False},
    }
    if reply_from:
        message["reply_to_message"] = {"message_id": 6, "from": reply_from}
    return message


def _configure(monkeypatch):
    monkeypatch.setattr(telegram, "_self_bot_id", "99")
    monkeypatch.setenv("TG_BOT_USERNAME", "ProtoMegaBot")
    monkeypatch.setenv("TG_BOT_REGISTRY_JSON", json.dumps({"entries": [
        {"telegram_user_id": "99", "telegram_username": "ProtoMegaBot", "agent_instance_id": "mega", "platform": "omegaclaw", "display_name": "Mega", "social_role": "always-attending", "important_bots": [55]},
        {"telegram_user_id": "55", "telegram_username": "OtherBot", "agent_instance_id": "other", "platform": "openclaw", "display_name": "Other", "social_role": "always-attending"},
    ]}))


def test_registry_derives_bot_flags(monkeypatch):
    _configure(monkeypatch)
    message = _message(reply_from={"id": 55, "is_bot": False, "first_name": "Other"})
    result = telegram._build_inbound_identity(message, message["chat"], message["from"], "hello")
    assert result["addressee_classification"] == "SECONDARY"
    assert result["inbound_envelope"]["reply_to"]["sender_is_bot"] is True


def test_mention_wins_conflicting_reply(monkeypatch):
    _configure(monkeypatch)
    message = _message("@ProtoMegaBot answer", [{"type": "mention", "offset": 0, "length": 13}], {"id": 55, "is_bot": True})
    result = telegram._build_inbound_identity(message, message["chat"], message["from"], message["text"])
    assert result["addressee_classification"] == "DIRECT"
    assert result["reinforced"] is False


def test_agreeing_mention_and_reply_are_reinforced(monkeypatch):
    _configure(monkeypatch)
    message = _message("@ProtoMegaBot answer", [{"type": "mention", "offset": 0, "length": 13}], {"id": 99, "is_bot": True})
    result = telegram._build_inbound_identity(message, message["chat"], message["from"], message["text"])
    assert result["addressee_classification"] == "DIRECT"
    assert result["reinforced"] is True


def test_unaddressed_room_message_is_group(monkeypatch):
    _configure(monkeypatch)
    message = _message()
    result = telegram._build_inbound_identity(message, message["chat"], message["from"], "hello")
    assert result["addressee_classification"] == "GROUP"
