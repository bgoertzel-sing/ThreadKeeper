"""Subagent dispatch primitive for OmegaClaw.

The `dispatch` function below is the Python target of the MeTTa
`(delegate goal tools persona max_turns)` skill defined in
src/skills.metta. It runs a bounded, narrowly-scoped child LLM loop
against a configurable provider/model/endpoint (per the persona's
JSON config) and returns a single-string digest to the parent loop.

Architectural intent: pair the foundation-model parent (routing
judgment) with a narrow specialist subagent (execution) chosen per
task. The persona config binds each subagent to its own
provider/model/endpoint — typically a smaller, cheaper, or more-
specialized model than the parent runs. See
docs/reference-skills-subagent.md for the skill reference and
docs/tutorial-09-subagents.md for the end-to-end walkthrough.

Provider integration uses lib_llm_ext.AIProvider — instantiated
fresh per dispatch from the persona's JSON config. Stays inside
the existing class abstraction; does not mutate
lib_llm_ext._provider_registry.

The minimal response-cleanup logic below (strip <think> blocks,
strip markdown fences, parse line-leading s-exprs) keeps the
dispatch primitive independent of any specific format-adapter
beyond what reasoning models routinely emit.

v1 scope (documented in docs/reference-skills-subagent.md):
- Tool registry: search, read-file, write-file, append-file, shell
  (restricted), tavily-search, technical-analysis. Excluded:
  remember, query, episodes, pin, metta, send, delegate.
- One dispatch at a time, synchronously.
- No subagent → subagent recursion.
- Digest returned as a single-line string, capped per
  OMEGACLAW_SUBAGENT_MAX_DIGEST_CHARS (default 2000).
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
import tempfile
import uuid
import hashlib
import contextlib
try:
    import fcntl
except Exception:  # pragma: no cover - non-Unix fallback
    fcntl = None

# Worker-call usage log — SAME file the parent loop + dashboard read, so
# delegated work shows up on the ThreadKeeper mesh's Local Worker tile.
_USAGE_LOG_PATH = os.path.join(
    os.environ.get("MEMORY_DIR", "/PeTTa/repos/OmegaClaw-Core/memory"),
    "usage.jsonl",
)


def _log_worker_usage(model, in_tok, out_tok):
    """Append a worker LLM call to usage.jsonl. Never raises."""
    try:
        rec = {"ts": time.time(), "model": model,
               "input_tokens": int(in_tok or 0), "output_tokens": int(out_tok or 0)}
        with open(_USAGE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------
# ThreadKeeper escalation gate.
#
# Delegations to a CLOUD specialist are the expensive node — so before we
# dispatch one, we consult ThreadKeeper's budget policy (which lives in
# src/escalation.metta, evaluated through PeTTa by BudgetTracker). LOCAL
# delegations (Ollama on .41/.248) are free and always proceed ungated.
#
# Fail-CLOSED by default: if the policy can't be evaluated (module missing,
# etc.), cloud delegation is refused. Operators may temporarily restore the
# prototype's historical fail-open behavior with
# OMEGACLAW_SUBAGENT_BUDGET_FALLBACK=allow, but the safe default is deny.
# ----------------------------------------------------------------------
_LOCAL_NODE_ROLES = frozenset(["worker_loop", "control_loop", "local", "worker"])
_CLOUD_NODE_ROLES = frozenset(["cloud_specialist", "cloud", "specialist", "adjudicator"])
_OPENAI_COMPAT_ENDPOINTS = frozenset(["openai_compatible", "openai-compatible", "openai", "cloud"])
_OLLAMA_ENDPOINTS = frozenset(["ollama_native", "ollama-native", "ollama"])


def _node_role(cfg):
    """Return the explicit persona node role.

    Older prototypes guessed cloud/local status from model/base_url strings.
    ThreadKeeper hardening now requires persona metadata to say what kind of
    worker this is, so safety gates do not depend on fragile endpoint names.
    """
    return (cfg.get("node_role") or "").strip().lower()


def _endpoint_kind(cfg):
    """Return explicit provider transport metadata for worker LLM calls.

    `endpoint_kind` is preferred. For compatibility with existing persona files,
    provider names are accepted only as metadata labels (never by base_url/model
    substring). This keeps cloud/local budget classification on `node_role`.
    """
    kind = (cfg.get("endpoint_kind") or cfg.get("provider_transport") or cfg.get("provider") or "").strip().lower()
    if kind in _OLLAMA_ENDPOINTS:
        return "ollama_native"
    if kind in _OPENAI_COMPAT_ENDPOINTS:
        return "openai_compatible"
    return kind


def _persona_is_cloud(cfg):
    """Classify a persona from explicit `node_role` metadata only."""
    role = _node_role(cfg)
    if role in _CLOUD_NODE_ROLES:
        return True
    if role in _LOCAL_NODE_ROLES:
        return False
    raise ValueError(
        "persona config has invalid node_role; expected one of "
        f"{sorted(_LOCAL_NODE_ROLES | _CLOUD_NODE_ROLES)}"
    )


def _escalation_policy_path():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("OMEGACLAW_ESCALATION_METTA_PATH", ""),
        os.path.join(here, "escalation.metta"),
        os.path.join(here, "..", "src", "escalation.metta"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.realpath(os.path.abspath(path))
    return ""


def _escalation_policy_integrity():
    """Return (ok, reason) for optional escalation.metta integrity pin.

    Operators can set OMEGACLAW_ESCALATION_METTA_SHA256 to the trusted policy
    hash. When set, mismatches fail closed before any cloud delegation.
    """
    expected = os.environ.get("OMEGACLAW_ESCALATION_METTA_SHA256", "").strip().lower()
    if not expected:
        return (True, "no escalation policy hash configured")
    path = _escalation_policy_path()
    if not path:
        return (False, "escalation.metta not found for integrity check")
    try:
        with open(path, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
    except Exception as e:
        return (False, f"escalation.metta integrity read failed: {type(e).__name__}: {e}")
    if actual != expected:
        return (False, f"escalation.metta integrity mismatch at {path}")
    return (True, f"escalation.metta integrity ok at {path}")


def _escalation_gate(cfg, thread_id="default"):
    """Return (allowed: bool, reason: str). Local → always allow.
    Cloud → ThreadKeeper's MeTTa policy decides. Never raises (fail-closed by
    default, configurable with OMEGACLAW_SUBAGENT_BUDGET_FALLBACK=allow)."""
    if not _persona_is_cloud(cfg):
        return (True, "local node — no budget gate")

    def fallback(reason):
        mode = os.environ.get(
            "OMEGACLAW_SUBAGENT_BUDGET_FALLBACK", "deny"
        ).strip().lower()
        if mode in ("allow", "open", "fail-open", "true", "1"):
            return (True, f"{reason} — fail-open allow by explicit fallback")
        return (False, f"{reason} — fail-closed deny")

    integrity_ok, integrity_reason = _escalation_policy_integrity()
    if not integrity_ok:
        return (False, integrity_reason)

    try:
        # Locate threadkeeper_budget.py: shipped beside this overlay module,
        # or in the repo src/. Add whichever dir holds it to sys.path.
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            here,                                            # overlay/
            os.path.join(here, "..", "src"),                 # repo src/
            os.environ.get("THREADKEEPER_SRC_DIR", ""),
        ]
        BudgetTracker = None
        for d in candidates:
            if d and os.path.isfile(os.path.join(d, "threadkeeper_budget.py")):
                if d not in sys.path:
                    sys.path.insert(0, d)
                from threadkeeper_budget import BudgetTracker  # noqa
                break
        if BudgetTracker is None:
            return fallback("budget module unavailable")
        bt = BudgetTracker()
        # A cloud delegation IS the "this subproblem is hard" signal.
        d = bt.should_escalate(thread_id=thread_id, subproblem_is_hard=True)
        return (bool(d.allowed), d.reason)
    except Exception as e:
        return fallback(f"gate error ({type(e).__name__})")


# Persona-config directory. Configurable via env var; default is
# memory/personas-subagent/ resolved relative to this module's
# parent (i.e. the OmegaClaw-Core repo root).
_DEFAULT_PERSONA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "memory", "personas-subagent"
)
PERSONA_DIR = os.environ.get("OMEGACLAW_SUBAGENT_PERSONA_DIR", _DEFAULT_PERSONA_DIR)

# Hard caps. Per-call max_turns is clamped by the lower of dispatch
# arg, persona-config default, and this hard cap. Same for digest.
SUBAGENT_MAX_TURNS_HARD_CAP = int(
    os.environ.get("OMEGACLAW_SUBAGENT_MAX_TURNS", "8")
)
SUBAGENT_MAX_DIGEST_CHARS = int(
    os.environ.get("OMEGACLAW_SUBAGENT_MAX_DIGEST_CHARS", "2000")
)
SUBAGENT_DEFAULT_OUTPUT_TOKENS = 1500

# Per-subagent-iteration history cap. The subagent's internal history
# is much smaller than the parent's (~4000 chars vs 30000) because
# the subagent operates on a focused goal, not an ongoing
# conversation.
_SUBAGENT_HISTORY_CAP = 4000
_SUBAGENT_RESULTS_CAP = 4000
_SUBAGENT_HISTORY_MAX_TURNS = int(os.environ.get("OMEGACLAW_SUBAGENT_HISTORY_MAX_TURNS", "6"))

# Shell tool restrictions. Subagent's shell is more restricted than
# parent's — disabled by default, optional executable allowlist, no shell=True,
# output truncated, default 30s timeout.
_SHELL_OUTPUT_CAP = 4000
_SHELL_TIMEOUT_S = 30

# Subagent LLM call reliability controls. Keep defaults bounded so a stuck
# worker endpoint cannot hang the parent loop indefinitely.
_SUBAGENT_LLM_TIMEOUT_S = int(os.environ.get("OMEGACLAW_SUBAGENT_LLM_TIMEOUT_S", "180"))
_SUBAGENT_LLM_RETRIES = int(os.environ.get("OMEGACLAW_SUBAGENT_LLM_RETRIES", "1"))
_SUBAGENT_LLM_BACKOFF_S = float(os.environ.get("OMEGACLAW_SUBAGENT_LLM_BACKOFF_S", "1.0"))
_SUBAGENT_LLM_CALLS_PER_MINUTE = int(os.environ.get("OMEGACLAW_SUBAGENT_LLM_CALLS_PER_MINUTE", "60"))
_SUBAGENT_MAX_CONCURRENT_LLM_CALLS = int(os.environ.get("OMEGACLAW_SUBAGENT_MAX_CONCURRENT_LLM_CALLS", "4"))

# Per-dispatch safety controls. Tool-call quota bounds work even if a worker
# loops or emits many calls per turn. Cancellation is intentionally file-based
# so supervisors/parents can stop in-flight work without signals or shared state.
_SUBAGENT_MAX_TOOL_CALLS = int(os.environ.get("OMEGACLAW_SUBAGENT_MAX_TOOL_CALLS", "24"))
_SUBAGENT_CANCEL_FILE = os.environ.get("OMEGACLAW_SUBAGENT_CANCEL_FILE", "")
_SUBAGENT_MAX_PATH_ARG_CHARS = int(os.environ.get("OMEGACLAW_SUBAGENT_MAX_PATH_ARG_CHARS", "512"))
_SUBAGENT_MAX_TOOL_ARG_CHARS = int(os.environ.get("OMEGACLAW_SUBAGENT_MAX_TOOL_ARG_CHARS", "20000"))
_SUBAGENT_MAX_CONTRACT_ITEMS = int(os.environ.get("OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEMS", "32"))
_SUBAGENT_MAX_CONTRACT_ITEM_CHARS = int(os.environ.get("OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEM_CHARS", "512"))
_SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS = int(os.environ.get("OMEGACLAW_SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS", "4000"))

# Persistent local run records. Full worker prompts/responses/tool results are
# kept out of the parent context; the parent receives only a bounded structured
# digest plus the local transcript path for audit/debug.
_DEFAULT_SUBAGENT_RUN_DIR = os.path.join(
    os.environ.get("MEMORY_DIR", os.path.join(os.getcwd(), "memory")),
    "subagent-runs",
)
SUBAGENT_RUN_DIR = os.environ.get("OMEGACLAW_SUBAGENT_RUN_DIR", _DEFAULT_SUBAGENT_RUN_DIR)


def _subagent_workspace_root():
    """Return the filesystem root visible to subagent file tools.

    Defaults to the current working directory so deployments that run the agent
    from the repo keep the historical relative-path ergonomics while closing
    absolute/parent traversal escapes. Override with
    OMEGACLAW_SUBAGENT_WORKSPACE for a narrower or dedicated scratch root.
    """
    root = os.environ.get("OMEGACLAW_SUBAGENT_WORKSPACE") or os.getcwd()
    return os.path.realpath(os.path.abspath(root))


def _resolve_workspace_path(path):
    if not path or "\x00" in str(path):
        raise ValueError("invalid path")
    root = _subagent_workspace_root()
    raw = str(path)
    candidate = raw if os.path.isabs(raw) else os.path.join(root, raw)
    resolved = os.path.realpath(os.path.abspath(candidate))
    if os.path.commonpath([root, resolved]) != root:
        raise ValueError(
            f"path escapes subagent workspace ({root}): {path}"
        )
    return resolved


def _safe_slug(text, max_len=48):
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text or "").strip()).strip("-._")
    return (slug or "run")[:max_len]


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _json_atomic_write(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = _json_bytes(data)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=parent or None
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return hashlib.sha256(payload).hexdigest()
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _write_transcript_integrity_sidecar(path, digest):
    """Write a small checksum sidecar for local transcript audit checks."""
    if not path or not digest:
        return ""
    sidecar = f"{path}.sha256"
    parent = os.path.dirname(sidecar)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(sidecar)}.", suffix=".tmp", dir=parent or None
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{digest}  {os.path.basename(path)}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, sidecar)
        return sidecar
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _append_run_index(record):
    """Append a compact audit index entry for a finished subagent run.

    Full transcripts stay in per-run JSON files so parent context remains
    bounded. This append-only JSONL index gives operators a cheap local run list
    with transcript checksum/provenance, guarded by a sidecar lock for
    cross-process writers when fcntl is available.
    """
    if not record:
        return ""
    os.makedirs(SUBAGENT_RUN_DIR, exist_ok=True)
    index_path = os.path.join(SUBAGENT_RUN_DIR, "index.jsonl")
    entry = {
        "run_id": record.get("run_id", ""),
        "persona_key": record.get("persona_key", ""),
        "status": record.get("status", ""),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "transcript_path": record.get("transcript_path", ""),
        "transcript_sha256": record.get("transcript_sha256", ""),
    }
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    lock_path = f"{index_path}.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(index_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return index_path


def _rate_limit_state_path(label):
    safe = _safe_slug(label or "worker", max_len=32)
    return os.path.join(SUBAGENT_RUN_DIR, f".llm-rate-{safe}.json")


def _concurrency_state_path(label):
    safe = _safe_slug(label or "worker", max_len=32)
    return os.path.join(SUBAGENT_RUN_DIR, f".llm-inflight-{safe}.json")


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _read_json_state(f):
    f.seek(0)
    try:
        return json.load(f)
    except Exception:
        return {}


def _write_json_state(f, data):
    f.seek(0)
    f.truncate()
    json.dump(data, f, ensure_ascii=False, sort_keys=True)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())


@contextlib.contextmanager
def _workspace_file_lock(resolved_path):
    """Serialize updates to one workspace file when fcntl is available.

    Atomic rename protects readers from torn writes, but append-style updates
    also need a per-target critical section to avoid lost updates when multiple
    parent/worker processes append to the same artifact concurrently. The lock
    file lives beside the target and is itself inside the sandbox-resolved
    parent directory.
    """
    parent = os.path.dirname(resolved_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if fcntl is None:
        yield
        return
    lock_path = os.path.join(parent or ".", f".{os.path.basename(resolved_path)}.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _atomic_replace_text(resolved_path, content):
    parent = os.path.dirname(resolved_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(resolved_path)}.", suffix=".tmp", dir=parent or None
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, resolved_path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _subagent_llm_concurrency_acquire(label):
    """Reserve one in-flight worker LLM slot across parent processes.

    This complements the calls/minute rate guard: rate limiting bounds spend over
    time, while concurrency limiting prevents several long worker calls from
    piling up at once. Set OMEGACLAW_SUBAGENT_MAX_CONCURRENT_LLM_CALLS=0 to
    disable locally.
    """
    limit = max(0, int(_SUBAGENT_MAX_CONCURRENT_LLM_CALLS))
    if limit == 0:
        return (True, "disabled", "")
    if fcntl is None:
        return (False, "fcntl unavailable for atomic concurrency state", "")
    path = _concurrency_state_path(label)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    now = time.time()
    stale_before = now - 3600.0
    try:
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            data = _read_json_state(f)
            inflight = []
            for entry in data.get("inflight", []):
                try:
                    pid = int(entry.get("pid"))
                    ts = float(entry.get("ts", 0))
                except Exception:
                    continue
                if ts >= stale_before and _pid_alive(pid):
                    inflight.append(entry)
            if len(inflight) >= limit:
                _write_json_state(f, {"inflight": inflight})
                return (False, f"{len(inflight)}/{limit} worker LLM calls already in flight", "")
            inflight.append({"token": token, "pid": os.getpid(), "ts": now})
            _write_json_state(f, {"inflight": inflight})
        return (True, f"{len(inflight)}/{limit} worker LLM calls in flight", token)
    except Exception as e:
        return (False, f"concurrency state error: {type(e).__name__}: {e}", "")


def _subagent_llm_concurrency_release(label, token):
    if not token or fcntl is None:
        return
    path = _concurrency_state_path(label)
    try:
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            data = _read_json_state(f)
            inflight = [e for e in data.get("inflight", []) if e.get("token") != token]
            _write_json_state(f, {"inflight": inflight})
    except Exception:
        pass


def _subagent_llm_rate_limit_acquire(label):
    """Atomically reserve one worker LLM call for the current minute.

    This is a small cross-process backpressure guard for ThreadKeeper workers:
    even if several parent loops invoke subagents at once, each configured
    endpoint label has a bounded calls/minute budget. Set
    OMEGACLAW_SUBAGENT_LLM_CALLS_PER_MINUTE=0 to disable locally.
    """
    limit = max(0, int(_SUBAGENT_LLM_CALLS_PER_MINUTE))
    if limit == 0:
        return (True, "disabled")
    if fcntl is None:
        return (False, "fcntl unavailable for atomic rate-limit state")
    path = _rate_limit_state_path(label)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    now = time.time()
    window_start = now - 60.0
    try:
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            data = _read_json_state(f)
            calls = [float(ts) for ts in data.get("calls", []) if float(ts) >= window_start]
            if len(calls) >= limit:
                return (False, f"{len(calls)}/{limit} calls already used in the last 60s")
            calls.append(now)
            _write_json_state(f, {"calls": calls})
        return (True, f"{len(calls)}/{limit} calls used in the last 60s")
    except Exception as e:
        return (False, f"rate-limit state error: {type(e).__name__}: {e}")


def _new_run_record(persona_key, goal):
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:10]}"
    path = os.path.join(SUBAGENT_RUN_DIR, f"{run_id}-{_safe_slug(persona_key)}.json")
    return {
        "run_id": run_id,
        "persona_key": persona_key,
        "goal": str(goal or ""),
        "started_at": time.time(),
        "finished_at": None,
        "status": "running",
        "history_digest": [],
        "turns": [],
        "files_changed": [],
        "tests_run": [],
        "transcript_path": path,
        "task_contract": {},
    }


def _finish_run_record(record, status, summary=None):
    if not record:
        return ""
    record["status"] = status
    record["summary"] = summary or ""
    record["finished_at"] = time.time()
    try:
        digest = _json_atomic_write(record["transcript_path"], record)
        sidecar = _write_transcript_integrity_sidecar(record["transcript_path"], digest)
        record["transcript_sha256"] = digest
        record["transcript_sha256_path"] = sidecar
        record["run_index_path"] = _append_run_index(record)
    except Exception as e:
        record["record_write_error"] = f"{type(e).__name__}: {e}"
    return record.get("transcript_path", "")


def _structured_return(summary, record=None, status="ok", uncertainty="low",
                       next_action="return to parent", max_chars=None):
    limit = max_chars or SUBAGENT_MAX_DIGEST_CHARS
    payload = {
        "summary": cap(summary, max(100, limit // 2)),
        "files_changed": list(dict.fromkeys((record or {}).get("files_changed", []))),
        "tests_run": list(dict.fromkeys((record or {}).get("tests_run", []))),
        "uncertainty": uncertainty,
        "next_action": next_action,
        "transcript_path": (record or {}).get("transcript_path", ""),
        "transcript_sha256": (record or {}).get("transcript_sha256", ""),
        "status": status,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    # Preserve valid JSON by shrinking variable-length fields instead of
    # truncating the serialized object mid-token.
    payload["summary"] = cap(summary, 200)
    payload["files_changed"] = payload["files_changed"][:20]
    payload["tests_run"] = payload["tests_run"][:10]
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    payload["summary"] = cap(summary, 80)
    payload["files_changed"] = []
    payload["tests_run"] = []
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _digest_history_entry(entry):
    t, raw, res = entry
    return f"turn {t}: response={_clip(cap(raw, 500), 240)}; results={_clip(cap(res, 500), 240)}"


def _append_bounded_history(history, entry, history_digest):
    history.append(entry)
    max_turns = max(1, _SUBAGENT_HISTORY_MAX_TURNS)
    while len(history) > max_turns:
        evicted = history.pop(0)
        history_digest.append(_digest_history_entry(evicted))
    # Keep the digest bounded too; this is for prompt context, not audit.
    if len(history_digest) > max_turns:
        del history_digest[:-max_turns]


def _shell_enabled():
    return os.environ.get("OMEGACLAW_SUBAGENT_ENABLE_SHELL", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _shell_allowlist():
    raw = os.environ.get("OMEGACLAW_SUBAGENT_SHELL_ALLOWLIST", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


# ----------------------------------------------------------------------
# Persona config loading
# ----------------------------------------------------------------------

def _validate_persona_key(persona_key):
    """Keep persona lookup on named configs, not paths.

    Persona keys come from model/tool-facing `(delegate ...)` calls. Even though
    persona files are deployment-controlled, the lookup key itself should be a
    simple identifier so a child/parent prompt cannot traverse arbitrary JSON
    files via `../` or absolute paths.
    """
    key = str(persona_key or "").strip()
    if not key or not re.match(r"^[A-Za-z0-9_.-]+$", key) or key in (".", ".."):
        raise ValueError("persona key must be a simple identifier")
    return key


def load_persona_config(persona_key):
    """Read memory/personas-subagent/<key>.json. Returns dict with the
    fields documented in docs/subagent-design.md §4.4.1."""
    persona_key = _validate_persona_key(persona_key)
    path = os.path.join(PERSONA_DIR, f"{persona_key}.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"persona config '{persona_key}.json' not found at {path}"
        )
    with open(path, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"persona config '{persona_key}.json' is malformed JSON: {e}"
            )
    required = ["persona_file", "provider", "model", "api_key_env", "node_role"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError(
            f"persona config '{persona_key}.json' missing required field(s): {missing}"
        )
    role = _node_role(cfg)
    if role not in (_LOCAL_NODE_ROLES | _CLOUD_NODE_ROLES):
        raise ValueError(
            f"persona config '{persona_key}.json' has invalid node_role '{cfg.get('node_role')}'; "
            f"expected one of {sorted(_LOCAL_NODE_ROLES | _CLOUD_NODE_ROLES)}"
        )
    kind = _endpoint_kind(cfg)
    if kind not in {"ollama_native", "openai_compatible"}:
        raise ValueError(
            f"persona config '{persona_key}.json' has invalid endpoint_kind/provider metadata '{kind}'; "
            "expected ollama_native or openai_compatible"
        )
    cfg["_persona_key"] = persona_key
    cfg["_node_role"] = role
    cfg["_endpoint_kind"] = kind
    return cfg


def _resolve_persona_prompt_path(persona_file, persona_key):
    """Resolve a persona prompt path inside PERSONA_DIR.

    Persona configs are deployment-controlled, but the config file is still part
    of the subagent trust boundary. Do not let a malformed/malicious config read
    arbitrary absolute paths or escape the persona directory via `..` segments.
    """
    rel = str(persona_file or "").strip()
    if not rel:
        raise ValueError(f"persona prompt for key '{persona_key}' is empty")
    base = os.path.realpath(PERSONA_DIR)
    candidate = rel if os.path.isabs(rel) else os.path.join(base, rel)
    path = os.path.realpath(candidate)
    try:
        common = os.path.commonpath([base, path])
    except ValueError:
        common = ""
    if common != base:
        raise ValueError(
            f"persona prompt '{persona_file}' for key '{persona_key}' escapes persona directory"
        )
    return path


def load_persona_prompt(persona_file, persona_key, expected_sha256=""):
    """Read the persona text and optionally verify its SHA-256.

    Persona JSON may include `persona_sha256` to pin the prompt file. When set,
    a missing/mismatched prompt hash fails closed before any worker call.
    """
    path = _resolve_persona_prompt_path(persona_file, persona_key)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"persona prompt '{persona_file}' for key '{persona_key}' "
            f"not found at {path}"
        )
    with open(path, "rb") as f:
        raw = f.read()
    expected = str(expected_sha256 or "").strip().lower()
    if expected:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise ValueError(
                f"persona prompt '{persona_file}' for key '{persona_key}' "
                "failed sha256 integrity check"
            )
    return raw.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------
# Tool subset parsing + validation
# ----------------------------------------------------------------------

# v1 tool registry. Keys are skill names exposed to subagents; values
# are (callable, category) pairs. Categories: "endpoint_independent"
# (works regardless of where the subagent loop runs);
# "parent_env_bound" (requires parent process state — none in v1).
# Tools NOT in this dict are unknown to the subagent. Tools in
# _V1_EXCLUDED are deliberately forbidden.
_V1_EXCLUDED = frozenset([
    "remember", "pin", "metta", "send", "delegate", "query", "episodes",
])


def _build_tool_registry():
    """Construct the per-process tool registry once. Imports are inline
    so that import failures don't break dispatch — instead the affected
    tool simply isn't registered."""
    registry = {}

    # File I/O — pure stdlib
    registry["read-file"] = (_tool_read_file, "endpoint_independent")
    registry["write-file"] = (_tool_write_file, "endpoint_independent")
    registry["append-file"] = (_tool_append_file, "endpoint_independent")

    # Shell — restricted subprocess
    registry["shell"] = (_tool_shell, "endpoint_independent")

    # Web search — reuses channels/websearch.py
    try:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "channels"
        ))
        import websearch
        registry["search"] = (
            lambda q: websearch.search(q),
            "endpoint_independent",
        )
    except Exception as e:
        # Search not registered if websearch import fails. Diagnostic
        # available through error path if the subagent tries to use it.
        registry["_search_import_error"] = str(e)

    # Remote-agent skills via src/agentverse.py
    try:
        import agentverse
        registry["tavily-search"] = (
            lambda q: agentverse.tavily_search(q),
            "endpoint_independent",
        )
        registry["technical-analysis"] = (
            lambda t: agentverse.technical_analysis(t),
            "endpoint_independent",
        )
    except Exception:
        # Agentverse-backed skills unavailable if uagents isn't
        # importable. Subagent gets a clear error if it tries.
        pass

    return registry


