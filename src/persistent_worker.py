"""Provider-free lifecycle contract for ThreadKeeper persistent workers.

The authoritative policy is ``persistent_worker_lifecycle.metta``. This
module is the strict, fail-closed parity contract used by storage/process
adapters and hosts where the MeTTa runtime is unavailable. It performs no
provider, tool, process, queue, or filesystem effects.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows parity host
    fcntl = None


LIFECYCLE_VERSION = "threadkeeper.persistent-worker.lifecycle.v1"
MANIFEST_VERSION = "threadkeeper.persistent-worker.task-manifest.v1"
EVENT_VERSION = "threadkeeper.persistent-worker.event.v1"
STATUS_VERSION = "threadkeeper.persistent-worker.status.v1"
ATTEMPT_VERSION = "threadkeeper.persistent-worker.attempt.v1"
CHECKPOINT_VERSION = "threadkeeper.persistent-worker.checkpoint.v1"
RECOVERY_VERSION = "threadkeeper.persistent-worker.recovery.v1"

MAX_MANIFEST_BYTES = 262144
MAX_EVENT_LOG_BYTES = 1048576
MAX_EVENT_LINE_BYTES = 65536
MAX_STATUS_TASKS = 1000
MAX_ATTEMPTS = 1000
MAX_CHECKPOINTS = 1000
MAX_ATTEMPT_BYTES = 65536
MAX_CHECKPOINT_BYTES = 262144

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

STATES = frozenset({
    "CREATED", "QUEUED", "CLAIMED", "RUNNING", "CHECKPOINTED",
    "WAITING_INPUT", "NEEDS_ADJUDICATION", "CANCEL_REQUESTED",
    "FAILED_RETRYABLE", "COMPLETED", "CANCELLED", "FAILED_TERMINAL",
    "EXPIRED",
})

TERMINAL_STATES = frozenset({
    "COMPLETED", "CANCELLED", "FAILED_TERMINAL", "EXPIRED",
})

ALLOWED_TRANSITIONS = frozenset({
    ("CREATED", "QUEUED"),
    ("CREATED", "CANCELLED"),
    ("QUEUED", "CLAIMED"),
    ("QUEUED", "CANCEL_REQUESTED"),
    ("QUEUED", "EXPIRED"),
    ("CLAIMED", "RUNNING"),
    ("CLAIMED", "CANCEL_REQUESTED"),
    ("CLAIMED", "FAILED_RETRYABLE"),
    ("CLAIMED", "FAILED_TERMINAL"),
    ("CLAIMED", "EXPIRED"),
    ("RUNNING", "CHECKPOINTED"),
    ("RUNNING", "WAITING_INPUT"),
    ("RUNNING", "NEEDS_ADJUDICATION"),
    ("RUNNING", "COMPLETED"),
    ("RUNNING", "CANCEL_REQUESTED"),
    ("RUNNING", "FAILED_RETRYABLE"),
    ("RUNNING", "FAILED_TERMINAL"),
    ("RUNNING", "EXPIRED"),
    ("CHECKPOINTED", "QUEUED"),
    ("CHECKPOINTED", "CANCEL_REQUESTED"),
    ("CHECKPOINTED", "FAILED_TERMINAL"),
    ("CHECKPOINTED", "EXPIRED"),
    ("WAITING_INPUT", "QUEUED"),
    ("WAITING_INPUT", "CANCEL_REQUESTED"),
    ("WAITING_INPUT", "FAILED_TERMINAL"),
    ("WAITING_INPUT", "EXPIRED"),
    ("NEEDS_ADJUDICATION", "COMPLETED"),
    ("NEEDS_ADJUDICATION", "CANCEL_REQUESTED"),
    ("NEEDS_ADJUDICATION", "FAILED_TERMINAL"),
    ("NEEDS_ADJUDICATION", "EXPIRED"),
    ("CANCEL_REQUESTED", "CANCELLED"),
    ("CANCEL_REQUESTED", "FAILED_TERMINAL"),
    ("FAILED_RETRYABLE", "QUEUED"),
    ("FAILED_RETRYABLE", "CANCEL_REQUESTED"),
    ("FAILED_RETRYABLE", "FAILED_TERMINAL"),
    ("FAILED_RETRYABLE", "EXPIRED"),
})


@dataclass(frozen=True)
class TransitionDecision:
    allowed: bool
    reason: str
    lifecycle_version: str = LIFECYCLE_VERSION


def is_terminal(state):
    """Return terminality for a known state; unknown input fails closed."""
    return isinstance(state, str) and state in TERMINAL_STATES


def transition_decision(from_state, to_state):
    """Return a deterministic, fail-closed lifecycle transition verdict."""
    if not isinstance(from_state, str) or from_state not in STATES:
        return TransitionDecision(False, "unknown from-state")
    if not isinstance(to_state, str) or to_state not in STATES:
        return TransitionDecision(False, "unknown to-state")
    if from_state in TERMINAL_STATES:
        return TransitionDecision(False, "terminal state cannot transition")
    if from_state == to_state:
        return TransitionDecision(False, "state changes require a distinct target")
    if (from_state, to_state) in ALLOWED_TRANSITIONS:
        return TransitionDecision(True, "allowed by lifecycle policy")
    return TransitionDecision(False, "transition denied by lifecycle policy")


def _validate_id(value, label):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _canonical_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _ensure_directory(path):
    current = os.path.sep
    for part in os.path.abspath(path).strip(os.path.sep).split(os.path.sep):
        if not part:
            continue
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("persistent-worker root must not contain symlinks")


def _open_regular(path, flags, mode=0o600):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None and (stat.S_ISLNK(info.st_mode) or
                             not stat.S_ISREG(info.st_mode)):
        raise ValueError("persistent-worker record must be a regular file")
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("persistent-worker record must be a regular file")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _atomic_create(path, payload):
    parent = os.path.dirname(path)
    _ensure_directory(parent)
    if os.path.lexists(path):
        raise FileExistsError("persistent-worker record already exists")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        # A hard link gives immutable create-if-absent semantics; unlike
        # os.replace it cannot silently overwrite another creator's record.
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _bounded_json_read(path, max_bytes):
    descriptor = _open_regular(path, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as source:
        if os.fstat(source.fileno()).st_size > max_bytes:
            raise ValueError("persistent-worker record exceeds byte limit")
        payload = source.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("persistent-worker record exceeds byte limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid persistent-worker JSON record") from error
    if not isinstance(value, dict):
        raise ValueError("persistent-worker JSON record must be an object")
    return value


def _task_directory(root, task_id):
    _validate_id(task_id, "task id")
    return os.path.join(os.path.abspath(root), "tasks", task_id)


def _parse_timestamp(value, label):
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid {label}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"invalid {label}")
    return parsed


def create_task_manifest(root, manifest):
    """Durably create one immutable v1 task manifest without running it."""
    if not isinstance(manifest, dict):
        raise ValueError("task manifest must be an object")
    record = dict(manifest)
    required = {
        "task_id", "deployment_id", "created_at", "objective", "persona_key",
        "tool_subset", "task_contract", "budgets", "provenance",
    }
    if not required.issubset(record):
        raise ValueError("task manifest is missing required fields")
    _validate_id(record["task_id"], "task id")
    _validate_id(record["deployment_id"], "deployment id")
    if not isinstance(record["objective"], str) or not record["objective"].strip():
        raise ValueError("task objective must be a non-empty string")
    if not isinstance(record["persona_key"], str) or not record["persona_key"].strip():
        raise ValueError("persona key must be a non-empty string")
    if not isinstance(record["tool_subset"], list):
        raise ValueError("tool subset must be a list")
    for field in ("task_contract", "budgets", "provenance"):
        if not isinstance(record[field], dict):
            raise ValueError(f"{field} must be an object")
    record.update({
        "manifest_version": MANIFEST_VERSION,
        "lifecycle_version": LIFECYCLE_VERSION,
        "version": 0,
        "state": "CREATED",
    })
    unsigned = dict(record)
    unsigned.pop("manifest_sha256", None)
    record["manifest_sha256"] = _sha256(unsigned)
    payload = _canonical_bytes(record)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("task manifest exceeds byte limit")
    directory = _task_directory(root, record["task_id"])
    _atomic_create(os.path.join(directory, "manifest.json"), payload)
    return dict(record)


def _read_manifest(root, task_id):
    directory = _task_directory(root, task_id)
    record = _bounded_json_read(
        os.path.join(directory, "manifest.json"), MAX_MANIFEST_BYTES
    )
    digest = record.get("manifest_sha256")
    unsigned = dict(record)
    unsigned.pop("manifest_sha256", None)
    if record.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("unsupported task manifest version")
    if record.get("lifecycle_version") != LIFECYCLE_VERSION:
        raise ValueError("task manifest lifecycle version mismatch")
    if record.get("task_id") != task_id or digest != _sha256(unsigned):
        raise ValueError("task manifest integrity check failed")
    if record.get("state") != "CREATED" or record.get("version") != 0:
        raise ValueError("invalid initial task manifest state")
    return record


def _event_hash(event):
    unsigned = dict(event)
    unsigned.pop("event_sha256", None)
    return _sha256(unsigned)


def _read_events(path):
    try:
        descriptor = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return []
    events = []
    total = 0
    with os.fdopen(descriptor, "rb") as source:
        if os.fstat(source.fileno()).st_size > MAX_EVENT_LOG_BYTES:
            raise ValueError("persistent-worker event log exceeds byte limit")
        for line in source:
            total += len(line)
            if total > MAX_EVENT_LOG_BYTES or len(line) > MAX_EVENT_LINE_BYTES:
                raise ValueError("persistent-worker event log exceeds byte limit")
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("invalid persistent-worker event") from error
            if not isinstance(event, dict):
                raise ValueError("persistent-worker event must be an object")
            if not line.endswith(b"\n"):
                raise ValueError("partial persistent-worker event")
            events.append(event)
    return events


def _replay_events(manifest, events):
    state = "CREATED"
    version = 0
    prior_hash = ""
    for sequence, event in enumerate(events, 1):
        if event.get("event_version") != EVENT_VERSION:
            raise ValueError("unsupported persistent-worker event version")
        if event.get("lifecycle_version") != LIFECYCLE_VERSION:
            raise ValueError("persistent-worker event lifecycle mismatch")
        if event.get("task_id") != manifest["task_id"]:
            raise ValueError("persistent-worker event task mismatch")
        if event.get("sequence") != sequence or event.get("version") != version + 1:
            raise ValueError("persistent-worker event sequence mismatch")
        if event.get("prior_state") != state:
            raise ValueError("persistent-worker event prior state mismatch")
        if event.get("previous_event_sha256") != prior_hash:
            raise ValueError("persistent-worker event chain mismatch")
        if event.get("event_sha256") != _event_hash(event):
            raise ValueError("persistent-worker event integrity check failed")
        decision = transition_decision(state, event.get("new_state"))
        if not decision.allowed:
            raise ValueError("persistent-worker event transition denied")
        state = event["new_state"]
        version = event["version"]
        prior_hash = event["event_sha256"]
    return state, version, prior_hash


def append_task_event(root, task_id, *, event_id, expected_version,
                      prior_state, new_state, actor, timestamp=None,
                      payload_sha256=""):
    """Append one CAS-checked lifecycle event; performs no worker effects."""
    _validate_id(event_id, "event id")
    _validate_id(actor, "event actor")
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        raise ValueError("expected version must be an integer")
    if payload_sha256 and not _SHA256_RE.fullmatch(payload_sha256):
        raise ValueError("invalid payload digest")
    directory = _task_directory(root, task_id)
    manifest = _read_manifest(root, task_id)
    lock_path = os.path.join(directory, "events.lock")
    descriptor = _open_regular(lock_path, os.O_RDWR | os.O_CREAT)
    with os.fdopen(descriptor, "a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        events_path = os.path.join(directory, "events.jsonl")
        events = _read_events(events_path)
        state, version, prior_hash = _replay_events(manifest, events)
        for existing in events:
            if existing.get("event_id") == event_id:
                replay_fields = {
                    "version": expected_version + 1,
                    "prior_state": prior_state,
                    "new_state": new_state,
                    "actor": actor,
                    "payload_sha256": payload_sha256,
                }
                if any(existing.get(key) != value
                       for key, value in replay_fields.items()):
                    raise ValueError("conflicting persistent-worker event replay")
                return dict(existing)
        if version != expected_version or state != prior_state:
            raise ValueError("persistent-worker compare-and-swap failed")
        decision = transition_decision(state, new_state)
        if not decision.allowed:
            raise ValueError("persistent-worker transition denied")
        event = {
            "event_version": EVENT_VERSION,
            "lifecycle_version": LIFECYCLE_VERSION,
            "task_id": task_id,
            "event_id": event_id,
            "sequence": len(events) + 1,
            "version": version + 1,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "prior_state": state,
            "new_state": new_state,
            "payload_sha256": payload_sha256,
            "previous_event_sha256": prior_hash,
        }
        event["event_sha256"] = _event_hash(event)
        line = _canonical_bytes(event)
        if len(line) > MAX_EVENT_LINE_BYTES:
            raise ValueError("persistent-worker event exceeds byte limit")
        output_fd = _open_regular(
            events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
        )
        with os.fdopen(output_fd, "ab") as output:
            if os.fstat(output.fileno()).st_size + len(line) > MAX_EVENT_LOG_BYTES:
                raise ValueError("persistent-worker event log exceeds byte limit")
            output.write(line)
            output.flush()
            os.fsync(output.fileno())
        return dict(event)


def worker_status(root, task_id):
    """Return a bounded, read-only status projection with verified lineage."""
    manifest = _read_manifest(root, task_id)
    events = _read_events(os.path.join(_task_directory(root, task_id), "events.jsonl"))
    state, version, last_hash = _replay_events(manifest, events)
    return {
        "status_version": STATUS_VERSION,
        "task_id": task_id,
        "deployment_id": manifest["deployment_id"],
        "state": state,
        "version": version,
        "terminal": is_terminal(state),
        "created_at": manifest["created_at"],
        "updated_at": events[-1]["timestamp"] if events else manifest["created_at"],
        "event_count": len(events),
        "last_event_id": events[-1]["event_id"] if events else "",
        "last_event_sha256": last_hash,
        "manifest_sha256": manifest["manifest_sha256"],
    }


def list_worker_statuses(root, limit=100):
    """List verified task statuses; one corrupt task fails the read closed."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_STATUS_TASKS:
        raise ValueError("invalid status task limit")
    tasks_root = os.path.join(os.path.abspath(root), "tasks")
    try:
        entries = os.scandir(tasks_root)
    except FileNotFoundError:
        return []
    statuses = []
    with entries:
        for entry in sorted(entries, key=lambda item: item.name):
            _validate_id(entry.name, "task id")
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise ValueError("persistent-worker task entry must be a directory")
            statuses.append(worker_status(root, entry.name))
            if len(statuses) >= limit:
                break
    return statuses


