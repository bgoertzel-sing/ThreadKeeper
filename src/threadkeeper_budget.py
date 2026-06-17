"""ThreadKeeper — cost-awareness & escalation seam.

ThreadKeeper's thesis is that *reasoning quality* should be decoupled
from *reasoning frequency*: the cheap local control loop runs every
iteration, and an expensive cloud specialist is invoked only for hard
subproblems. That "only when justified" needs an enforceable policy,
not a hope. This module is that policy seam.

It does three things:

  1. RECORD   — append one usage record per LLM call to a JSONL log
                (carried forward from OmegaClaw's memory/usage.jsonl).
  2. ACCOUNT  — sum tokens (and an example-rate cost estimate) per
                node-role and per thread.
  3. DECIDE   — `should_escalate(...)` weighs cumulative spend against
                the budget thresholds in threadkeeper.config.yaml and
                returns an auditable allow/deny decision.

It is intentionally small and dependency-light (stdlib + optional
PyYAML, which OmegaClaw already requires). The decision logic is a
working v1, but the SEAM is the point: swap in a richer policy
(per-provider rate cards, sliding windows, RL-tuned thresholds)
without touching the call sites.

Wiring: the worker/control loops call `BudgetTracker.record(...)`
after each LLM call, and the control loop calls
`BudgetTracker.should_escalate(...)` before issuing a `(delegate ...)`
to a cloud specialist. `src/subagent.py` already logs usage to the
same JSONL today; this module reads and reasons over it. Integrating
the `record()` call directly into `lib_llm_ext.AIProvider.chat` is the
natural next step and is marked in the README's roadmap.

This module never raises into the agent's reasoning path — every
public method degrades to a safe default if config or logs are
missing.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import yaml  # PyYAML — already in OmegaClaw requirements.txt
except Exception:  # pragma: no cover - degrade gracefully
    yaml = None


# ----------------------------------------------------------------------
# Defaults — used when threadkeeper.config.yaml is absent or unreadable.
# Mirror the values documented in threadkeeper.config.yaml so behavior
# is predictable even without the file.
# ----------------------------------------------------------------------
_DEFAULTS = {
    "thread_token_ceiling": 2_000_000,
    "escalation_soft_fraction": 0.5,
    "min_local_iterations_before_escalation": 2,
    "rates_per_1k_tokens": {
        "control_loop": {"input": 0.0, "output": 0.0},
        "worker_loop": {"input": 0.0, "output": 0.0},
        "cloud_specialist": {"input": 0.015, "output": 0.075},
        "adjudicator": {"input": 0.015, "output": 0.075},
    },
}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_CONFIG_PATH = os.path.join(_REPO_ROOT, "threadkeeper.config.yaml")
_DEFAULT_USAGE_LOG = os.path.join(_REPO_ROOT, "memory", "usage.jsonl")
_DEFAULT_ESCALATION_LOG = os.path.join(_REPO_ROOT, "memory", "escalations.jsonl")


# ----------------------------------------------------------------------
# Data records
# ----------------------------------------------------------------------
@dataclass
class UsageRecord:
    """One LLM call's token accounting."""
    ts: float
    thread_id: str
    node_role: str          # control_loop | worker_loop | cloud_specialist | adjudicator
    model: str
    input_tokens: int
    output_tokens: int

    def cost_estimate(self, rates: dict) -> float:
        r = rates.get(self.node_role) or rates.get("cloud_specialist") or {}
        return (
            (self.input_tokens / 1000.0) * float(r.get("input", 0.0))
            + (self.output_tokens / 1000.0) * float(r.get("output", 0.0))
        )


@dataclass
class EscalationDecision:
    """The auditable result of a should_escalate() call."""
    allowed: bool
    reason: str
    thread_id: str
    spent_tokens: int = 0
    ceiling_tokens: int = 0
    soft_threshold_tokens: int = 0
    local_iterations: int = 0
    ts: float = field(default_factory=time.time)