_TOOL_REGISTRY = None  # initialized lazily

def _tool_registry():
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is None:
        _TOOL_REGISTRY = _build_tool_registry()
    return _TOOL_REGISTRY


def parse_subset(tool_subset_csv):
    """Validate a CSV of tool names against the registry. Returns the
    list of tool names. Raises ValueError on unknown / v1-excluded."""
    if not tool_subset_csv:
        raise ValueError("tool subset is empty")
    names = [n.strip() for n in tool_subset_csv.split(",") if n.strip()]
    reg = _tool_registry()
    excluded = [n for n in names if n in _V1_EXCLUDED]
    if excluded:
        raise ValueError(
            f"skill(s) {excluded} are not callable by subagents in v1 "
            "(see docs/subagent-design.md §4.5.2)"
        )
    unknown = [n for n in names if n not in reg]
    if unknown:
        raise ValueError(
            f"unknown skill(s) {unknown}; registered subagent tools: "
            f"{sorted(k for k in reg.keys() if not k.startswith('_'))}"
        )
    return names


def validate_endpoint_compat(tool_names, cfg):
    """Placeholder for forward-compatible Option C runner-vs-tool
    validation. In Option B v1, the loop is always in-process, so all
    non-excluded tools are reachable regardless of where the subagent's
    LLM endpoint lives. Always passes."""
    return True


