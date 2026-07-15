"""Provider-free lifecycle contract for ThreadKeeper persistent workers.

The authoritative policy is ``persistent_worker_lifecycle.metta``. This
module is the strict, fail-closed parity contract used by storage/process
adapters and hosts where the MeTTa runtime is unavailable. It performs no
provider, tool, process, queue, or filesystem effects.
"""

from dataclasses import dataclass


LIFECYCLE_VERSION = "threadkeeper.persistent-worker.lifecycle.v1"

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