# ----------------------------------------------------------------------
# The tracker
# ----------------------------------------------------------------------
class BudgetTracker:
    """Tracks token usage per loop and decides escalation against budget.

    Construct once per process (or per thread). Cheap to construct: it
    reads config lazily and never touches the network.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        usage_log: Optional[str] = None,
        escalation_log: Optional[str] = None,
    ):
        self._config_path = config_path or os.environ.get(
            "THREADKEEPER_CONFIG", _DEFAULT_CONFIG_PATH
        )
        self._budget = self._load_budget()
        gov = self._load_governance()
        self.usage_log = usage_log or gov.get("usage_log_abs", _DEFAULT_USAGE_LOG)
        self.escalation_log = escalation_log or gov.get(
            "escalation_log_abs", _DEFAULT_ESCALATION_LOG
        )
        self._record_decisions = gov.get("record_escalation_decisions", True)

    # -- config loading -------------------------------------------------
    def _load_raw_config(self) -> dict:
        if yaml is None or not os.path.isfile(self._config_path):
            return {}
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def _load_budget(self) -> dict:
        cfg = self._load_raw_config().get("budget", {})
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in cfg.items() if v is not None})
        # ensure rates always present
        if "rates_per_1k_tokens" not in merged or not merged["rates_per_1k_tokens"]:
            merged["rates_per_1k_tokens"] = _DEFAULTS["rates_per_1k_tokens"]
        return merged

    def _load_governance(self) -> dict:
        gov = self._load_raw_config().get("governance", {}) or {}
        out = dict(gov)
        # resolve relative log paths against repo root
        if gov.get("usage_log"):
            out["usage_log_abs"] = self._abs(gov["usage_log"])
        if gov.get("escalation_log"):
            out["escalation_log_abs"] = self._abs(gov["escalation_log"])
        return out

    @staticmethod
    def _abs(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(_REPO_ROOT, p)

    # -- recording ------------------------------------------------------
    def record(
        self,
        node_role: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        thread_id: str = "default",
    ) -> UsageRecord:
        """Append one usage record to the JSONL log. Never raises."""
        rec = UsageRecord(
            ts=time.time(),
            thread_id=thread_id,
            node_role=node_role,
            model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
        )
        try:
            os.makedirs(os.path.dirname(self.usage_log), exist_ok=True)
            with open(self.usage_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec)) + "\n")
        except Exception:
            pass  # accounting must never break the response path
        return rec

    def record_from_openai_response(self, node_role, model, resp, thread_id="default"):
        """Convenience: pull usage straight off an OpenAI-style response
        object (`resp.usage.prompt_tokens` / `.completion_tokens`)."""
        u = getattr(resp, "usage", None)
        return self.record(
            node_role=node_role,
            model=model,
            input_tokens=int(getattr(u, "prompt_tokens", 0) or 0) if u else 0,
            output_tokens=int(getattr(u, "completion_tokens", 0) or 0) if u else 0,
            thread_id=thread_id,
        )

    # -- accounting -----------------------------------------------------
    def _iter_records(self, thread_id: Optional[str] = None):
        if not os.path.isfile(self.usage_log):
            return
        try:
            with open(self.usage_log, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if thread_id is not None and d.get("thread_id") != thread_id:
                        continue
                    yield d
        except Exception:
            return

    def spent_tokens(self, thread_id: str = "default") -> int:
        total = 0
        for d in self._iter_records(thread_id):
            total += int(d.get("input_tokens", 0) or 0)
            total += int(d.get("output_tokens", 0) or 0)
        return total

    def spent_cost_estimate(self, thread_id: str = "default") -> float:
        rates = self._budget["rates_per_1k_tokens"]
        total = 0.0
        for d in self._iter_records(thread_id):
            rec = UsageRecord(
                ts=d.get("ts", 0.0),
                thread_id=d.get("thread_id", thread_id),
                node_role=d.get("node_role", "cloud_specialist"),
                model=d.get("model", ""),
                input_tokens=int(d.get("input_tokens", 0) or 0),
                output_tokens=int(d.get("output_tokens", 0) or 0),
            )
            total += rec.cost_estimate(rates)
        return round(total, 6)

    def local_iterations(self, thread_id: str = "default") -> int:
        """Count cheap (control/worker) calls logged for this thread."""
        n = 0
        for d in self._iter_records(thread_id):
            if d.get("node_role") in ("control_loop", "worker_loop"):
                n += 1
        return n

    # -- the decision ---------------------------------------------------
    def should_escalate(
        self,
        thread_id: str = "default",
        subproblem_is_hard: bool = True,
    ) -> EscalationDecision:
        """Decide whether escalation to a cloud specialist is permitted.

        Policy v1:
          * Below `min_local_iterations_before_escalation` cheap loops →
            deny (iterate cheaply first).
          * At/over the hard token ceiling → deny (budget exhausted).
          * Below the soft threshold → allow (escalation is cheap relative
            to the budget).
          * Between soft and hard → allow only if the caller marks the
            subproblem hard.

        The returned decision is auditable and (optionally) logged.
        """
        ceiling = int(self._budget["thread_token_ceiling"])
        soft = int(ceiling * float(self._budget["escalation_soft_fraction"]))
        min_local = int(self._budget["min_local_iterations_before_escalation"])

        spent = self.spent_tokens(thread_id)
        local_iters = self.local_iterations(thread_id)

        def decide(allowed, reason):
            d = EscalationDecision(
                allowed=allowed,
                reason=reason,
                thread_id=thread_id,
                spent_tokens=spent,
                ceiling_tokens=ceiling,
                soft_threshold_tokens=soft,
                local_iterations=local_iters,
            )
            self._maybe_log_decision(d)
            return d

        if local_iters < min_local:
            return decide(
                False,
                f"iterate cheaply first: {local_iters}/{min_local} local "
                "iterations before escalation is allowed",
            )
        if spent >= ceiling:
            return decide(
                False,
                f"budget exhausted: {spent} >= ceiling {ceiling} tokens; "
                "finish on cheap nodes or stop",
            )
        if spent < soft:
            return decide(
                True,
                f"under soft threshold ({spent} < {soft}); escalation freely "
                "permitted",
            )
        if subproblem_is_hard:
            return decide(
                True,
                f"between soft ({soft}) and hard ({ceiling}); escalating "
                "because subproblem flagged hard",
            )
        return decide(
            False,
            f"between soft ({soft}) and hard ({ceiling}); subproblem not "
            "hard enough to justify spend",
        )

    def _maybe_log_decision(self, d: EscalationDecision) -> None:
        if not self._record_decisions:
            return
        try:
            os.makedirs(os.path.dirname(self.escalation_log), exist_ok=True)
            with open(self.escalation_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(d)) + "\n")
        except Exception:
            pass

    # -- summary --------------------------------------------------------
    def summary(self, thread_id: str = "default") -> dict:
        """A compact, human/audit-readable snapshot for the dashboard."""
        ceiling = int(self._budget["thread_token_ceiling"])
        spent = self.spent_tokens(thread_id)
        return {
            "thread_id": thread_id,
            "spent_tokens": spent,
            "ceiling_tokens": ceiling,
            "fraction_used": round(spent / ceiling, 4) if ceiling else None,
            "estimated_cost": self.spent_cost_estimate(thread_id),
            "local_iterations": self.local_iterations(thread_id),
        }


# ----------------------------------------------------------------------
# CLI: `python src/threadkeeper_budget.py [thread_id]` prints a summary
# and the current escalation verdict. Handy for the Quickstart demo.
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    tid = sys.argv[1] if len(sys.argv) > 1 else "default"
    bt = BudgetTracker()
    print("=== ThreadKeeper budget summary ===")
    print(json.dumps(bt.summary(tid), indent=2))
    verdict = bt.should_escalate(tid)
    print("=== escalation verdict ===")
    print(json.dumps(asdict(verdict), indent=2))