# ----------------------------------------------------------------------
# Provider resolution
# ----------------------------------------------------------------------

def resolve_or_instantiate_provider(provider_name, model_name, base_url, var_name, endpoint_kind=None):
    """Build an AIProvider scoped to this dispatch. Stays inside
    lib_llm_ext's existing class abstraction; does not mutate
    _provider_registry.

    A fresh AIProvider instance per dispatch ensures each persona's
    (provider, model, base_url, api_key_env) binding is honored
    exactly — no shared mutable state across dispatches with
    different bindings. AIProvider's _ensure_client lazy-inits the
    underlying openai client on first .chat() call, so this is
    cheap at construction (no network).

    Returns a dict with keys: provider (AIProvider), model, provider_name."""
    api_key = os.environ.get(var_name)
    if not api_key:
        raise RuntimeError(
            f"env var '{var_name}' is unset; cannot reach endpoint for "
            f"provider '{provider_name}'"
        )
    # Build a plain OpenAI-compatible client for the CLOUD worker path
    # (e.g. GLM 5.2 specialist). The LOCAL Ollama path in
    # _call_subagent_llm uses native urllib and ignores this client, so a
    # client failure here doesn't break local delegation. Import deferred
    # so the module stays lint-importable without openai installed.
    client = None
    try:
        import openai
        client = openai.OpenAI(api_key=api_key, base_url=(base_url or None))
    except Exception:
        client = None  # local path doesn't need it
    return {
        "provider": client,
        "model": model_name,
        "provider_name": provider_name,
        "base_url": base_url or "",
        "var_name": var_name,
        "endpoint_kind": endpoint_kind or _endpoint_kind({"provider": provider_name}),
    }


