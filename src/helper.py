from collections import deque
import json
import re
from datetime import datetime

TS_RE = re.compile(r'^\("(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"')
LLM_COMMANDS = {
    "append-file",
    "continue-thinking",
    "deontic-conclude",
    "deontic-conflicts",
    "deontic-norms",
    "directive-board",
    "directive-next",
    "directive-status",
    "directive-summary",
    "episodes",
    "extension-status",
    "goalchainer-decide",
    "goalchainer-solve",
    "goalchainer-status",
    "metta",
    "pin",
    "query",
    "read-file",
    "remember",
    "search",
    "send",
    "shell",
    "tavily-search",
    "technical-analysis",
    "write-file",
}

ZERO_ARG_LLM_COMMANDS = {
    "extension-status",
    "goalchainer-status",
}

TWO_ARG_LLM_COMMANDS = {
    "append-file",
    "write-file",
}

MAX_LLM_COMMANDS_PER_RESPONSE = 5
MODEL_ACTION_PROTOCOL = "omegaclaw.action.v1"

INVALID_ACTION_RESPONSE_MESSAGE = (
    "ProtoMegaTron produced an invalid internal action format; no proposed "
    "actions were executed. Please retry while the pipeline diagnostic is "
    "recorded."
)

MISSING_SEND_RESPONSE_MESSAGE = (
    "ProtoMegaTron produced internal actions but no user-facing reply; no "
    "proposed actions were executed. Please retry while the pipeline "
    "diagnostic is recorded."
)


def extract_timestamp(line):
    m = TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def is_new_message(received_text, correlation_id, previous_text, previous_correlation_id):
    """Identify ingress by immutable correlation ID, with legacy text fallback."""
    received = str(received_text or "")
    if not received:
        return False
    correlation = str(correlation_id or "")
    if correlation:
        return correlation != str(previous_correlation_id or "")
    return received != str(previous_text or "")


