import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("auth", types.SimpleNamespace())
spec = importlib.util.spec_from_file_location("telegram_under_test", ROOT / "channels" / "telegram.py")
telegram = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telegram)


def message_with_mentions(text, handles, reply_to_bot=False):
    entities = []
    search_from = 0
    for handle in handles:
        start = text.index(handle, search_from)
        entities.append({"type": "mention", "offset": start, "length": len(handle)})
        search_from = start + len(handle)
    message = {"entities": entities}
    if reply_to_bot:
        message["reply_to_message"] = {"from": {"is_bot": True, "id": 123}}
    return message


class TelegramAddressFilterTest(unittest.TestCase):
    def test_self_mention_wins_over_collaborator_mention(self):
        text = "@Protomegabot write the CLA paper using results from @Protocosmobot"
        message = message_with_mentions(text, ["@Protomegabot", "@Protocosmobot"])
        self.assertFalse(telegram._should_skip_group_response(message, text))

    def test_other_bot_only_is_skipped(self):
        text = "@Protocosmobot please provide the results"
        message = message_with_mentions(text, ["@Protocosmobot"])
        self.assertTrue(telegram._should_skip_group_response(message, text))

    def test_self_only_and_unaddressed_are_not_skipped(self):
        text = "@Protomegabot please write the paper"
        self.assertFalse(telegram._should_skip_group_response(
            message_with_mentions(text, ["@Protomegabot"]), text))
        self.assertFalse(telegram._should_skip_group_response({"entities": []}, "hello group"))

    def test_self_mention_wins_even_when_replying_to_other_bot(self):
        text = "@ProtomegaTron please take this"
        message = message_with_mentions(text, ["@ProtomegaTron"], reply_to_bot=True)
        self.assertFalse(telegram._should_skip_group_response(message, text))


if __name__ == "__main__":
    unittest.main()