# ----------------------------------------------------------------------
# LLM call — uses AIProvider.chat from lib_llm_ext.
# ----------------------------------------------------------------------

def _call_with_retries(call_once, label):
    """Run one bounded worker call with retry/backoff. Returns text or error."""
    attempts = max(1, _SUBAGENT_LLM_RETRIES + 1)
    last_exc = None
    for attempt in range(1, attempts + 1):
        in_flight, concurrency_reason, concurrency_token = _subagent_llm_concurrency_acquire(label)
        if not in_flight:
            return f"(subagent LLM call concurrency-limited via {label}: {concurrency_reason})"
        try:
            allowed, reason = _subagent_llm_rate_limit_acquire(label)
            if not allowed:
                return f"(subagent LLM call rate-limited via {label}: {reason})"
            try:
                return call_once()
            except Exception as e:
                last_exc = e
                if attempt < attempts:
                    delay = max(0.0, _SUBAGENT_LLM_BACKOFF_S) * (2 ** (attempt - 1))
                    if delay:
                        time.sleep(delay)
        finally:
            _subagent_llm_concurrency_release(label, concurrency_token)
    return (
        f"(subagent LLM call failed after {attempts} attempt(s) "
        f"via {label}: {type(last_exc).__name__}: {last_exc})"
    )