def around_time(needle_time_str, k):
    needle_time_str = needle_time_str.replace(r'\"', '').replace('"', '').strip()
    filename = "repos/OmegaClaw-Core/memory/history.metta"
    target = datetime.strptime(needle_time_str, "%Y-%m-%d %H:%M:%S")
    best_lineno = None
    best_line = None
    best_diff = None
    buffer = []
    best_idx = None
    with open(filename, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            buffer.append((lineno, line))
            ts = extract_timestamp(line)
            if ts is None:
                continue
            diff = abs((ts - target).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_lineno = lineno
                best_line = line
                best_idx = len(buffer) - 1
    if best_lineno is None:
        return
    start = max(0, best_idx - k)
    end = min(len(buffer), best_idx + k + 1)
    ret = ""
    for lineno, line in buffer[start:end]:
        ret += f"{lineno}:{line}"
    return ret


def _strip_outer_parens(line):
    if line.startswith("(") and line.endswith(")"):
        return line[1:-1].strip()
    return line


def _get_command_name(line):
    normalized = line.strip()
    while normalized.startswith("("):
        normalized = normalized[1:].lstrip()
    while normalized.endswith(")"):
        normalized = normalized[:-1].rstrip()
    if not normalized:
        return ""
    return normalized.split(maxsplit=1)[0]


def _is_known_command(line):
    return _get_command_name(line) in LLM_COMMANDS


def _decode_quoted_arg(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _merge_send_continuations(lines):
    merged = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if _get_command_name(line) != "send":
            merged.append(line)
            idx += 1
            continue

        send_wrapped = line.strip().startswith("(")
        head = line.strip()
        while head.startswith("("):
            head = head[1:].lstrip()
        parts = head.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        decoded_payload = _decode_quoted_arg(payload) if payload.startswith('"') else None
        text = decoded_payload if decoded_payload is not None else payload

        idx += 1
        continuations = []
        while idx < len(lines) and not _is_known_command(lines[idx]):
            continuation = lines[idx].strip()
            if send_wrapped and continuation.endswith(")"):
                continuation = continuation[:-1].rstrip()
                continuations.append(continuation)
                idx += 1
                break
            continuations.append(continuation)
            idx += 1

        if continuations:
            if text:
                text = text + "\n" + "\n".join(continuations)
            else:
                text = "\n".join(continuations)
            merged.append(f"send {json.dumps(text, ensure_ascii=False)}")
        else:
            merged.append(line)
    return merged


NOOP_LLM_RESPONSES = {
    "no response from openclaw",
    "no response from openclaw.",
}


def _truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalized_response_text(value):
    text = str(value).replace("_quote_", '"').replace("_newline_", "\n")
    return re.sub(r"\s+", " ", text.strip()).strip('"').lower()


def _is_suppressed_noop_response(value):
    if _normalized_response_text(value) in NOOP_LLM_RESPONSES:
        return True
    text = str(value or "").strip()
    match = re.fullmatch(r"\(*send\s+(\"(?:\\.|[^\"])*\")\)*", text, re.DOTALL)
    if not match:
        return False
    decoded = _decode_quoted_arg(match.group(1))
    return _normalized_response_text(decoded) in NOOP_LLM_RESPONSES


def _looks_like_known_command_response(s):
    lines = [line.strip() for line in s.splitlines() if line.strip()]
    if not lines:
        return False
    return any(_is_known_command(line) for line in lines)


def _looks_like_action_syntax(s):
    text = str(s or "").lstrip()
    return text.startswith("(") or _looks_like_known_command_response(text)


def _parse_json_string_args(rest):
    """Parse a whitespace-separated sequence of JSON string literals.

    Model-proposed actions are executable, so their boundary is deliberately
    stricter than the legacy repair code: nested expressions and bare atoms are
    rejected before MeTTa sees them.
    """
    decoder = json.JSONDecoder()
    args = []
    remaining = rest.lstrip()
    while remaining:
        try:
            value, end = decoder.raw_decode(remaining)
        except Exception:
            return None
        if not isinstance(value, str):
            return None
        args.append(value)
        remaining = remaining[end:].lstrip()
    return args


def _validated_action_names(response):
    """Return top-level action names, or ``None`` for an invalid batch.

    The accepted representation is one outer list containing up to five
    allowlisted calls. Every argument must be a quoted JSON string. This check
    happens before ``sread``/``eval`` so a valid action cannot smuggle a nested
    executable form and an unknown form cannot execute alongside valid calls.
    """
    text = str(response or "").strip()
    if text == "()":
        return []
    if len(text) < 4 or not text.startswith("(") or not text.endswith(")"):
        return None

    inner = text[1:-1].strip()
    names = []
    idx = 0
    while idx < len(inner):
        while idx < len(inner) and inner[idx].isspace():
            idx += 1
        if idx >= len(inner):
            break
        if inner[idx] != "(":
            return None
        block, end = _read_balanced_block(inner, idx)
        if end <= idx or not block.endswith(")"):
            return None
        idx = end

        body = block[1:-1].strip()
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)(?:\s+(.*))?$", body, re.DOTALL)
        if not match:
            return None
        name = match.group(1)
        if name not in LLM_COMMANDS:
            return None
        args = _parse_json_string_args(match.group(2) or "")
        if args is None:
            return None
        expected = 0 if name in ZERO_ARG_LLM_COMMANDS else 2 if name in TWO_ARG_LLM_COMMANDS else 1
        if len(args) != expected:
            return None
        names.append(name)
        if len(names) > MAX_LLM_COMMANDS_PER_RESPONSE:
            return None

    return names


def _wrap_top_level_calls(text):
    """Wrap one or more complete top-level forms in the executor list.

    This supports the legacy `(send "...") (pin "...")` representation
    without repairing or quoting anything inside a proposed action.
    """
    source = str(text or "").strip()
    forms = []
    idx = 0
    while idx < len(source):
        while idx < len(source) and source[idx].isspace():
            idx += 1
        if idx >= len(source):
            break
        if source[idx] != "(":
            return None
        block, end = _read_balanced_block(source, idx)
        if end <= idx or not block.endswith(")"):
            return None
        forms.append(block)
        idx = end
    if not forms:
        return None
    return "(" + " ".join(forms) + ")"


def _fallback_send(message):
    return f'((send {json.dumps(message, ensure_ascii=False)}))'


def _normalize_json_action_envelope(text):
    """Validate the preferred model protocol and translate it to MeTTa calls.

    User-facing text is a first-class field rather than a `send` tool proposed
    by the model. Only allowlisted, arity-checked string actions cross into the
    executable MeTTa representation.
    """
    try:
        payload = json.loads(text)
    except Exception:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "protocol", "reply", "actions", "continue"
    }:
        return None
    if payload["protocol"] != MODEL_ACTION_PROTOCOL:
        return None

    calls = []
    reply = payload["reply"]
    if reply is not None:
        if not isinstance(reply, dict) or set(reply) != {"text"}:
            return None
        reply_text = reply["text"]
        if not isinstance(reply_text, str) or not reply_text.strip():
            return None
        calls.append(("send", [reply_text]))

    actions = payload["actions"]
    if not isinstance(actions, list):
        return None
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"name", "args"}:
            return None
        name = action["name"]
        args = action["args"]
        if (
            not isinstance(name, str)
            or name not in LLM_COMMANDS
            or name in {"send", "continue-thinking"}
            or not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
        ):
            return None
        expected = 0 if name in ZERO_ARG_LLM_COMMANDS else 2 if name in TWO_ARG_LLM_COMMANDS else 1
        if len(args) != expected:
            return None
        calls.append((name, args))

    continuation = payload["continue"]
    if continuation is not None:
        if not isinstance(continuation, dict) or set(continuation) != {"reason"}:
            return None
        reason = continuation["reason"]
        if not isinstance(reason, str) or not reason.strip():
            return None
        calls.append(("continue-thinking", [reason]))

    if not calls or len(calls) > MAX_LLM_COMMANDS_PER_RESPONSE:
        return None

    encoded_calls = []
    for name, args in calls:
        encoded_args = " ".join(json.dumps(arg, ensure_ascii=False) for arg in args)
        encoded_calls.append(f"({name}{(' ' + encoded_args) if encoded_args else ''})")
    normalized = "(" + " ".join(encoded_calls) + ")"
    return normalized, [name for name, _args in calls]


