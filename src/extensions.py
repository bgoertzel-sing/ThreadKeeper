"""Late extension loader for OmegaClaw.

Supports two-phase boot: core startup (Telegram, memory, LLM, basic skills)
finishes first and the bot becomes responsive, then optional heavy extensions
(deontic reasoning, directive task coordination, GoalChainer, etc.) load with
failure isolation so a single broken extension never blocks the bot.

Configuration:
  OMEGACLAW_LATE_EXTENSIONS=deontic,directive,skills_deontic,policy_guard,nal_bridge

Each name maps to a MeTTa import path in EXTENSION_REGISTRY below.
The MeTTa side (lib_extensions.metta) calls these helpers to track status
and wraps each import in catch for failure isolation.
"""

import os
from typing import Dict, List, Set

# Maps extension name -> MeTTa import path (relative to OmegaClaw-Core)
EXTENSION_REGISTRY: Dict[str, str] = {
    "deontic":          "lib_deontic",
    "directive":        "lib_directive",
    "skills_deontic":   "src/skills_deontic",
    "policy_guard":     "src/policy_guard",
    "nal_bridge":       "src/integration/nal",
    # Add new extensions here as they become available.
    # "goalchainer":    "lib_goalchainer",
}

_loaded: Set[str] = set()
_failed: Dict[str, str] = {}
_deferred: Dict[str, str] = {}


def normalize_name(name: str) -> str:
    """Normalize env/config spellings to registry keys."""
    return str(name).strip().replace("-", "_")


def get_late_extensions() -> List[str]:
    """Read OMEGACLAW_LATE_EXTENSIONS env var, return ordered list of registry names."""
    raw = os.environ.get("OMEGACLAW_LATE_EXTENSIONS", "")
    seen = set()
    out: List[str] = []
    for item in raw.split(","):
        name = normalize_name(item)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def is_configured(name: str) -> str:
    """Return "true" iff an extension is requested; string form is MeTTa-stable."""
    return "true" if normalize_name(name) in set(get_late_extensions()) else "false"


def get_extension_path(name: str) -> str:
    """Map extension name to its MeTTa import path (empty string if unknown)."""
    return EXTENSION_REGISTRY.get(normalize_name(name), "")


def mark_loaded(name: str) -> None:
    name = normalize_name(name)
    _loaded.add(name)
    _failed.pop(name, None)
    _deferred.pop(name, None)


def mark_failed(name: str, error: str) -> None:
    name = normalize_name(name)
    _failed[name] = str(error)[:200]
    _loaded.discard(name)
    _deferred.pop(name, None)


def mark_deferred(name: str, reason: str) -> None:
    name = normalize_name(name)
    _deferred[name] = str(reason)[:200]
    _loaded.discard(name)
    _failed.pop(name, None)


def is_loaded(name: str) -> bool:
    return name in _loaded


def status_report() -> str:
    """Human-readable status of all configured late extensions."""
    exts = get_late_extensions()
    if not exts:
        return "No late extensions configured."
    lines = []
    for name in exts:
        if name in _loaded:
            lines.append(f"  loaded:   {name}")
        elif name in _failed:
            lines.append(f"  FAILED:   {name} ({_failed[name]})")
        elif name in _deferred:
            lines.append(f"  deferred: {name} ({_deferred[name]})")
        else:
            lines.append(f"  pending:  {name}")
    return "\n".join(lines)


def list_loaded() -> List[str]:
    """Return list of successfully loaded extension names."""
    return sorted(_loaded)