def _call_subagent_llm(provider_handle, content, max_tokens):
    """Call the subagent's worker LLM and return response text.

    For LOCAL Ollama endpoints we use the NATIVE /api/chat path with
    {"think": false} — the OpenAI /v1 path on this Ollama build returns
    EMPTY content for reasoning models (qwen/gemma/gpt-oss/granite) because
    hidden <think> tokens consume the whole budget. The native path with
    thinking disabled returns real content. For non-Ollama (cloud) endpoints
    we fall back to AIProvider.chat (/v1), which is correct there.

    Never raises into the MeTTa interpreter — returns a (subagent ...) string
    on failure.
    """
    base_url = (provider_handle.get("base_url") or "").rstrip("/")
    model = provider_handle["model"]
    endpoint_kind = (provider_handle.get("endpoint_kind") or "").strip().lower()

    if base_url and endpoint_kind == "ollama_native":
        import json as _json
        import urllib.request as _u
        root = base_url[:-3] if base_url.endswith("/v1") else base_url
        def call_once():
            body = _json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
                "think": False,
                "options": {"num_predict": max_tokens},
            }).encode()
            req = _u.Request(root + "/api/chat", data=body,
                             headers={"Content-Type": "application/json"})
            with _u.urlopen(req, timeout=_SUBAGENT_LLM_TIMEOUT_S) as r:
                data = _json.loads(r.read().decode("utf-8", errors="replace"))
            _log_worker_usage(model, data.get("prompt_eval_count", 0),
                              data.get("eval_count", 0))
            return (data.get("message") or {}).get("content", "") or ""
        return _call_with_retries(call_once, "ollama")

    # Cloud endpoint — standard OpenAI /v1 chat (GLM/DeepSeek separate
    # reasoning from content correctly here).
    client = provider_handle["provider"]
    if client is None:
        return "(subagent error: no cloud client available)"
    def call_once():
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens,
            timeout=_SUBAGENT_LLM_TIMEOUT_S,
        )
        try:
            u = resp.usage
            _log_worker_usage(model, getattr(u, "prompt_tokens", 0),
                              getattr(u, "completion_tokens", 0))
        except Exception:
            pass
        return resp.choices[0].message.content or ""
    return _call_with_retries(call_once, "openai-compatible")


# ----------------------------------------------------------------------
# Prompt construction
# ----------------------------------------------------------------------

# Tool catalogue descriptions — these are the strings the subagent
# sees so it knows what's callable. Mirrors src/skills.metta:getSkills
# but narrowed per dispatch.
_TOOL_DESCRIPTIONS = {
    "search":
        "- Search the web; returns titles + snippets: search query",
    "read-file":
        "- Read file to string: read-file filename",
    "write-file":
        "- Write string to file: write-file filename string",
    "append-file":
        "- Append line to file: append-file filename string",
    "shell":
        "- Execute shell command without apostrophe in string; "
        "returns command output: shell string",
    "tavily-search":
        "- Search the web via Tavily Search Agent: tavily-search query",
    "technical-analysis":
        "- Technical analysis for a stock ticker: technical-analysis ticker",
}


def _normalize_task_contract(goal, cfg=None):
    """Return (objective_text, contract_dict) for optional task contracts.

    Contracts are intentionally data-only and may be supplied either in the
    persona JSON as `task_contract` or inline as a JSON goal object containing
    `objective`, `allowed_paths`, `forbidden_actions`, `done_criteria`,
    and/or `max_tool_calls`.
    This keeps the existing `(delegate goal tools persona max_turns)` API while
    giving parent agents a concrete way to narrow a child task.
    """
    contract = dict((cfg or {}).get("task_contract") or {})
    objective = str(goal or "")
    try:
        parsed = json.loads(goal) if isinstance(goal, str) else goal
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        inline = parsed.get("task_contract") if isinstance(parsed.get("task_contract"), dict) else parsed
        if isinstance(inline, dict):
            for key in ("allowed_paths", "forbidden_actions", "done_criteria", "max_tool_calls"):
                if key in inline:
                    contract[key] = inline[key]
            objective = str(inline.get("objective") or parsed.get("objective") or objective)
    objective = str(objective or "").strip()
    contract["objective"] = objective
    contract["allowed_paths"] = _contract_string_list(contract.get("allowed_paths"))
    contract["forbidden_actions"] = _contract_string_list(contract.get("forbidden_actions"))
    contract["done_criteria"] = _contract_string_list(contract.get("done_criteria"))
    return objective, contract