def _attempt_path(root, task_id, attempt_id):
    _validate_id(attempt_id, "attempt id")
    return os.path.join(
        _task_directory(root, task_id), "attempts", f"{attempt_id}.json"
    )


def _read_attempt(root, task_id, attempt_id):
    record = _bounded_json_read(
        _attempt_path(root, task_id, attempt_id), MAX_ATTEMPT_BYTES
    )
    digest = record.get("attempt_sha256")
    unsigned = dict(record)
    unsigned.pop("attempt_sha256", None)
    if record.get("attempt_version") != ATTEMPT_VERSION:
        raise ValueError("unsupported persistent-worker attempt version")
    if record.get("lifecycle_version") != LIFECYCLE_VERSION:
        raise ValueError("persistent-worker attempt lifecycle mismatch")
    if record.get("task_id") != task_id or record.get("attempt_id") != attempt_id:
        raise ValueError("persistent-worker attempt identity mismatch")
    _validate_id(record.get("claim_event_id"), "claim event id")
    _validate_id(record.get("worker_id"), "worker id")
    _parse_timestamp(record.get("created_at"), "attempt creation timestamp")
    _parse_timestamp(record.get("lease_expires_at"), "attempt lease timestamp")
    if (not isinstance(record.get("task_version"), int) or
            isinstance(record.get("task_version"), bool) or
            record["task_version"] < 1):
        raise ValueError("invalid persistent-worker attempt task version")
    if (not isinstance(record.get("sequence"), int) or
            isinstance(record.get("sequence"), bool) or record["sequence"] < 1):
        raise ValueError("invalid persistent-worker attempt sequence")
    if digest != _sha256(unsigned):
        raise ValueError("persistent-worker attempt integrity check failed")
    return record


