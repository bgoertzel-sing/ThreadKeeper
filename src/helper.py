from collections import deque
import json
import re
from datetime import datetime

TS_RE = re.compile(r'^\("(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"')
LLM_COMMANDS = {
    "append-file",
    "continue-thinking",
    "episodes",
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


_ALLOWED_INTERNAL_ACTIONS = LLM_COMMANDS | {"extension-status"}
_MAX_ACTIONS = 5
_ERROR_INVALID = "invalid internal action format"
_ERROR_NO_REPLY = "no user-facing reply"


def extract_timestamp(line):
    m = TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


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
    return _normalized_response_text(value) in NOOP_LLM_RESPONSES


def _looks_like_known_command_response(s):
    lines = [line.strip() for line in s.splitlines() if line.strip()]
    if not lines:
        return False
    return any(_is_known_command(line) for line in lines)



def _try_parse_json_envelope(s, is_new_message=False):
    """Try to parse an omegaclaw.action.v1 JSON envelope.
    Returns (result_str, error_str). Both None if not a JSON envelope."""
    s_stripped = s.strip()
    if not s_stripped.startswith('{'):
        return None, None
    try:
        obj = json.loads(s_stripped)
    except (json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    if obj.get('protocol') != 'omegaclaw.action.v1':
        return None, None
    reply = obj.get('reply')
    actions = obj.get('actions', [])
    cont = obj.get('continue')
    parts = []
    has_send_in_reply = False
    if reply is not None and isinstance(reply, dict):
        text = reply.get('text', '')
        if text and not _is_suppressed_noop_response(text):
            parts.append(f'(send {json.dumps(text, ensure_ascii=False)})')
            has_send_in_reply = True
    # During a new message burst, require at least one user-facing send
    if _truthy(is_new_message) and not has_send_in_reply:
        # Check if any action is a send
        has_send_action = any(
            isinstance(a, dict) and a.get('name') == 'send'
            for a in actions
        )
        if not has_send_action:
            return None, _ERROR_NO_REPLY
    for action in actions:
        if not isinstance(action, dict):
            return None, _ERROR_INVALID
        name = action.get('name', '')
        args = action.get('args', [])
        if name not in _ALLOWED_INTERNAL_ACTIONS:
            return None, _ERROR_INVALID
        arg_str = ' '.join(json.dumps(a, ensure_ascii=False) for a in args) if args else ''
        if arg_str:
            parts.append(f'({name} {arg_str})')
        else:
            parts.append(f'({name})')
    if cont is not None and isinstance(cont, dict):
        reason = cont.get('reason', '')
        if reason:
            parts.append(f'(continue-thinking {json.dumps(reason, ensure_ascii=False)})')
        else:
            parts.append('(continue-thinking)')
    if not parts:
        return '()', None
    if len(parts) > _MAX_ACTIONS:
        return None, _ERROR_INVALID
    return '(' + ' '.join(parts) + ')', None


def _parse_sexpr_tokens(s):
    """Parse a string into a list of top-level s-expression strings."""
    s = s.strip()
    if not s:
        return []
    results = []
    i = 0
    while i < len(s):
        while i < len(s) and s[i] in (' ', '\t', '\n', '\r'):
            i += 1
        if i >= len(s):
            break
        if s[i] != '(':
            return None
        depth = 0
        in_str = False
        escaped = False
        start = i
        while i < len(s):
            ch = s[i]
            if escaped:
                escaped = False
                i += 1
                continue
            if ch == '\\' and in_str:
                escaped = True
                i += 1
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        results.append(s[start:i+1])
                        i += 1
                        break
            i += 1
        else:
            return None
    return results


def _validate_sexpr_batch(s, is_new_message=False):
    """Validate a batch of s-expressions. Returns (result_str, error_str)."""
    sexprs = _parse_sexpr_tokens(s)
    if sexprs is None:
        return None, None
    if not sexprs:
        return None, None
    # If we got exactly one token, try stripping outer parens and re-parsing.
    # This handles the case where the batch is wrapped in an extra layer: ((a) (b))
    if len(sexprs) == 1:
        inner = _strip_outer_parens(sexprs[0].strip())
        re_parsed = _parse_sexpr_tokens(inner)
        if re_parsed is not None and len(re_parsed) > 1:
            sexprs = re_parsed
    validated = []
    has_send = False
    for sexpr in sexprs:
        inner = _strip_outer_parens(sexpr.strip())
        parts = inner.split(maxsplit=1)
        if not parts:
            return None, _ERROR_INVALID
        cmd = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd not in _ALLOWED_INTERNAL_ACTIONS:
            return None, _ERROR_INVALID
        if rest and rest.startswith('(') and rest.endswith(')'):
            return None, _ERROR_INVALID
        if cmd == 'send':
            has_send = True
            decoded = _decode_quoted_arg(rest) if rest.startswith('"') else None
            check_text = decoded if decoded is not None else rest
            if _is_suppressed_noop_response(check_text):
                continue
        validated.append(sexpr.strip())
    if len(validated) > _MAX_ACTIONS:
        return None, _ERROR_INVALID
    if _truthy(is_new_message) and not has_send:
        return None, _ERROR_NO_REPLY
    return '(' + ' '.join(validated) + ')', None


def is_new_message(text_a, corr_a, text_b, corr_b):
    """Return True if message A is a new message relative to B.

    Uses correlation ID as the primary key.  If both correlation IDs
    are empty (legacy/unknown), fall back to text comparison.
    """
    if corr_a or corr_b:
        return corr_a != corr_b
    return text_a != text_b


def balance_parentheses_for_message(s, is_new_message=False):
    s = str(s).replace("_quote_", '"').replace("_newline_", "\n")
    if _is_suppressed_noop_response(s):
        return "()"
    # Try JSON envelope parsing first
    parsed, err = _try_parse_json_envelope(s, is_new_message)
    if parsed is not None:
        return parsed
    if err is not None:
        return err
    # Try s-expression batch validation
    sparsed, serr = _validate_sexpr_batch(s, is_new_message)
    if sparsed is not None:
        return sparsed
    if serr is not None:
        return serr
    if _truthy(is_new_message) and not _looks_like_known_command_response(s):
        text = s.strip()
        if text:
            return f'((send {json.dumps(text, ensure_ascii=False)}))'
    return balance_parentheses(s)


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


def memory_file_path(filename: str = "history.metta") -> str:
    """Return the full path to a memory file.

    Honours the OMEGACLAW_MEMORY_DIR environment variable when set;
    otherwise falls back to the legacy default location.
    """
    import os
    staging = os.environ.get("OMEGACLAW_MEMORY_DIR")
    if staging:
        return os.path.join(staging, filename)
    return os.path.join("repos", "OmegaClaw-Core", "memory", filename)
