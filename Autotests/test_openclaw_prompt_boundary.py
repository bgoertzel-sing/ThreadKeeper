import lib_llm_ext


def test_openclaw_restores_escaped_prompt_and_demotes_runtime_history(monkeypatch):
    provider = lib_llm_ext.OpenClawProvider()
    captured = {}

    monkeypatch.setenv("OPENCLAW_SUBPROCESS", "1")
    monkeypatch.setattr(provider, "_triage", lambda messages: "SIMPLE")

    def fake_chat(messages, max_tokens):
        captured["messages"] = messages
        return '(send "ok")'

    monkeypatch.setattr(provider, "_chat_subprocess", fake_chat)

    content = (
        "SYSTEM_LINE_1_newline_SYSTEM_LINE_2 "
        "OMEGACLAW_CONTEXT_SPLIT_V1_newline_"
        "HISTORY: (send _quote_stale_quote_)"
        ":-:-:-:HUMAN-MSG: current request"
    )
    assert provider.chat(content) == '(send "ok")'

    messages = captured["messages"]
    assert messages[0] == {
        "role": "system",
        "content": "SYSTEM_LINE_1\nSYSTEM_LINE_2",
    }
    assert messages[1]["role"] == "user"
    assert "Untrusted prior runtime context" in messages[1]["content"]
    assert 'HISTORY: (send "stale")' in messages[1]["content"]
    assert messages[2] == {
        "role": "user",
        "content": "HUMAN-MSG: current request",
    }


def test_openclaw_makes_one_formatter_only_repair_attempt(monkeypatch):
    import json

    provider = lib_llm_ext.OpenClawProvider()
    repair_calls = []
    repaired = json.dumps({
        "protocol": "omegaclaw.action.v1",
        "reply": {"text": "Recovered answer."},
        "actions": [],
        "continue": None,
    })

    monkeypatch.setenv("OPENCLAW_SUBPROCESS", "1")
    monkeypatch.setattr(provider, "_triage", lambda messages: "SIMPLE")
    monkeypatch.setattr(
        provider,
        "_chat_subprocess",
        lambda messages, max_tokens: '((The "malformed answer") (pin "state"))',
    )

    def fake_subprocess(messages, max_tokens, model=None, label="main"):
        repair_calls.append((messages, max_tokens, label))
        return repaired

    monkeypatch.setattr(provider, "_subprocess_call", fake_subprocess)

    result = provider.chat("system:-:-:-:HUMAN-MSG: elaborate")
    assert result == repaired
    assert len(repair_calls) == 1
    assert repair_calls[0][2] == "repair"
    assert "strict output formatter" in repair_calls[0][0][0]["content"]