def list_attempts(root, task_id):
    """Return verified immutable attempts in creation order."""
    directory = os.path.join(_task_directory(root, task_id), "attempts")
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return []
    records = []
    with entries:
        for entry in entries:
            if entry.name == "attempts.lock":
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise ValueError("persistent-worker attempt entry must be a file")
            if not entry.name.endswith(".json"):
                raise ValueError("invalid persistent-worker attempt entry")
            attempt_id = entry.name[:-5]
            records.append(_read_attempt(root, task_id, attempt_id))
            if len(records) > MAX_ATTEMPTS:
                raise ValueError("persistent-worker attempt count exceeds limit")
    records.sort(key=lambda item: item["sequence"])
    previous = ""
    for sequence, record in enumerate(records, 1):
        if record["sequence"] != sequence:
            raise ValueError("persistent-worker attempt sequence mismatch")
        if record.get("prior_attempt_sha256") != previous:
            raise ValueError("persistent-worker attempt chain mismatch")
        previous = record["attempt_sha256"]
    return records


def create_attempt(root, task_id, *, attempt_id, claim_event_id, worker_id,
                   lease_expires_at, created_at=None):
    """Create one immutable attempt/lease after a successful lifecycle claim."""
    _validate_id(attempt_id, "attempt id")
    _validate_id(claim_event_id, "claim event id")
    _validate_id(worker_id, "worker id")
    status = worker_status(root, task_id)
    if status["state"] != "CLAIMED":
        raise ValueError("persistent-worker attempt requires a claimed task")
    if status["last_event_id"] != claim_event_id:
        raise ValueError("persistent-worker attempt claim event mismatch")
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    created = _parse_timestamp(created_at, "attempt creation timestamp")
    expires = _parse_timestamp(lease_expires_at, "attempt lease timestamp")
    if expires <= created:
        raise ValueError("persistent-worker attempt lease must expire after creation")
    attempt_directory = os.path.join(_task_directory(root, task_id), "attempts")
    _ensure_directory(attempt_directory)
    lock_path = os.path.join(attempt_directory, "attempts.lock")
    descriptor = _open_regular(lock_path, os.O_RDWR | os.O_CREAT)
    with os.fdopen(descriptor, "a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        attempts = list_attempts(root, task_id)
        if os.path.lexists(_attempt_path(root, task_id, attempt_id)):
            existing = _read_attempt(root, task_id, attempt_id)
            if (existing["claim_event_id"] != claim_event_id or
                    existing["worker_id"] != worker_id or
                    existing["lease_expires_at"] != lease_expires_at):
                raise ValueError("conflicting persistent-worker attempt replay")
            return existing
        if len(attempts) >= MAX_ATTEMPTS:
            raise ValueError("persistent-worker attempt count exceeds limit")
        record = {
            "attempt_version": ATTEMPT_VERSION,
            "lifecycle_version": LIFECYCLE_VERSION,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "claim_event_id": claim_event_id,
            "worker_id": worker_id,
            "sequence": len(attempts) + 1,
            "task_version": status["version"],
            "manifest_sha256": status["manifest_sha256"],
            "created_at": created_at,
            "lease_expires_at": lease_expires_at,
            "prior_attempt_sha256": attempts[-1]["attempt_sha256"] if attempts else "",
        }
        record["attempt_sha256"] = _sha256(record)
        payload = _canonical_bytes(record)
        if len(payload) > MAX_ATTEMPT_BYTES:
            raise ValueError("persistent-worker attempt exceeds byte limit")
        _atomic_create(_attempt_path(root, task_id, attempt_id), payload)
    return dict(record)


def _checkpoint_directory(root, task_id):
    return os.path.join(_task_directory(root, task_id), "checkpoints")


def _read_checkpoint(root, task_id, checkpoint_id):
    _validate_id(checkpoint_id, "checkpoint id")
    path = os.path.join(_checkpoint_directory(root, task_id), f"{checkpoint_id}.json")
    record = _bounded_json_read(path, MAX_CHECKPOINT_BYTES)
    digest = record.get("checkpoint_sha256")
    unsigned = dict(record)
    unsigned.pop("checkpoint_sha256", None)
    if record.get("checkpoint_version") != CHECKPOINT_VERSION:
        raise ValueError("unsupported persistent-worker checkpoint version")
    if record.get("lifecycle_version") != LIFECYCLE_VERSION:
        raise ValueError("persistent-worker checkpoint lifecycle mismatch")
    if record.get("task_id") != task_id or record.get("checkpoint_id") != checkpoint_id:
        raise ValueError("persistent-worker checkpoint identity mismatch")
    _validate_id(record.get("attempt_id"), "attempt id")
    _parse_timestamp(record.get("created_at"), "checkpoint timestamp")
    if not isinstance(record.get("payload"), dict):
        raise ValueError("persistent-worker checkpoint payload must be an object")
    if record.get("payload_sha256") != _sha256(record["payload"]):
        raise ValueError("persistent-worker checkpoint payload integrity check failed")
    if digest != _sha256(unsigned):
        raise ValueError("persistent-worker checkpoint integrity check failed")
    return record


def read_checkpoint_chain(root, task_id):
    """Return a bounded, verified immutable checkpoint chain."""
    directory = _checkpoint_directory(root, task_id)
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return []
    records = []
    with entries:
        for entry in entries:
            if entry.name == "checkpoints.lock":
                continue
            if (entry.is_symlink() or not entry.is_file(follow_symlinks=False) or
                    not entry.name.endswith(".json")):
                raise ValueError("invalid persistent-worker checkpoint entry")
            records.append(_read_checkpoint(root, task_id, entry.name[:-5]))
            if len(records) > MAX_CHECKPOINTS:
                raise ValueError("persistent-worker checkpoint count exceeds limit")
    records.sort(key=lambda item: item["sequence"])
    previous = ""
    for sequence, record in enumerate(records, 1):
        if record.get("sequence") != sequence:
            raise ValueError("persistent-worker checkpoint sequence mismatch")
        if record.get("previous_checkpoint_sha256") != previous:
            raise ValueError("persistent-worker checkpoint chain mismatch")
        previous = record["checkpoint_sha256"]
    return records


def create_checkpoint(root, task_id, *, checkpoint_id, attempt_id, payload,
                      created_at=None):
    """Atomically append bounded worker state to the immutable checkpoint chain."""
    _validate_id(checkpoint_id, "checkpoint id")
    attempt = _read_attempt(root, task_id, attempt_id)
    status = worker_status(root, task_id)
    if status["state"] not in {"RUNNING", "CHECKPOINTED"}:
        raise ValueError("persistent-worker checkpoint requires a running task")
    if attempt["manifest_sha256"] != status["manifest_sha256"]:
        raise ValueError("persistent-worker checkpoint manifest mismatch")
    attempts = list_attempts(root, task_id)
    if not attempts or attempts[-1]["attempt_id"] != attempt_id:
        raise ValueError("persistent-worker checkpoint requires latest attempt")
    if not isinstance(payload, dict):
        raise ValueError("persistent-worker checkpoint payload must be an object")
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    _parse_timestamp(created_at, "checkpoint timestamp")
    directory = _checkpoint_directory(root, task_id)
    _ensure_directory(directory)
    lock_path = os.path.join(directory, "checkpoints.lock")
    descriptor = _open_regular(lock_path, os.O_RDWR | os.O_CREAT)
    with os.fdopen(descriptor, "a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        checkpoints = read_checkpoint_chain(root, task_id)
        record = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "lifecycle_version": LIFECYCLE_VERSION,
            "task_id": task_id,
            "checkpoint_id": checkpoint_id,
            "attempt_id": attempt_id,
            "sequence": len(checkpoints) + 1,
            "task_version": status["version"],
            "created_at": created_at,
            "payload": payload,
            "payload_sha256": _sha256(payload),
            "previous_checkpoint_sha256": (
                checkpoints[-1]["checkpoint_sha256"] if checkpoints else ""
            ),
        }
        record["checkpoint_sha256"] = _sha256(record)
        encoded = _canonical_bytes(record)
        if len(encoded) > MAX_CHECKPOINT_BYTES:
            raise ValueError("persistent-worker checkpoint exceeds byte limit")
        path = os.path.join(directory, f"{checkpoint_id}.json")
        try:
            _atomic_create(path, encoded)
        except FileExistsError:
            existing = _read_checkpoint(root, task_id, checkpoint_id)
            replay = dict(record)
            replay["sequence"] = existing.get("sequence")
            replay["previous_checkpoint_sha256"] = existing.get(
                "previous_checkpoint_sha256"
            )
            replay["checkpoint_sha256"] = _sha256({
                key: value for key, value in replay.items()
                if key != "checkpoint_sha256"
            })
            if existing != replay:
                raise ValueError("conflicting persistent-worker checkpoint replay")
            return existing
    return dict(record)


def recovery_assessment(root, task_id, *, now=None):
    """Assess restart recovery from durable state without running any effect."""
    status = worker_status(root, task_id)
    now_value = now or datetime.now(timezone.utc).isoformat()
    current = _parse_timestamp(now_value, "recovery timestamp")
    attempts = list_attempts(root, task_id)
    checkpoints = read_checkpoint_chain(root, task_id)
    result = {
        "recovery_version": RECOVERY_VERSION,
        "task_id": task_id,
        "state": status["state"],
        "task_version": status["version"],
        "recoverable": False,
        "reason": "task state does not require stale-attempt recovery",
        "attempt_id": attempts[-1]["attempt_id"] if attempts else "",
        "checkpoint_id": checkpoints[-1]["checkpoint_id"] if checkpoints else "",
    }
    if status["state"] not in {"CLAIMED", "RUNNING"}:
        return result
    if not attempts:
        result["reason"] = "claimed task has no durable attempt"
        return result
    latest = attempts[-1]
    if latest["manifest_sha256"] != status["manifest_sha256"]:
        result["reason"] = "attempt manifest mismatch"
        return result
    if checkpoints and checkpoints[-1]["attempt_id"] != latest["attempt_id"]:
        result["reason"] = "latest checkpoint belongs to another attempt"
        return result
    if current <= _parse_timestamp(latest["lease_expires_at"], "attempt lease timestamp"):
        result["reason"] = "attempt lease is still active"
        return result
    result["recoverable"] = True
    result["reason"] = "attempt lease expired with verified durable lineage"
    return result


def recover_stale_attempt(root, task_id, *, recovery_id, actor="supervisor",
                          now=None):
    """Record an expired attempt as retryable; requeue remains a separate effect."""
    _validate_id(recovery_id, "recovery id")
    status = worker_status(root, task_id)
    if status["state"] == "FAILED_RETRYABLE" and status["last_event_id"] == recovery_id:
        attempts = list_attempts(root, task_id)
        checkpoints = read_checkpoint_chain(root, task_id)
        return {
            "recovery_version": RECOVERY_VERSION,
            "task_id": task_id,
            "state": "FAILED_RETRYABLE",
            "task_version": status["version"],
            "recoverable": True,
            "reason": "expired attempt recorded for explicit requeue",
            "attempt_id": attempts[-1]["attempt_id"] if attempts else "",
            "checkpoint_id": checkpoints[-1]["checkpoint_id"] if checkpoints else "",
        }
    assessment = recovery_assessment(root, task_id, now=now)
    if not assessment["recoverable"]:
        return assessment
    status = worker_status(root, task_id)
    attempt = _read_attempt(root, task_id, assessment["attempt_id"])
    append_task_event(
        root, task_id, event_id=recovery_id,
        expected_version=status["version"], prior_state=status["state"],
        new_state="FAILED_RETRYABLE", actor=actor,
        payload_sha256=attempt["attempt_sha256"],
    )
    assessment = dict(assessment)
    assessment["state"] = "FAILED_RETRYABLE"
    assessment["task_version"] = status["version"] + 1
    assessment["reason"] = "expired attempt recorded for explicit requeue"
    return assessment


def requeue_persistent(root, task_id, *, requeue_id, actor="supervisor",
                       enqueue=None):
    """Explicitly requeue a verified retryable task without running it.

    Recovery and requeue are deliberately separate effects.  This function
    verifies the immutable attempt/checkpoint lineage before recreating the
    normal bounded queue record, then records the queue digest in a lifecycle
    CAS event.  It never claims work, calls a provider, or runs a tool.
    """
    _validate_id(requeue_id, "requeue id")
    status = worker_status(root, task_id)
    if status["state"] == "QUEUED" and status["last_event_id"] == requeue_id:
        return status
    if status["state"] != "FAILED_RETRYABLE":
        raise ValueError("persistent task is not explicitly requeueable")

    manifest = _read_manifest(root, task_id)
    attempts = list_attempts(root, task_id)
    checkpoints = read_checkpoint_chain(root, task_id)
    if not attempts:
        raise ValueError("retryable persistent task has no durable attempt")
    latest_attempt = attempts[-1]
    if latest_attempt["manifest_sha256"] != manifest["manifest_sha256"]:
        raise ValueError("persistent-worker requeue attempt manifest mismatch")
    if checkpoints and checkpoints[-1]["attempt_id"] != latest_attempt["attempt_id"]:
        raise ValueError("persistent-worker requeue checkpoint lineage mismatch")

    adapter = enqueue or _subagent_module().enqueue_persistent_dispatch
    result_text = adapter(
        task_id,
        manifest["objective"],
        ",".join(manifest["tool_subset"]),
        manifest["persona_key"],
        manifest["budgets"].get("max_turns"),
        manifest["budgets"].get("max_result_chars"),
    )
    try:
        result = json.loads(result_text) if isinstance(result_text, str) else result_text
    except json.JSONDecodeError as error:
        raise ValueError("persistent requeue returned invalid JSON") from error
    if not isinstance(result, dict) or result.get("status") != "queued":
        raise ValueError("persistent requeue did not produce a queued task")
    queue_sha256 = result.get("queue_sha256", "")
    if not _SHA256_RE.fullmatch(str(queue_sha256)):
        raise ValueError("persistent requeue omitted queue integrity digest")

    append_task_event(
        root, task_id, event_id=requeue_id,
        expected_version=status["version"], prior_state="FAILED_RETRYABLE",
        new_state="QUEUED", actor=actor, payload_sha256=queue_sha256,
    )
    return worker_status(root, task_id)


def _subagent_module():
    # Lazy import keeps lifecycle/status inspection provider-free and makes the
    # queue effects seam explicit in tests.
    import subagent
    return subagent


def _same_manifest(existing, requested):
    for field in (
        "task_id", "deployment_id", "created_at", "objective", "persona_key",
        "tool_subset", "task_contract", "budgets", "provenance",
    ):
        if existing.get(field) != requested.get(field):
            return False
    return True


def spawn_persistent(root, manifest, *, spawn_id, actor="parent",
                     enqueue=None):
    """Create and queue a persistent task through validated bounded dispatch.

    This stops at the existing queue boundary: it never claims work, starts a
    process, calls a provider, or runs a tool. A failed enqueue leaves a
    visible CREATED manifest that the same idempotent spawn request may retry.
    """
    _validate_id(spawn_id, "spawn id")
    if not isinstance(manifest, dict):
        raise ValueError("task manifest must be an object")
    task_id = _validate_id(manifest.get("task_id"), "task id")
    try:
        stored = create_task_manifest(root, manifest)
    except FileExistsError:
        stored = _read_manifest(root, task_id)
        if not _same_manifest(stored, manifest):
            raise ValueError("conflicting persistent task spawn replay")
    status = worker_status(root, task_id)
    if status["state"] == "QUEUED":
        return status
    if status["state"] != "CREATED":
        raise ValueError("persistent task is not spawnable")
    adapter = enqueue or _subagent_module().enqueue_persistent_dispatch
    result_text = adapter(
        task_id,
        stored["objective"],
        ",".join(stored["tool_subset"]),
        stored["persona_key"],
        stored["budgets"].get("max_turns"),
        stored["budgets"].get("max_result_chars"),
    )
    try:
        result = json.loads(result_text) if isinstance(result_text, str) else result_text
    except json.JSONDecodeError as error:
        raise ValueError("persistent enqueue returned invalid JSON") from error
    if not isinstance(result, dict) or result.get("status") != "queued":
        raise ValueError("persistent enqueue did not produce a queued task")
    payload_sha256 = result.get("queue_sha256", "")
    if not _SHA256_RE.fullmatch(str(payload_sha256)):
        raise ValueError("persistent enqueue omitted queue integrity digest")
    append_task_event(
        root, task_id, event_id=spawn_id, expected_version=0,
        prior_state="CREATED", new_state="QUEUED", actor=actor,
        payload_sha256=payload_sha256,
    )
    return worker_status(root, task_id)


def cancel_persistent(root, task_id, *, cancel_id, actor="parent",
                      request_cancel=None):
    """Durably request cancellation before recording the lifecycle event."""
    _validate_id(cancel_id, "cancel id")
    status = worker_status(root, task_id)
    if status["terminal"] or status["state"] == "CANCEL_REQUESTED":
        return status
    if status["state"] == "CREATED":
        append_task_event(
            root, task_id, event_id=cancel_id, expected_version=status["version"],
            prior_state="CREATED", new_state="CANCELLED", actor=actor,
        )
        return worker_status(root, task_id)
    adapter = request_cancel or _subagent_module().request_persistent_dispatch_cancel
    adapter(task_id)  # safety first: a crash here can only leave cancellation stricter
    # Cancellation may race a claim. The token is already durable, so retry
    # the lifecycle CAS from the winning nonterminal state; this records the
    # intervention without permitting a later provider/tool effect.
    for _attempt in range(3):
        status = worker_status(root, task_id)
        if status["terminal"] or status["state"] == "CANCEL_REQUESTED":
            return status
        try:
            append_task_event(
                root, task_id, event_id=cancel_id,
                expected_version=status["version"],
                prior_state=status["state"], new_state="CANCEL_REQUESTED",
                actor=actor,
            )
            return worker_status(root, task_id)
        except ValueError as error:
            if "compare-and-swap" not in str(error):
                raise
    raise ValueError("persistent cancellation compare-and-swap retry exhausted")


def run_persistent_queued_dispatch(root, task_id, *, claim_id,
                                   cancel_present=None, run_queued=None,
                                   attempt_id=None, worker_id="worker",
                                   lease_seconds=300):
    """Claim through lifecycle CAS only if cancellation has not won the race."""
    _validate_id(claim_id, "claim id")
    _validate_id(attempt_id or claim_id, "attempt id")
    _validate_id(worker_id, "worker id")
    if (not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or
            not 1 <= lease_seconds <= 86400):
        raise ValueError("persistent-worker lease seconds out of range")
    status = worker_status(root, task_id)
    if status["state"] != "QUEUED":
        return {"status": "not_claimed", "task": status}
    subagent = _subagent_module() if cancel_present is None or run_queued is None else None
    cancel_check = cancel_present or (
        lambda value: os.path.isfile(subagent.persistent_dispatch_cancel_path(value))
    )
    if cancel_check(task_id):
        return {"status": "cancelled_before_claim", "task": worker_status(root, task_id)}
    append_task_event(
        root, task_id, event_id=claim_id, expected_version=status["version"],
        prior_state="QUEUED", new_state="CLAIMED", actor="worker",
    )
    attempt_id = attempt_id or claim_id
    created = datetime.now(timezone.utc)
    attempt = create_attempt(
        root, task_id, attempt_id=attempt_id, claim_event_id=claim_id,
        worker_id=worker_id, created_at=created.isoformat(),
        lease_expires_at=(created + timedelta(seconds=lease_seconds)).isoformat(),
    )
    runner = run_queued or subagent.run_queued_dispatch
    queue_path = os.path.join(
        subagent._dispatch_queue_dir(), f"persistent-{task_id}.json"
    ) if subagent is not None else f"persistent-{task_id}.json"
    return {"status": "claimed", "task": worker_status(root, task_id),
            "attempt": attempt,
            "queue_result": runner(queue_path)}
