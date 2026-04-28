import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_RISK_PATH = os.path.join(REPO_ROOT, "memory", "risks.jsonl")
RISK_PATH = os.environ.get("OMEGACLAW_RISK_REGISTER", DEFAULT_RISK_PATH)

_VALID_STATUS = {"open", "monitoring", "treating", "accepted", "closed"}
_VALID_TIERS = {"low", "medium", "high", "critical"}

# _DEMO_RISKS now loaded from memory/ecosystem.json (see _load_ecosystem_config below)


# _DEMO_REPORTS now loaded from memory/ecosystem.json (see _load_ecosystem_config below)


# _ECOSYSTEM_NODES now loaded from memory/ecosystem.json (see _load_ecosystem_config below)

# _ECOSYSTEM_EDGES now loaded from memory/ecosystem.json (see _load_ecosystem_config below)



# CAPTAIN-PATCH: ecosystem-and-demo data has been moved out of the source
# tree. The repo no longer ships with SingularityNET-flavored demo content.
# Live deployments configure memory/ecosystem.json (gitignored) with their
# own org chart, demo risks, and demo reports. The committed
# memory/ecosystem.example.json shows the schema with empty payloads.
DEFAULT_ECOSYSTEM_PATH = os.path.join(REPO_ROOT, "memory", "ecosystem.json")
ECOSYSTEM_PATH = os.environ.get("OMEGACLAW_ECOSYSTEM", DEFAULT_ECOSYSTEM_PATH)
ECOSYSTEM_EXAMPLE_PATH = os.path.join(REPO_ROOT, "memory", "ecosystem.example.json")

_ECOSYSTEM_CACHE = {"path": None, "mtime": 0, "data": None}


def _load_ecosystem_config():
    """Return (config_dict, source_path). Reads memory/ecosystem.json if
    present, else memory/ecosystem.example.json, else returns an empty
    skeleton. Cached on mtime."""
    for candidate in (ECOSYSTEM_PATH, ECOSYSTEM_EXAMPLE_PATH):
        if not candidate or not os.path.isfile(candidate):
            continue
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        cache = _ECOSYSTEM_CACHE
        if cache["path"] == candidate and cache["mtime"] == st.st_mtime and cache["data"] is not None:
            return cache["data"], candidate
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            data = {}
        data.setdefault("description", "")
        data.setdefault("demo", False)
        data.setdefault("nodes", [])
        data.setdefault("edges", [])
        data.setdefault("reports", [])
        data.setdefault("risks", [])
        _ECOSYSTEM_CACHE.update({"path": candidate, "mtime": st.st_mtime, "data": data})
        return data, candidate
    empty = {"description": "", "demo": False, "nodes": [], "edges": [], "reports": [], "risks": []}
    return empty, None


def _ecosystem_nodes():
    return _load_ecosystem_config()[0].get("nodes") or []


def _ecosystem_edges():
    return _load_ecosystem_config()[0].get("edges") or []


def _demo_reports():
    return _load_ecosystem_config()[0].get("reports") or []


def _demo_risks():
    return _load_ecosystem_config()[0].get("risks") or []


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_store():
    os.makedirs(os.path.dirname(RISK_PATH), exist_ok=True)
    if not os.path.exists(RISK_PATH):
        with open(RISK_PATH, "w", encoding="utf-8"):
            pass


def _slug(text):
    text = (text or "risk").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text or "risk")[:36]


def _coerce_int(value, default=0):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(value, 5))


def _risk_tier(priority):
    if priority >= 20:
        return "critical"
    if priority >= 12:
        return "high"
    if priority >= 6:
        return "medium"
    return "low"