def _validate_task_contract(contract):
    """Fail closed on oversized or unsafe task-contract data.

    Contracts are prompt-visible and persisted in transcripts, so they need the
    same bounded-shape treatment as tool arguments. `allowed_paths` also gets a
    dry-run workspace resolution now, rather than waiting until a tool call.
    """
    objective = str((contract or {}).get("objective") or "")
    if len(objective) > _SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS:
        return (
            "task contract objective exceeds "
            f"{_SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS} characters"
        )
    for field in ("allowed_paths", "forbidden_actions", "done_criteria"):
        values = list((contract or {}).get(field) or [])
        if len(values) > _SUBAGENT_MAX_CONTRACT_ITEMS:
            return f"task contract {field} has {len(values)} item(s), max {_SUBAGENT_MAX_CONTRACT_ITEMS}"
        for value in values:
            if len(str(value)) > _SUBAGENT_MAX_CONTRACT_ITEM_CHARS:
                return (
                    f"task contract {field} item exceeds "
                    f"{_SUBAGENT_MAX_CONTRACT_ITEM_CHARS} characters"
                )
    if "max_tool_calls" in (contract or {}):
        raw_quota = (contract or {}).get("max_tool_calls")
        if isinstance(raw_quota, bool):
            return f"task contract max_tool_calls entry '{raw_quota}' is not an integer"
        if isinstance(raw_quota, int):
            quota = raw_quota
        elif isinstance(raw_quota, str) and re.match(r"^\d+$", raw_quota.strip()):
            quota = int(raw_quota.strip())
        else:
            return f"task contract max_tool_calls entry '{raw_quota}' is not an integer"
        if quota < 0:
            return "task contract max_tool_calls must be non-negative"
        contract["max_tool_calls"] = quota
    for action in (contract or {}).get("forbidden_actions") or []:
        if not re.match(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$", str(action)):
            return f"task contract forbidden_actions entry '{action}' is not a safe action identifier"
    for prefix in (contract or {}).get("allowed_paths") or []:
        try:
            _resolve_workspace_path(prefix)
        except Exception as e:
            return f"task contract allowed_paths entry '{prefix}' is outside workspace: {e}"
    return ""



def _contract_string_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        value = [str(value)]
    out = []
    for item in value:
        text = str(item or "").strip()
        if text and "\x00" not in text:
            out.append(text)
    return out


def _path_within_contract(path, contract):
    allowed = (contract or {}).get("allowed_paths") or []
    if not allowed:
        return True
    try:
        resolved = _resolve_workspace_path(path)
    except Exception:
        return False
    for prefix in allowed:
        try:
            allowed_path = _resolve_workspace_path(prefix)
        except Exception:
            continue
        if os.path.commonpath([allowed_path, resolved]) == allowed_path:
            return True
    return False


def _tool_forbidden_by_contract(name, contract):
    forbidden = {x.strip().lower() for x in ((contract or {}).get("forbidden_actions") or [])}
    aliases = {
        name.lower(),
        name.lower().replace("-", "_"),
    }
    if name in ("write-file", "append-file"):
        aliases.update({"write", "file-write", "modify-files"})
    if name == "shell":
        aliases.update({"exec", "execute", "run-command", "shell-exec"})
    return bool(forbidden & aliases)


def tools_catalog(tool_names):
    """Build the subagent's SKILLS block — narrowed to the subset."""
    lines = []
    for name in tool_names:
        desc = _TOOL_DESCRIPTIONS.get(name)
        if desc:
            lines.append(desc)
    # emit is always available — it is how the subagent terminates
    lines.append(
        "- Emit your final digest to the parent and end the loop: "
        "emit string"
    )
    return "\n".join(lines)


def build_subagent_prompt(persona, catalog, last_results, history, goal,
                          iteration, max_iterations, history_digest=None,
                          task_contract=None):
    """Build the subagent's per-turn prompt. Shape mirrors the parent's
    getContext but with smaller per-component caps appropriate to a
    short-lived helper."""
    history_snippet = ""
    if history:
        # Keep the tail of the subagent's own history under the cap
        joined = "\n".join(
            f"[turn {t}] response: {_clip(r, 800)} | results: {_clip(res, 800)}"
            for (t, r, res) in history
        )
        history_snippet = joined[-_SUBAGENT_HISTORY_CAP:]
    parts = [
        f"PERSONA: {persona.strip()}",
        f"TOOLS:\n{catalog}",
        "OUTPUT_FORMAT: Emit one s-expression per line, each starting with '('. "
        "Use the tools above. When you have your final answer, emit "
        "(emit \"<digest>\") on its own line and stop. Do not narrate; "
        "do not wrap output in markdown fences; do not use <think> blocks. "
        "No more than 3 tool calls per turn.",
        f"GOAL: {goal}",
        f"ITERATION: {iteration} of {max_iterations} maximum",
    ]
    if task_contract:
        parts.append(
            "TASK_CONTRACT:\n"
            f"objective: {task_contract.get('objective') or goal}\n"
            f"allowed_paths: {task_contract.get('allowed_paths') or []}\n"
            f"forbidden_actions: {task_contract.get('forbidden_actions') or []}\n"
            f"done_criteria: {task_contract.get('done_criteria') or []}\n"
            f"max_tool_calls: {task_contract.get('max_tool_calls', 'global-default')}"
        )
    if last_results:
        parts.append(f"LAST_RESULTS:\n{last_results[-_SUBAGENT_RESULTS_CAP:]}")
    if history_digest:
        parts.append(f"HISTORY_DIGEST:\n{_clip(' | '.join(history_digest), _SUBAGENT_HISTORY_CAP)}")
    if history_snippet:
        parts.append(f"HISTORY:\n{history_snippet}")
    return "\n\n".join(parts)


def _clip(s, n):
    s = s if s is not None else ""
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


# ----------------------------------------------------------------------
# Response parsing — strips <think> blocks, markdown fences, finds
# s-expressions starting at line beginnings. Self-contained; does NOT
# depend on lib_llm_ext.
# ----------------------------------------------------------------------

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n|^\s*```\s*$", re.MULTILINE)


def _strip_thinking(text):
    return _THINK_RE.sub("", text)


def _strip_fences(text):
    return _FENCE_RE.sub("", text)


def parse_calls(adapted_text):
    """Find lines starting with '(' and parse each as one s-expression.
    Returns list of (skill_name, [args]) tuples. Best-effort — bad
    lines are skipped, not raised on."""
    text = _strip_thinking(adapted_text)
    text = _strip_fences(text)
    calls = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("("):
            continue
        if not line.endswith(")"):
            continue
        # Strip outer parens
        inner = line[1:-1].strip()
        if not inner:
            continue
        # Parse: skill_name <args>; first whitespace-separated token is
        # the skill name; the rest is the argument (which may itself
        # be quoted). For v1 we only support single-arg skills and
        # two-arg write-file/append-file.
        m = re.match(r"^([A-Za-z][A-Za-z0-9_\-]*)\s*(.*)$", inner, re.DOTALL)
        if not m:
            continue
        name = m.group(1)
        rest = m.group(2).strip()
        args = _parse_args(name, rest)
        calls.append((name, args))
    return calls


def _parse_args(skill_name, rest):
    """Tolerant arg parser. Handles quoted strings and bare tokens.
    For the small v1 skill set we don't need a real lexer."""
    if not rest:
        return []
    # Two-arg skills: filename then content
    if skill_name in ("write-file", "append-file"):
        # Pull the filename (first quoted string or first whitespace
        # token), then everything else is content
        if rest.startswith('"'):
            end = _find_close_quote(rest, 1)
            if end == -1:
                return [rest]
            filename = rest[1:end]
            content = rest[end + 1:].strip()
            if content.startswith('"') and content.endswith('"'):
                content = content[1:-1]
            return [filename, content]
        parts = rest.split(None, 1)
        if len(parts) == 1:
            return [parts[0], ""]
        filename, content = parts[0], parts[1].strip()
        if content.startswith('"') and content.endswith('"'):
            content = content[1:-1]
        return [filename, content]
    # Single-arg skills
    if rest.startswith('"') and rest.endswith('"'):
        return [rest[1:-1]]
    return [rest]


def _find_close_quote(s, start):
    i = start
    while i < len(s):
        if s[i] == '\\':
            i += 2
            continue
        if s[i] == '"':
            return i
        i += 1
    return -1


# ----------------------------------------------------------------------
# Tool execution
# ----------------------------------------------------------------------

def _tool_read_file(path):
    try:
        resolved = _resolve_workspace_path(path)
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"(read-file error: {e})"


def _tool_write_file(path, content):
    try:
        resolved = _resolve_workspace_path(path)
        with _workspace_file_lock(resolved):
            _atomic_replace_text(resolved, content)
        return "WRITE-FILE-SUCCESS"
    except Exception as e:
        return f"(write-file error: {e})"


def _tool_append_file(path, content):
    try:
        resolved = _resolve_workspace_path(path)
        with _workspace_file_lock(resolved):
            existing = ""
            if os.path.exists(resolved):
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            new_content = existing
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            new_content += content + "\n"
            _atomic_replace_text(resolved, new_content)
        return "APPEND-FILE-SUCCESS"
    except Exception as e:
        return f"(append-file error: {e})"


def _tool_shell(cmd):
    """Restricted command runner: disabled unless explicitly enabled and
    executable-allowlisted. Uses shell=False so metacharacters are arguments,
    not command separators."""
    if not _shell_enabled():
        return "(shell error: disabled by default; set OMEGACLAW_SUBAGENT_ENABLE_SHELL=1 and OMEGACLAW_SUBAGENT_SHELL_ALLOWLIST)"
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return f"(shell error: invalid command: {e})"
    if not argv:
        return "(shell error: empty command)"
    allow = _shell_allowlist()
    exe = os.path.basename(argv[0])
    if not allow or exe not in allow:
        return f"(shell error: executable '{exe}' is not allowlisted)"
    try:
        out = subprocess.run(
            argv, shell=False, capture_output=True, timeout=_SHELL_TIMEOUT_S,
        )
        text = (out.stdout or b"").decode("utf-8", errors="replace")
        text += (out.stderr or b"").decode("utf-8", errors="replace")
        return text[:_SHELL_OUTPUT_CAP]
    except subprocess.TimeoutExpired:
        return f"(shell error: timed out after {_SHELL_TIMEOUT_S}s)"
    except Exception as e:
        return f"(shell error: {e})"


def _validate_tool_args(name, args):
    expected = {
        "read-file": 1,
        "shell": 1,
        "search": 1,
        "tavily-search": 1,
        "technical-analysis": 1,
        "write-file": 2,
        "append-file": 2,
    }
    if name not in expected:
        return None
    want = expected[name]
    if len(args) != want:
        return f"expected {want} arg(s), got {len(args)}"
    if any(a is None for a in args):
        return "arguments must not be null"
    if name in ("read-file", "write-file", "append-file") and not str(args[0]).strip():
        return "path argument must not be empty"
    if name in ("read-file", "write-file", "append-file") and len(str(args[0])) > _SUBAGENT_MAX_PATH_ARG_CHARS:
        return f"path argument exceeds {_SUBAGENT_MAX_PATH_ARG_CHARS} characters"
    too_long = [i + 1 for i, arg in enumerate(args) if len(str(arg)) > _SUBAGENT_MAX_TOOL_ARG_CHARS]
    if too_long:
        return f"argument(s) {too_long} exceed {_SUBAGENT_MAX_TOOL_ARG_CHARS} characters"
    if any("\x00" in str(a) for a in args):
        return "arguments must not contain NUL bytes"
    return None


def _cancel_requested():
    path = (_SUBAGENT_CANCEL_FILE or "").strip()
    return bool(path and os.path.exists(path))


def run_tools(calls, allowed_names, record=None, quota=None, task_contract=None):
    """Execute each call against the registry, return aggregated result
    string for the next turn's prompt."""
    if not calls:
        return "(no parseable tool calls in last response)"
    reg = _tool_registry()
    out_parts = []
    remaining = None if quota is None else max(0, int(quota))
    for (name, args) in calls:
        if _cancel_requested():
            out_parts.append("(CANCELLED: subagent cancellation token present)")
            break
        if name != "emit":
            if remaining is not None and remaining <= 0:
                out_parts.append("(QUOTA_EXCEEDED: subagent tool-call quota exhausted)")
                break
            if remaining is not None:
                remaining -= 1
        if name == "emit":
            # emit is the loop terminator; handled by the caller
            continue
        if _tool_forbidden_by_contract(name, task_contract):
            out_parts.append(f"(CONTRACT_VIOLATION: {name} is forbidden by task contract)")
            continue
        if name not in allowed_names:
            out_parts.append(
                f"(SKILL_REJECTED: {name} not in this dispatch's tool subset)"
            )
            continue
        tool = reg.get(name)
        if tool is None:
            out_parts.append(f"(SKILL_UNAVAILABLE: {name} not registered)")
            continue
        arg_error = _validate_tool_args(name, args)
        if arg_error:
            out_parts.append(f"(SKILL_ARG_ERROR: {name}: {arg_error})")
            continue
        if name in ("read-file", "write-file", "append-file") and not _path_within_contract(args[0], task_contract):
            out_parts.append(
                f"(CONTRACT_VIOLATION: {name} path '{args[0]}' outside allowed_paths)"
            )
            continue
        fn, _category = tool
        try:
            result = fn(*args)
        except TypeError as e:
            out_parts.append(f"(SKILL_ARG_ERROR: {name}: {e})")
            continue
        except Exception as e:
            out_parts.append(f"(SKILL_RUNTIME_ERROR: {name}: {e})")
            continue
        if record is not None and name in ("write-file", "append-file") and str(result).endswith("SUCCESS"):
            record.setdefault("files_changed", []).append(str(args[0]))
        if record is not None and name == "shell" and args and _looks_like_test_command(str(args[0])):
            record.setdefault("tests_run", []).append(str(args[0]))
        out_parts.append(f"(COMMAND_RETURN: ({name} {args[0] if args else ''}) "
                         f"{_clip(str(result), 2000)})")
    if record is not None and remaining is not None:
        record["tool_calls_remaining"] = remaining
    return " ".join(out_parts)


def _looks_like_test_command(cmd):
    try:
        argv = shlex.split(cmd) if cmd else []
    except ValueError:
        return False
    names = {os.path.basename(a) for a in argv[:3]}
    return bool(names & {"pytest", "unittest", "tox", "nox", "make"}) or " test" in f" {cmd} "


def _extract_final_emit(calls):
    """Return (emit_value, protocol_error) for a final-only emit response.

    Earlier prototypes accepted the first `(emit ...)` anywhere in a worker
    response. That let a malformed or adversarial response hide later tool calls
    or conflicting emits from the parent. Harden the contract: a final digest is
    accepted only when the parsed response contains exactly one call, and that
    call is a single-argument `emit`.
    """
    emit_calls = [(name, args) for (name, args) in calls if name == "emit"]
    if not emit_calls:
        return (None, "")
    if len(calls) != 1 or len(emit_calls) != 1:
        return (None, "EMIT_PROTOCOL_VIOLATION: emit must be the only parsed call in a final response")
    _name, args = emit_calls[0]
    if len(args) != 1 or args[0] is None:
        return (None, "EMIT_PROTOCOL_VIOLATION: emit requires exactly one non-null argument")
    if "\x00" in str(args[0]):
        return (None, "EMIT_PROTOCOL_VIOLATION: emit argument must not contain NUL bytes")
    return (args[0], "")


# ----------------------------------------------------------------------
# Result post-processing
# ----------------------------------------------------------------------

def cap(text, max_chars):
    """Newline-to-space + hard length cap. Ensures the digest lands
    cleanly inside the parent's LAST_SKILL_USE_RESULTS."""
    s = (text or "").replace("\n", " ").replace("\r", " ")
    s = " ".join(s.split())
    if len(s) > max_chars:
        s = s[: max_chars - 3] + "..."
    return s


def error(msg):
    """Wrap an error into the legacy setup-error string.

    Dispatch uses _structured_setup_error where possible so failures also get
    transcript records; keep this helper for direct callers and exceptional
    pre-dispatch paths.
    """
    return f"(subagent error: {msg})"


def _structured_setup_error(msg, persona_key, goal, max_chars,
                            record_status="setup_error", task_contract=None,
                            next_action="fix setup/config before retry"):
    """Return a structured error digest and persist a minimal run record.

    Early failures used to return only `(subagent error: ...)`, which made them
    invisible to transcript/audit tooling. Persist enough local context for the
    parent/operator to debug without making any worker LLM call.
    """
    record = _new_run_record(persona_key or "unknown", goal)
    if task_contract is not None:
        record["task_contract"] = dict(task_contract)
    summary = error(msg)
    _finish_run_record(record, record_status, summary)
    return _structured_return(
        summary, record, status="error", uncertainty="high",
        next_action=next_action, max_chars=max_chars,
    )


# ----------------------------------------------------------------------
# The dispatch entry point — called from MeTTa via py-call
# ----------------------------------------------------------------------

def dispatch(goal, tool_subset_csv, persona_key, max_turns=None,
             max_chars=None):
    """Entry point invoked by (delegate ...) in src/skills.metta.

    Returns a single-line string (length ≤ max_chars or
    SUBAGENT_MAX_DIGEST_CHARS) suitable for inclusion in the
    parent's LAST_SKILL_USE_RESULTS.

    Failure path always returns a (subagent error: ...) string;
    never raises into the MeTTa interpreter."""
    # 1. Bound the per-call caps
    if max_turns is None:
        max_turns = SUBAGENT_MAX_TURNS_HARD_CAP
    try:
        max_turns = int(max_turns)
    except (TypeError, ValueError):
        max_turns = SUBAGENT_MAX_TURNS_HARD_CAP
    bounded_turns = max(1, min(max_turns, SUBAGENT_MAX_TURNS_HARD_CAP))

    if max_chars is None:
        max_chars = SUBAGENT_MAX_DIGEST_CHARS
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = SUBAGENT_MAX_DIGEST_CHARS
    bounded_chars = max(100, min(max_chars, SUBAGENT_MAX_DIGEST_CHARS))

    # 2. Load persona config
    try:
        cfg = load_persona_config(persona_key)
    except (FileNotFoundError, ValueError) as e:
        return _structured_setup_error(str(e), persona_key, goal, bounded_chars)
    objective, task_contract = _normalize_task_contract(goal, cfg)
    contract_error = _validate_task_contract(task_contract)
    if contract_error:
        return _structured_setup_error(
            contract_error, persona_key, objective, bounded_chars,
            record_status="contract_invalid", task_contract=task_contract,
            next_action="fix task contract before retry",
        )

    # 2b. ThreadKeeper escalation gate. A delegation to a CLOUD specialist is
    # the expensive node — consult the budget policy (src/escalation.metta via
    # PeTTa) before spending. If denied, refuse the dispatch and return the
    # [metta]-tagged reason so the parent loop sees WHY (and can finish on cheap
    # nodes). Local delegations are free and pass through. Gate errors fail
    # closed by default unless an operator explicitly sets fail-open fallback.
    gate_allowed, gate_reason = _escalation_gate(cfg)
    if not gate_allowed:
        denial = (
            f"(escalation denied) {gate_reason} — "
            f"cloud delegation to persona '{persona_key}' refused by the "
            f"ThreadKeeper budget policy; finish on local/cheap nodes or stop."
        )
        record = _new_run_record(persona_key, objective)
        record["task_contract"] = dict(task_contract)
        _finish_run_record(record, "escalation_denied", denial)
        return _structured_return(
            denial, record, status="error", uncertainty="high",
            next_action="finish on local/cheap nodes or stop", max_chars=bounded_chars,
        )

    # 3. Resolve tool subset
    subset_csv = (tool_subset_csv or "").strip()
    if not subset_csv:
        default_subset = cfg.get("default_tool_subset", [])
        if not default_subset:
            return _structured_setup_error(
                f"no tool subset given and persona '{persona_key}' has no "
                "default_tool_subset",
                persona_key, objective, bounded_chars,
                record_status="tool_subset_invalid", task_contract=task_contract,
                next_action="provide an explicit tool subset or persona default_tool_subset",
            )
        subset_csv = ",".join(default_subset)
    try:
        tool_names = parse_subset(subset_csv)
    except ValueError as e:
        return _structured_setup_error(
            str(e), persona_key, objective, bounded_chars,
            record_status="tool_subset_invalid", task_contract=task_contract,
            next_action="fix delegate tool subset before retry",
        )

    validate_endpoint_compat(tool_names, cfg)  # always passes in v1

    # 4. Load persona prompt
    try:
        persona_text = load_persona_prompt(
            cfg["persona_file"], persona_key, cfg.get("persona_sha256")
        )
    except (FileNotFoundError, ValueError) as e:
        return _structured_setup_error(
            str(e), persona_key, objective, bounded_chars,
            record_status="persona_prompt_invalid", task_contract=task_contract,
        )

    # 5. Resolve provider
    try:
        provider_handle = resolve_or_instantiate_provider(
            provider_name=cfg["provider"],
            model_name=cfg["model"],
            base_url=cfg.get("base_url"),
            var_name=cfg["api_key_env"],
            endpoint_kind=cfg.get("_endpoint_kind"),
        )
    except RuntimeError as e:
        return _structured_setup_error(
            str(e), persona_key, objective, bounded_chars,
            record_status="provider_invalid", task_contract=task_contract,
        )

    # 6. Run the mini-loop
    catalog = tools_catalog(tool_names)
    history = []
    history_digest = []
    last_results = ""
    tool_calls_remaining = max(0, _SUBAGENT_MAX_TOOL_CALLS)
    if "max_tool_calls" in task_contract:
        tool_calls_remaining = min(tool_calls_remaining, max(0, int(task_contract["max_tool_calls"])))
    max_out_tok = int(cfg.get("max_output_tokens", SUBAGENT_DEFAULT_OUTPUT_TOKENS))
    run_record = _new_run_record(persona_key, objective)
    run_record["task_contract"] = dict(task_contract)

    for turn in range(bounded_turns):
        if _cancel_requested():
            _finish_run_record(run_record, "cancelled", "subagent cancellation token present before LLM call")
            return _structured_return(
                "subagent cancellation token present before LLM call", run_record,
                status="cancelled", uncertainty="low", next_action="return to parent",
                max_chars=bounded_chars,
            )
        prompt = build_subagent_prompt(
            persona_text, catalog, last_results, history, objective,
            turn + 1, bounded_turns, history_digest, task_contract,
        )
        raw = _call_subagent_llm(provider_handle, prompt, max_out_tok)
        turn_record = {"turn": turn + 1, "prompt": prompt, "raw_response": raw, "tool_calls": []}
        # If the call failed catastrophically, _call_subagent_llm
        # already returned a (subagent ...) string; surface as digest.
        if (
            raw.startswith("(subagent LLM call failed")
            or raw.startswith("(subagent LLM call rate-limited")
            or raw.startswith("(subagent LLM call concurrency-limited")
        ):
            run_record.setdefault("turns", []).append(turn_record)
            failed_status = "rate_limited" if "rate-limited" in raw else "llm_failed"
            if "concurrency-limited" in raw:
                failed_status = "concurrency_limited"
            _finish_run_record(run_record, failed_status, raw)
            return _structured_return(
                raw, run_record, status="error", uncertainty="high",
                next_action="inspect transcript_path or retry later", max_chars=bounded_chars,
            )

        calls = parse_calls(raw)
        turn_record["tool_calls"] = [{"name": n, "args": a} for (n, a) in calls]
        emit_value, emit_error = _extract_final_emit(calls)
        if emit_error:
            turn_record["tool_results"] = emit_error
            run_record.setdefault("turns", []).append(turn_record)
            _finish_run_record(run_record, "emit_protocol_violation", emit_error)
            return _structured_return(
                emit_error, run_record, status="error", uncertainty="high",
                next_action="retry with a well-formed final emit or inspect transcript_path",
                max_chars=bounded_chars,
            )
        if emit_value is not None:
            run_record.setdefault("turns", []).append(turn_record)
            _finish_run_record(run_record, "ok", emit_value)
            return _structured_return(emit_value, run_record, max_chars=bounded_chars)

        last_results = run_tools(
            calls, tool_names, run_record, quota=tool_calls_remaining,
            task_contract=task_contract,
        )
        tool_calls_remaining = run_record.get("tool_calls_remaining", tool_calls_remaining)
        if "QUOTA_EXCEEDED" in last_results:
            turn_record["tool_results"] = last_results
            run_record.setdefault("turns", []).append(turn_record)
            _finish_run_record(run_record, "quota_exceeded", last_results)
            return _structured_return(
                last_results, run_record, status="error", uncertainty="medium",
                next_action="dispatch with a narrower task or higher explicit quota",
                max_chars=bounded_chars,
            )
        if "CANCELLED:" in last_results:
            turn_record["tool_results"] = last_results
            run_record.setdefault("turns", []).append(turn_record)
            _finish_run_record(run_record, "cancelled", last_results)
            return _structured_return(
                last_results, run_record, status="cancelled", uncertainty="low",
                next_action="return to parent", max_chars=bounded_chars,
            )
        turn_record["tool_results"] = last_results
        run_record.setdefault("turns", []).append(turn_record)
        _append_bounded_history(history, (turn + 1, raw, last_results), history_digest)
        run_record["history_digest"] = list(history_digest)

    # Loop exhausted without (emit ...)
    fallback = (
        f"(subagent: max_turns ({bounded_turns}) reached without emit; "
        f"last_results: {_clip(last_results, 500)})"
    )
    _finish_run_record(run_record, "max_turns", fallback)
    return _structured_return(
        fallback, run_record, status="incomplete", uncertainty="medium",
        next_action="review transcript_path or dispatch a narrower follow-up", max_chars=bounded_chars,
    )