def balance_parentheses_for_message(s, require_send=False):
    s = str(s).replace("_quote_", '"').replace("_newline_", "\n")
    if _is_suppressed_noop_response(s):
        return "()"
    text = s.strip()
    if not text:
        return "()"

    send_required = _truthy(require_send)
    if text.startswith("{"):
        parsed_envelope = _normalize_json_action_envelope(text)
        if parsed_envelope is None:
            return _fallback_send(INVALID_ACTION_RESPONSE_MESSAGE) if send_required else "()"
        normalized, names = parsed_envelope
        if send_required and "send" not in names:
            return _fallback_send(MISSING_SEND_RESPONSE_MESSAGE)
        return normalized

    if send_required and not _looks_like_action_syntax(text):
        return _fallback_send(text)

    # Accept an already canonical outer action list. Otherwise normalize the
    # legacy one-call-per-line format, then validate the complete result.
    names = _validated_action_names(text)
    if names is not None:
        normalized = text
    elif text.lstrip().startswith("("):
        normalized = _wrap_top_level_calls(text) or text
    else:
        normalized = balance_parentheses(text)
    names = _validated_action_names(normalized)
    if names is None:
        return _fallback_send(INVALID_ACTION_RESPONSE_MESSAGE) if send_required else "()"
    if send_required and "send" not in names:
        return _fallback_send(MISSING_SEND_RESPONSE_MESSAGE)
    return normalized


def balance_parentheses(s):
    s = str(s).replace("_quote_", '"').replace("_newline_", "\n")
    sexprs = []
    special_two_arg_cmds = {"write-file", "append-file"}
    lines = [line.strip() for line in s.splitlines() if line.strip()]
    lines = _merge_send_continuations(lines)
    for line in lines:
        if line.startswith("(-"):
            line = "(pin -" + line[2:]
        elif line.startswith("-"):
            line = "pin " + line
        # remove one outer (...) if present
        line = _strip_outer_parens(line)
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        cmd = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "send":
            decoded_rest = _decode_quoted_arg(rest) if rest.startswith('"') else None
            if _is_suppressed_noop_response(decoded_rest if decoded_rest is not None else rest):
                continue
        if cmd in special_two_arg_cmds:
            if not rest:
                sexprs.append(f"({cmd})")
                continue
            # filename is first token unless already quoted
            if rest.startswith('"'):
                end = 1
                escaped = False
                while end < len(rest):
                    ch = rest[end]
                    if ch == '"' and not escaped:
                        break
                    escaped = (ch == '\\' and not escaped)
                    if ch != '\\':
                        escaped = False
                    end += 1
                if end < len(rest) and rest[end] == '"':
                    filename = rest[:end+1]
                    content = rest[end+1:].strip()
                else:
                    filename = '"' + rest[1:].replace('"', '\\"') + '"'
                    content = ""
            else:
                split_rest = rest.split(maxsplit=1)
                filename = '"' + split_rest[0].replace('"', '\\"') + '"'
                content = split_rest[1].strip() if len(split_rest) > 1 else ""
            if content:
                if content.startswith('"') and content.endswith('"'):
                    sexprs.append(f"({cmd} {filename} {content})")
                else:
                    content = content.replace('"', '\\"')
                    sexprs.append(f'({cmd} {filename} "{content}")')
            else:
                sexprs.append(f"({cmd} {filename})")
            continue
        if rest:
            if rest.startswith('"') and rest.endswith('"'):
                sexprs.append(f"({cmd} {rest})")
            else:
                rest = rest.replace('"', '\\"')
                sexprs.append(f'({cmd} "{rest}")')
        else:
            sexprs.append(f"({cmd})")
    ret = " ".join(sexprs)
    return "(" + ret + ")"


