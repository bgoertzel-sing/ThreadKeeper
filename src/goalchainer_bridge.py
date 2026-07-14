"""GoalChainer decision bridge for OmegaClaw/ProtomegaTron.

Exposes GoalChainer's solve_incident as a py-call skill from OmegaClaw's
MeTTa loop, so ProtomegaTron can request goal-aware decisions through the
existing skill mechanism.

Usage from OmegaClaw MeTTa:
  (goalchainer-decide "incident description")
  (goalchainer-solve "incident description")

Returns a compact JSON string with the recommended action, decisions, and
adjudication status.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add GoalChainer src to path
_GOALCHAINER_SRC = os.environ.get(
    "GOALCHAINER_SRC",
    str(Path(__file__).resolve().parent.parent.parent / "OmegaClaw-GoalChainer" / "src"),
)
if _GOALCHAINER_SRC not in sys.path:
    sys.path.insert(0, _GOALCHAINER_SRC)

try:
    from goal_chainer.pipeline import solve_incident as _solve
    _AVAILABLE = True
    _INIT_ERROR = None
except Exception as e:
    _AVAILABLE = False
    _INIT_ERROR = f"{type(e).__name__}: {e}"


def goalchainer_decide(request: str) -> str:
    """Run GoalChainer decision pipeline and return compact JSON.

    Returns the recommended action, status, and all ranked decisions.
    Does NOT execute any action — just decides and returns the ranking.
    """
    if not _AVAILABLE:
        return json.dumps({"error": "GoalChainer not available", "detail": _INIT_ERROR})

    try:
        result = _solve(request)
        # Return compact summary for OmegaClaw
        compact = {
            "request": result.get("request", request),
            "recommended_action": result.get("decided"),
            "label": result.get("label"),
            "status": result.get("status"),
            "decisions": [
                {
                    "action_id": d.get("action_id"),
                    "label": d.get("label"),
                    "status": d.get("status"),
                    "score": round(d.get("score", 0), 4),
                    "norm_status": d.get("norm_status", "unregulated"),
                }
                for d in result.get("decisions", [])
            ],
        }
        return json.dumps(compact, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"decision failed: {type(e).__name__}: {e}"})


def goalchainer_solve(request: str) -> str:
    """Run GoalChainer full solve (decide + execute) and return JSON.

    In the current canary phase, 'execute' means producing the incident
    artifact and leak-checked deliverable, not sending Telegram messages
    or modifying external systems.
    """
    if not _AVAILABLE:
        return json.dumps({"error": "GoalChainer not available", "detail": _INIT_ERROR})

    try:
        result = _solve(request)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": f"solve failed: {type(e).__name__}: {e}"})


def goalchainer_status() -> str:
    """Check if GoalChainer is available."""
    return json.dumps({
        "available": _AVAILABLE,
        "error": _INIT_ERROR,
        "src": _GOALCHAINER_SRC,
    })
