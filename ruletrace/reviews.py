"""Human review of every check: nothing is accepted just because the software said so.

For each design check:
1. An engineer reviews every finding that isn't a pass: **confirm** it ("yes, this must be fixed") or
   **dismiss** it with a written reason (false finding, doesn't apply here, accepted deviation agreed with
   class). A dismissal without a reason is refused.
2. When every open finding has been reviewed, the engineer **signs off as prepared**.
3. A second person with an approver role **approves** it (four-eyes: the approver can't be the person who
   prepared it). Approval can also be refused, with a reason, which sends it back.

Every decision is written to the tamper-evident audit chain with the design's fingerprint, so the record
shows who decided what, on which version of the design and of the rules.

    GET  /ruletrace/designs/{id}/reviews
    POST /ruletrace/designs/{id}/findings/review   {rule_id, element_id, decision: confirm|dismiss|clear, reason}
    POST /ruletrace/designs/{id}/signoff           {stage: prepared|approved|rejected|reopen, note}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from vartija_core import audit
from vartija_core.api import Principal

from .engine import check_design, load_rules
from .model import DesignError, design_from_dict

APPROVER_ROLES = {"ciso", "grc_manager", "admin", "compliance_lead", "class_surveyor"}
OPEN = {"fail", "warn", "unknown"}
DISMISS_REASONS = {"false_finding": "False finding (the design is actually compliant)",
                   "not_applicable": "Doesn't apply to this part or ship",
                   "accepted_deviation": "Accepted deviation (agreed with class / flag)",
                   "information_follows": "Information will follow in a later revision"}


class ReviewIn(BaseModel):
    rule_id: str = Field(max_length=120)
    element_id: str | None = Field(default=None, max_length=120)
    decision: str = Field(pattern="^(confirm|dismiss|clear)$")
    reason_code: str = Field(default="", max_length=40)
    reason: str = Field(default="", max_length=500)


class SignoffIn(BaseModel):
    stage: str = Field(pattern="^(prepared|approved|rejected|reopen)$")
    note: str = Field(default="", max_length=500)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key(rule_id: str, element_id: str | None) -> str:
    return f"{rule_id}|{element_id or ''}"


def is_approver(who: Principal) -> bool:
    return bool(set(who.roles) & APPROVER_ROLES)


def status_of(rec: dict, findings: list[dict]) -> dict:
    """Review progress for a check result: how many open findings are reviewed, and the sign-off stage."""
    open_keys = [key(f["rule_id"], f.get("element_id")) for f in findings if f["status"] in OPEN]
    reviewed = [k for k in open_keys if k in rec.get("findings", {})]
    so = rec.get("signoff") or {}
    stage = so.get("stage") or "in review"
    label = {"approved": f"Approved by {so.get('approved_by')}",
             "prepared": f"Prepared by {so.get('prepared_by')}, waiting for approval",
             "rejected": f"Sent back by {so.get('rejected_by')}"}.get(stage, f"{len(reviewed)} of {len(open_keys)} findings reviewed")
    return {"open": len(open_keys), "reviewed": len(reviewed), "stage": stage, "label": label,
            "confirmed": sum(1 for k in reviewed if rec["findings"][k]["decision"] == "confirm"),
            "dismissed": sum(1 for k in reviewed if rec["findings"][k]["decision"] == "dismiss")}


def load(store: Path, design_id: str) -> dict:
    p = store / f"{design_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"findings": {}, "signoff": {}, "history": []}


def add_review_routes(app, router, engineer, path_of) -> None:
    store = Path(app.state.settings.upload_dir) / "ruletrace" / "reviews"

    def _save(design_id: str, rec: dict) -> None:
        store.mkdir(parents=True, exist_ok=True)
        (store / f"{design_id}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")

    def _findings(design_id: str, overrides: dict | None = None) -> tuple[dict, list[dict]]:
        drec = json.loads(path_of(design_id).read_text(encoding="utf-8"))
        d = {**drec["design"], "ship": {**drec["design"]["ship"], **(overrides or {})}}
        try:
            return drec, check_design(design_from_dict(d, source_file=drec["filename"]), load_rules())["findings"]
        except DesignError as exc:
            raise HTTPException(400, str(exc)) from exc

    def _audit(who: Principal, action: str, payload: dict) -> int:
        with app.state.session_factory() as s:
            e = audit.append(s, action=action, actor=who.name, actor_kind=who.kind, payload=payload)
            s.commit()
            return e.seq

    @router.get("/designs/{design_id}/reviews")
    def get_reviews(design_id: str, who: Principal = Depends(engineer)) -> dict:
        path_of(design_id)
        rec = load(store, design_id)
        return {**rec, "can_approve": is_approver(who), "reasons": DISMISS_REASONS}

    @router.post("/designs/{design_id}/findings/review")
    def review_finding(design_id: str, body: ReviewIn, who: Principal = Depends(engineer)) -> dict:
        if who.kind != "human":
            raise HTTPException(403, "only a person can review a finding")
        drec = json.loads(path_of(design_id).read_text(encoding="utf-8"))
        rec = load(store, design_id)
        if (rec.get("signoff") or {}).get("stage") in {"prepared", "approved"}:
            raise HTTPException(409, "this check is signed off; reopen it before changing a review")
        k = key(body.rule_id, body.element_id)
        if body.decision == "clear":
            rec["findings"].pop(k, None)
        else:
            if body.decision == "dismiss":
                if body.reason_code not in DISMISS_REASONS:
                    raise HTTPException(400, f"choose a reason: {', '.join(DISMISS_REASONS)}")
                if len(body.reason.strip()) < 10:
                    raise HTTPException(400, "explain the dismissal in a sentence (at least 10 characters)")
            rec["findings"][k] = {"decision": body.decision, "reason_code": body.reason_code, "reason": body.reason.strip(),
                                  "by": who.name, "at": _now()}
        seq = _audit(who, "finding.reviewed", {"design_id": design_id, "design_sha256": drec["sha256"], "rule_id": body.rule_id,
                                               "element_id": body.element_id, "decision": body.decision,
                                               "reason_code": body.reason_code, "reason": body.reason[:200]})
        rec["history"].append({"at": _now(), "by": who.name, "what": f"{body.decision} {body.rule_id} {body.element_id or ''}".strip(),
                               "audit_seq": seq})
        _save(design_id, rec)
        return {**rec, "can_approve": is_approver(who), "reasons": DISMISS_REASONS}

    @router.post("/designs/{design_id}/signoff")
    def signoff(design_id: str, body: SignoffIn, who: Principal = Depends(engineer)) -> dict:
        if who.kind != "human":
            raise HTTPException(403, "only a person can sign off")
        drec, findings = _findings(design_id, (load(store, design_id).get("profile") or {}))
        rec = load(store, design_id)
        so = rec.get("signoff") or {}
        if body.stage == "prepared":
            st = status_of(rec, findings)
            if st["reviewed"] < st["open"]:
                raise HTTPException(409, f"review every open finding first ({st['reviewed']} of {st['open']} done)")
            so = {"stage": "prepared", "prepared_by": who.name, "prepared_at": _now(), "note": body.note}
        elif body.stage in {"approved", "rejected"}:
            if so.get("stage") != "prepared":
                raise HTTPException(409, "an engineer must sign off as prepared first")
            if not is_approver(who):
                raise HTTPException(403, "your role can't approve; ask a compliance lead")
            if who.name == so.get("prepared_by"):
                raise HTTPException(403, "four-eyes rule: you prepared this, so someone else must approve it")
            if body.stage == "rejected" and len(body.note.strip()) < 10:
                raise HTTPException(400, "say why it is sent back (at least 10 characters)")
            so = {**so, "stage": body.stage, f"{body.stage}_by": who.name, f"{body.stage}_at": _now(), f"{body.stage}_note": body.note}
        else:  # reopen
            if so.get("stage") == "approved" and not is_approver(who):
                raise HTTPException(403, "only an approver can reopen an approved check")
            so = {"stage": "in review", "reopened_by": who.name, "reopened_at": _now()}
        rec["signoff"] = so
        seq = _audit(who, f"check.{body.stage}", {"design_id": design_id, "design_sha256": drec["sha256"],
                                                  "note": body.note[:200], **status_of(rec, findings)})
        rec["history"].append({"at": _now(), "by": who.name, "what": f"sign-off: {body.stage}", "audit_seq": seq})
        _save(design_id, rec)
        return {**rec, "can_approve": is_approver(who), "reasons": DISMISS_REASONS, "status": status_of(rec, findings)}

    @router.post("/designs/{design_id}/reviews/profile")
    def set_profile(design_id: str, body: dict, who: Principal = Depends(engineer)) -> dict:
        """The what-if profile (keel date, sea areas) the reviewed check was run with, so sign-off re-checks the same thing."""
        path_of(design_id)
        rec = load(store, design_id)
        rec["profile"] = {k: v for k, v in (body or {}).items() if k in {"keel_laid", "operating_areas"}}
        _save(design_id, rec)
        return {"ok": True}
