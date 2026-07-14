import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import helper


def _envelope(reply="answer", actions=None, continuation=None):
    import json

    return json.dumps({
        "protocol": "omegaclaw.action.v1",
        "reply": None if reply is None else {"text": reply},
        "actions": actions or [],
        "continue": None if continuation is None else {"reason": continuation},
    })


def test_json_envelope_separates_reply_from_validated_actions():
    raw = _envelope(
        reply="I am checking.",
        actions=[{"name": "pin", "args": ["working"]}],
        continuation="finish",
    )
    assert helper.balance_parentheses_for_message(raw, True) == (
        '((send "I am checking.") (pin "working") '
        '(continue-thinking "finish"))'
    )


def test_json_envelope_rejects_unknown_action_without_partial_execution():
    raw = _envelope(
        reply="must not escape",
        actions=[{"name": "Unknown", "args": ["payload"]}],
    )
    result = helper.balance_parentheses_for_message(raw, True)
    assert "invalid internal action format" in result
    assert "must not escape" not in result


def test_json_envelope_requires_reply_during_user_burst():
    raw = _envelope(reply=None, actions=[{"name": "pin", "args": ["x"]}])
    result = helper.balance_parentheses_for_message(raw, True)
    assert "no user-facing reply" in result
    assert "(pin " not in result


def test_plain_text_on_user_burst_becomes_send():
    assert helper.balance_parentheses_for_message("A substantive answer.", True) == (
        '((send "A substantive answer."))'
    )


def test_canonical_and_legacy_action_batches_are_accepted():
    expected = '((send "ack") (continue-thinking "finish"))'
    assert helper.balance_parentheses_for_message(expected, True) == expected
    assert helper.balance_parentheses_for_message(
        '(send "ack") (continue-thinking "finish")', True
    ) == expected


def test_ship_of_theseus_malformed_reply_fails_closed():
    raw = (
        '((Ben "asked me to elaborate.") '
        '(The "Ship of Theseus reference follows.") '
        '(pin "memory continuity"))'
    )
    result = helper.balance_parentheses_for_message(raw, True)
    assert "invalid internal action format" in result
    assert "(pin " not in result
    assert "(Ben " not in result


def test_unknown_form_mixed_with_send_rejects_entire_batch():
    result = helper.balance_parentheses_for_message(
        '((send "would otherwise escape") (Unknown "payload"))', True
    )
    assert "invalid internal action format" in result
    assert "would otherwise escape" not in result


def test_nested_executable_argument_is_rejected_not_repaired():
    result = helper.balance_parentheses_for_message(
        '(send (shell "id"))', True
    )
    assert "invalid internal action format" in result
    assert '(shell "id")' not in result


def test_internal_only_user_response_becomes_delivery_diagnostic():
    result = helper.balance_parentheses_for_message('(pin "state")', True)
    assert "no user-facing reply" in result
    assert "(pin " not in result


def test_internal_only_background_response_remains_valid():
    assert helper.balance_parentheses_for_message('(pin "state")', False) == (
        '((pin "state"))'
    )


def test_intentional_noop_and_empty_response_remain_silent():
    assert helper.balance_parentheses_for_message("", True) == "()"
    assert helper.balance_parentheses_for_message(
        "No response from OpenClaw.", True
    ) == "()"


def test_zero_argument_extension_call_is_allowlisted():
    assert helper.balance_parentheses_for_message(
        '(extension-status)', False
    ) == '((extension-status))'


def test_more_than_five_actions_is_rejected():
    raw = " ".join(f'(pin "{i}")' for i in range(6))
    result = helper.balance_parentheses_for_message(raw, True)
    assert "invalid internal action format" in result
    assert "(pin " not in result


def test_message_newness_uses_correlation_not_repeated_text():
    assert helper.is_new_message("same", "corr-b", "same", "corr-a")
    assert not helper.is_new_message("same", "corr-a", "same", "corr-a")
    assert helper.is_new_message("different", "", "same", "")
    assert not helper.is_new_message("same", "", "same", "")