def normalize_string(x):
    try:
        if isinstance(x, bytes):
            return x.decode("utf-8", errors="ignore")
        return str(x).encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return str(x)


def _read_balanced_block(text, start):
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1], idx + 1
    return text[start:], len(text)


def compact_skill_results(value):
    """Remove duplicate COMMAND_RETURN blocks before feeding results back.

    PeTTa/MeTTa evaluation can produce duplicate alternatives for a single
    side-effecting skill call.  The side-effect is already deduped at the skill
    boundary where possible, but echoing dozens of identical COMMAND_RETURNs in
    LAST_SKILL_USE_RESULTS pollutes the next prompt and can induce stale-output
    loops.  Preserve first occurrences and all non-duplicate diagnostics.
    """
    text = normalize_string(value)
    marker = "(COMMAND_RETURN:"
    out = []
    seen = set()
    idx = 0
    while idx < len(text):
        pos = text.find(marker, idx)
        if pos < 0:
            out.append(text[idx:])
            break
        out.append(text[idx:pos])
        block, end = _read_balanced_block(text, pos)
        key = re.sub(r"\s+", " ", block).strip()
        if key not in seen:
            seen.add(key)
            out.append(block)
        idx = end
    return "".join(out)


def test_balance_parenthesis():
    assert balance_parentheses('(write-file test.txt hello world)') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(append-file test.txt hello world)') == '((append-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file "test.txt" hello world)') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file "test.txt" "hello world")') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file test.txt "hello world")') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(send test.xt hello world)') == '((send "test.xt hello world"))'
    assert balance_parentheses('write-file test.txt hello world') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('append-file test.txt hello world') == '((append-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file "test.txt" hello world') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file "test.txt" "hello world"') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file test.txt "hello world"') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('send test.xt hello world') == '((send "test.xt hello world"))'
    assert balance_parentheses('send Here are the planets:\n1. Mercury\n2. Venus') == '((send "Here are the planets:\\n1. Mercury\\n2. Venus"))'
    assert balance_parentheses('send Here are the options:\n- MacBook Air\n- ThinkPad X1\npin done') == '((send "Here are the options:\\n- MacBook Air\\n- ThinkPad X1") (pin "done"))'
    assert balance_parentheses('send "Plain text version:"\n**Mars** - red planet\nNote: Pluto is a dwarf planet') == '((send "Plain text version:\\n**Mars** - red planet\\nNote: Pluto is a dwarf planet"))'
    assert balance_parentheses('(send Here are the planets:\n1. Mercury\n2. Venus)') == '((send "Here are the planets:\\n1. Mercury\\n2. Venus"))'
    assert balance_parentheses('send "hello" world') == '((send "\\"hello\\" world"))'
    # bare "()" lines yield no tokens after _strip_outer_parens and must be skipped, not crash
    assert balance_parentheses('()') == '()'
    assert balance_parentheses('') == '()'
    assert balance_parentheses('   ') == '()'
    assert balance_parentheses('()\nsend hello') == '((send "hello"))'
    assert balance_parentheses_for_message('No response from OpenClaw.', True) == '()'
    assert balance_parentheses_for_message(' no   response from openclaw ', True) == '()'
    assert balance_parentheses_for_message('(send "No response from OpenClaw.")', True) == '()'
    assert balance_parentheses('send No response from OpenClaw.') == '()'
    assert balance_parentheses('(send "No response from OpenClaw.")') == '()'


if __name__ == "__main__":
    test_balance_parenthesis()
