import channels.telegram as telegram


def test_control_results_are_suppressed():
    assert telegram._telegram_publish_gate("SUPPRESS") == (False, "control_result")
    assert telegram._telegram_publish_gate("  NO_REPLY\n") == (False, "control_result")


def test_acknowledgements_and_silence_explanations_are_suppressed():
    assert telegram._telegram_publish_gate("Acknowledged.")[0] is False
    assert telegram._telegram_publish_gate("I'm staying quiet because this is for another bot.")[0] is False


def test_substantive_text_is_published():
    assert telegram._telegram_publish_gate("The classifier is now mention-first.") == (True, "reply")
