"""RuleTrace dashboard data: built only from RuleTrace's own design checks.

Every design check writes a `design.checked` entry to the audit chain (see api.py). The
dashboard reads those entries back, so what the engineer and the executive see is exactly
what was checked, by whom, against which version of the design and of the rule pack.

A design that was uploaded several times (same SHA-256) is counted once, using its latest check.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlalchemy import select
from vartija_core.audit import AuditEntry, verify_chain

VALUE_FILE = Path(__file__).parent / "value.yaml"


def load_value() -> dict:
    return yaml.safe_load(VALUE_FILE.read_text(encoding="utf-8")) or {}


def checked_entries(session) -> list[AuditEntry]:
    q = select(AuditEntry).where(AuditEntry.action == "design.checked").order_by(AuditEntry.seq)
    return list(session.scalars(q))


AI_AND_CHECK_ACTIONS = ("design.checked", "design.explained", "design.fixes_suggested", "design.whatif",
                        "rule.drafted", "assistant.asked", "ruleset.analysed")


def activity_entries(session) -> list[dict]:
    """Design checks and AI actions from the audit chain as plain dicts, oldest first (for the posture view)."""
    q = select(AuditEntry).where(AuditEntry.action.in_(AI_AND_CHECK_ACTIONS)).order_by(AuditEntry.seq)
    return [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action, "payload": e.payload or {}}
            for e in session.scalars(q)]


def _design_meta(store: Path, design_id: str) -> dict | None:
    p = store / f"{design_id}.json"
    if not p.exists():
        return None
    rec = json.loads(p.read_text(encoding="utf-8"))
    d = rec["design"]
    return {"title": d.get("title") or rec["filename"], "ship": d["ship"]["name"],
            "ship_type": d["ship"].get("ship_type"), "format": d.get("source_format"),
            "elements": len(d.get("elements", []))}


def _review(store: Path, design_id: str, counts: dict) -> dict:
    """Human review state of a design's check (see reviews.py)."""
    p = store.parent / "reviews" / f"{design_id}.json"
    rec = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    so = rec.get("signoff") or {}
    open_n = sum(counts.get(k, 0) for k in ("fail", "warn", "unknown"))
    reviewed = min(len(rec.get("findings") or {}), open_n)
    stage = so.get("stage") or ("not started" if not reviewed else "in review")
    label = {"approved": f"Approved by {so.get('approved_by')}", "prepared": f"Prepared by {so.get('prepared_by')}, awaiting approval",
             "rejected": f"Sent back by {so.get('rejected_by')}"}.get(stage, f"{reviewed} of {open_n} findings reviewed")
    return {"stage": stage, "label": label, "reviewed": reviewed, "open": open_n, "prepared_by": so.get("prepared_by"),
            "approved_by": so.get("approved_by"), "rejected_by": so.get("rejected_by")}


def readiness(counts: dict) -> tuple[str, str]:
    if counts.get("fail"):
        return "fix", "Needs design changes"
    if counts.get("unknown"):
        return "info", "Needs more design information"
    if counts.get("warn"):
        return "review", "Ready, with points to review"
    return "ready", "No issues found by the checked rules"


def build_summary(session, store: Path, rules: list) -> dict:
    by_rule = {r.id: r for r in rules}
    entries = checked_entries(session)

    latest: dict[str, AuditEntry] = {}          # design SHA-256 -> latest check
    for e in entries:
        latest[e.payload.get("design_sha256") or e.payload.get("design_id")] = e

    designs, by_regime = [], {}
    sev_open = {"major": 0, "minor": 0}
    findings_checked = 0
    removed = 0
    for e in sorted(latest.values(), key=lambda e: e.seq, reverse=True):
        p = e.payload
        if _design_meta(store, p.get("design_id", "")) is None:   # removed from RuleTrace: kept in the history only
            removed += 1
            continue
        counts = p.get("counts") or {}
        findings_checked += sum(counts.get(k, 0) for k in ("pass", "fail", "warn", "unknown"))
        fails = []
        for rule_id, element_id in p.get("failed") or []:
            r = by_rule.get(rule_id)
            regime = r.regime if r else "Other"
            sev = r.severity if r and r.severity in sev_open else "major"
            sev_open[sev] += 1
            g = by_regime.setdefault(regime, {"regime": regime, "fail": 0, "rules": {}})
            g["fail"] += 1
            g["rules"][rule_id] = r.title if r else rule_id
            fails.append({"rule_id": rule_id, "element_id": element_id, "regime": regime,
                          "title": r.title if r else rule_id, "ref": r.ref if r else "", "severity": sev})
        meta = _design_meta(store, p.get("design_id", ""))
        code, label = readiness(counts)
        rv = _review(store, p.get("design_id", ""), counts)
        designs.append({"design_id": p.get("design_id"), "available": meta is not None,
                        **(meta or {"title": p.get("file"), "ship": "", "format": "", "elements": 0}),
                        "file": p.get("file"), "design_sha256": p.get("design_sha256"),
                        "rules_sha256": p.get("rules_sha256"), "counts": counts, "what_if": p.get("what_if") or {},
                        "checked_by": e.actor, "checked_at": e.ts, "audit_seq": e.seq,
                        "readiness": code, "readiness_label": label, "fails": fails, "review": rv})

    regimes = sorted(({**g, "rules": [{"id": k, "title": v} for k, v in g["rules"].items()]}
                      for g in by_regime.values()), key=lambda g: -g["fail"])
    activity = [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "file": e.payload.get("file"),
                 "design_id": e.payload.get("design_id"), "counts": e.payload.get("counts") or {},
                 "what_if": e.payload.get("what_if") or {}, "entry_hash": e.entry_hash}
                for e in reversed(entries[-25:])]
    return {"designs": designs, "regimes": regimes, "activity": activity,
            "totals": {"designs": len(designs), "removed": removed, "checks_run": len(entries), "findings_checked": findings_checked,
                       "approved": sum(1 for d in designs if d["review"]["stage"] == "approved"),
                       "awaiting_approval": sum(1 for d in designs if d["review"]["stage"] == "prepared"),
                       "open_fail": sum(d["counts"].get("fail", 0) for d in designs),
                       "open_unknown": sum(d["counts"].get("unknown", 0) for d in designs),
                       "open_warn": sum(d["counts"].get("warn", 0) for d in designs),
                       "fail_by_severity": sev_open},
            "chain": verify_chain(session).as_dict(), "value": load_value(),
            "rule_pack": {"count": len(rules), "regimes": sorted({r.regime for r in rules})}}