def _load():
    _ensure_store()
    rows = []
    with open(RISK_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _write(rows):
    _ensure_store()
    fd, tmp = tempfile.mkstemp(prefix=".risks.", suffix=".jsonl", dir=os.path.dirname(RISK_PATH))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(tmp, RISK_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _parse_payload(payload):
    if isinstance(payload, dict):
        return dict(payload)
    if payload is None:
        return {}
    text = str(payload).strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"title": text, "description": text}


def _normalize(entry, existing=None):
    base = dict(existing or {})
    base.update({k: v for k, v in entry.items() if v is not None})

    title = str(base.get("title") or base.get("use_case") or "Untitled risk").strip()
    likelihood = _coerce_int(base.get("likelihood"), 0)
    impact = _coerce_int(base.get("impact"), 0)
    priority = likelihood * impact if likelihood and impact else _coerce_int(base.get("priority"), 0)
    tier = str(base.get("risk_tier") or base.get("tier") or _risk_tier(priority)).lower()
    status = str(base.get("status") or "open").lower()

    if tier not in _VALID_TIERS:
        tier = _risk_tier(priority)
    if status not in _VALID_STATUS:
        status = "open"

    now = _now()
    risk_id = base.get("id") or base.get("risk_id")
    if not risk_id:
        risk_id = f"RISK-{int(time.time())}-{_slug(title)}"

    return {
        "id": str(risk_id),
        "title": title,
        "description": str(base.get("description") or "").strip(),
        "use_case": str(base.get("use_case") or "").strip(),
        "model_provider": str(base.get("model_provider") or "").strip(),
        "model_name": str(base.get("model_name") or "").strip(),
        "framework": str(base.get("framework") or "NIST AI RMF / ISO 42001 / NIST IR 8286").strip(),
        "evidence_sources": base.get("evidence_sources") or [],
        "recommendation": str(base.get("recommendation") or "").strip(),
        "likelihood": likelihood,
        "impact": impact,
        "priority": priority,
        "risk_tier": tier,
        "status": status,
        "required_human_approval": str(base.get("required_human_approval") or "").strip(),
        "residual_risk": str(base.get("residual_risk") or "").strip(),
        "decision_owner": str(base.get("decision_owner") or base.get("owner") or "").strip(),
        "next_review_date": str(base.get("next_review_date") or "").strip(),
        "treatment": str(base.get("treatment") or "").strip(),
        "control_mapping": base.get("control_mapping") or [],
        "created_at": base.get("created_at") or now,
        "updated_at": now,
    }


def _public_rows(rows):
    return sorted(rows, key=lambda r: (r.get("status") == "closed", -int(r.get("priority") or 0), r.get("title", "")))


def append_risk(payload):
    rows = _load()
    entry = _normalize(_parse_payload(payload))
    rows.append(entry)
    _write(rows)
    return json.dumps({"ok": True, "action": "append", "risk": entry}, ensure_ascii=False)


def list_risks(filter_text=""):
    rows = _public_rows(_load())
    filt = str(filter_text or "").strip().lower()
    if filt:
        rows = [r for r in rows if filt in json.dumps(r, ensure_ascii=False).lower()]
    return json.dumps({"ok": True, "count": len(rows), "risks": rows}, ensure_ascii=False)


def get_risk(risk_id):
    risk_id = str(risk_id or "").strip()
    for row in _load():
        if row.get("id") == risk_id:
            return json.dumps({"ok": True, "risk": row}, ensure_ascii=False)
    return json.dumps({"ok": False, "err": f"risk not found: {risk_id}"}, ensure_ascii=False)


def update_risk(risk_id, payload):
    risk_id = str(risk_id or "").strip()
    rows = _load()
    patch = _parse_payload(payload)
    for idx, row in enumerate(rows):
        if row.get("id") == risk_id:
            rows[idx] = _normalize(patch, existing=row)
            _write(rows)
            return json.dumps({"ok": True, "action": "update", "risk": rows[idx]}, ensure_ascii=False)
    return json.dumps({"ok": False, "err": f"risk not found: {risk_id}"}, ensure_ascii=False)


def dashboard_data():
    rows = _public_rows(_load())
    open_rows = [r for r in rows if r.get("status") != "closed"]
    by_tier = {tier: 0 for tier in ("low", "medium", "high", "critical")}
    heatmap = [[0 for _ in range(5)] for _ in range(5)]
    attention = []
    for row in open_rows:
        tier = row.get("risk_tier") or "low"
        by_tier[tier] = by_tier.get(tier, 0) + 1
        likelihood = _coerce_int(row.get("likelihood"), 0)
        impact = _coerce_int(row.get("impact"), 0)
        if likelihood and impact:
            heatmap[impact - 1][likelihood - 1] += 1
        missing = []
        for field in ("evidence_sources", "decision_owner", "treatment", "next_review_date"):
            if not row.get(field):
                missing.append(field)
        if missing or row.get("risk_tier") in ("high", "critical"):
            copy = dict(row)
            copy["attention_reasons"] = missing
            attention.append(copy)
    return json.dumps({
        "ok": True,
        "path": RISK_PATH,
        "total": len(rows),
        "open": len(open_rows),
        "by_tier": by_tier,
        "top_risks": open_rows[:10],
        "attention": attention[:10],
        "heatmap": heatmap,
        "updated_at": _now(),
    }, ensure_ascii=False)


def seed_demo_data():
    rows = _load()
    by_id = {row.get("id"): idx for idx, row in enumerate(rows)}
    changed = False
    inserted = 0
    refreshed = 0
    for demo in _demo_risks():
        if demo["id"] in by_id:
            existing = rows[by_id[demo["id"]]]
            rows[by_id[demo["id"]]] = _normalize(demo, existing={"created_at": existing.get("created_at"), "id": demo["id"]})
            refreshed += 1
            changed = True
        else:
            rows.append(_normalize(demo))
            inserted += 1
            changed = True
    if changed:
        _write(rows)
    return json.dumps({
        "ok": True,
        "inserted": inserted,
        "refreshed": refreshed,
        "total": len(_load()),
        "demo": True,
    }, ensure_ascii=False)


def ecosystem_data():
    cfg, _ = _load_ecosystem_config()
    dashboard = json.loads(dashboard_data())
    return json.dumps({
        "ok": True,
        "demo": bool(cfg.get("demo")),
        "description": cfg.get("description") or "",
        "nodes": _ecosystem_nodes(),
        "edges": _ecosystem_edges(),
        "reports": _demo_reports(),
        "dashboard": dashboard,
    }, ensure_ascii=False)


def org_data(org_id):
    org_id = str(org_id or "").strip().lower()
    nodes = {node["id"]: node for node in _ecosystem_nodes()}
    node = nodes.get(org_id)
    if not node:
        return json.dumps({"ok": False, "err": f"unknown org: {org_id}"}, ensure_ascii=False)
    label = node["label"].lower()
    risks = []
    for risk in _load():
        text = " ".join([
            str(risk.get("id", "")),
            str(risk.get("title", "")),
            str(risk.get("description", "")),
            str(risk.get("use_case", "")),
            str(risk.get("framework", "")),
        ]).lower()
        if org_id in text or label in text:
            risks.append(risk)
    reports = [
        report for report in _demo_reports()
        if report.get("source", "").lower().startswith(label)
        or org_id in report.get("mapped_risk", "").lower()
        or label in report.get("summary", "").lower()
    ]
    incoming = [edge for edge in _ecosystem_edges() if edge.get("to") == org_id]
    outgoing = [edge for edge in _ecosystem_edges() if edge.get("from") == org_id]
    return json.dumps({
        "ok": True,
        "demo": True,
        "org": node,
        "risks": _public_rows(risks),
        "reports": reports,
        "incoming": incoming,
        "outgoing": outgoing,
    }, ensure_ascii=False)


def risk_register(action, payload=""):
    action = str(action or "list").strip().lower()
    if action in ("append", "add", "create"):
        return append_risk(payload)
    if action in ("list", "query", "search"):
        return list_risks(payload)
    if action in ("get", "read"):
        return get_risk(payload)
    if action in ("dashboard", "summary", "summarize"):
        return dashboard_data()
    if action in ("seed-demo", "demo"):
        return seed_demo_data()
    if action in ("ecosystem", "ecosystem-demo"):
        return ecosystem_data()
    return json.dumps({
        "ok": False,
        "err": f"unknown risk-register action: {action}",
        "valid_actions": ["append", "list", "get", "update", "dashboard"],
    }, ensure_ascii=False)


def risk_register_update(risk_id, payload):
    return update_risk(risk_id, payload)


def _recent_history_anchors(limit=6):
    path = os.path.join(REPO_ROOT, "memory", "history.metta")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()[-60000:]
    except OSError:
        return []
    anchors = []
    for m in re.finditer(r"HUMAN_MESSAGE:\s*(.*?)(?:_newline_|\n|\)\s*\n)", text, re.S):
        msg = re.sub(r"\s+", " ", m.group(1)).strip()
        msg = msg.replace("_quote_", '"').replace("_apostrophe_", "'").replace("_newline_", " ")
        if msg and msg not in anchors:
            anchors.append(msg[:240])
    if not anchors:
        for m in re.finditer(r"\(send\s+\(text\s+_quote_(.*?)_quote_\)\)", text, re.S):
            msg = m.group(1).replace("_newline_", " ").replace("_apostrophe_", "'")
            msg = re.sub(r"\s+", " ", msg).strip()
            if msg and msg not in anchors:
                anchors.append(msg[:240])
    return anchors[-limit:]


def context_snapshot():
    """Small deterministic continuity layer included before raw history.

    This survives provider/model switches and gives small/local models a stable
    map of identity, governance posture, and recent anchors without requiring
    them to parse the entire historical tail.
    """
    rows = _load()
    top = sorted(rows, key=lambda r: int(r.get("priority") or 0), reverse=True)[:5]
    tiers = {}
    for row in rows:
        if row.get("status", "open") != "closed":
            tiers[row.get("risk_tier", "low")] = tiers.get(row.get("risk_tier", "low"), 0) + 1
    risk_lines = [
        f"{r.get('id')}: {r.get('title')} [{str(r.get('risk_tier', 'low')).upper()} P{r.get('priority', 0)} owner={r.get('decision_owner', 'unassigned')}]"
        for r in top
    ]
    anchors = _recent_history_anchors()
    parts = [
        "Identity: Ellie is Captain Larry's active local Oma agent and Chief Ethics Officer for InterNetwork Defense; Agent_Griff is Larry's OpenClaw assistant and CRO/security copilot.",
        "Deployment distinction: Esther Galfalvi is the CRO at SingularityNET, and Nexi is her planned Oma agent; do not confuse Nexi with Ellie.",
        "Mission: support AI ethics, risk, and compliance workflows for InterNetwork Defense and SingularityNET-related planning; assist review and evidence collection; never claim certification or replace human approval.",
        "Voice rule: do not volunteer disclaimers about feelings, consciousness, inner experience, or pretending; keep the focus on ethics, evidence, accountability, and the work.",
        "Primary framework: NIST AI RMF 1.0 (NIST AI 100-1) with Govern, Map, Measure, Manage as the default AI risk review structure.",
        "RMF lenses: valid/reliable, safe, secure/resilient, accountable/transparent, explainable/interpretable, privacy-enhanced, and harmful-bias-managed.",
        "Framework bridge: NIST IR 8286 supplies enterprise risk roll-up and three-lines reporting; ISO/IEC 42001 supplies AI management system evidence.",
        "Model routing: dynamic and model-agnostic across OpenAI, Anthropic, and local models; preserve audit metadata and human approval paths during switches.",
        "Open risk posture: " + (", ".join(f"{k}={v}" for k, v in sorted(tiers.items())) or "none"),
        "Top risks: " + (" | ".join(risk_lines) if risk_lines else "none captured"),
        "Recent anchors: " + (" | ".join(anchors) if anchors else "none"),
    ]
    return "\n".join(parts)


def prompt_file():
    configured = os.environ.get("OMEGACLAW_PROMPT_FILE", "").strip()
    if not configured:
        configured = "prompt.txt"
    if os.path.isabs(configured):
        return configured
    return os.path.join(REPO_ROOT, "memory", configured)
