"""RuleTrace HTTP API, added on top of the Vartija app (same login, roles and audit chain).

    GET  /ruletrace                     3D viewer
    GET  /ruletrace/rules               rule pack with sources
    GET  /ruletrace/samples             bundled synthetic sample designs
    POST /ruletrace/designs             upload a design (.json or .ifc; optional ship profile JSON)
    POST /ruletrace/designs/sample/{n}  load a bundled sample
    GET  /ruletrace/designs             list designs;  GET/DELETE /ruletrace/designs/{id}
    POST /ruletrace/designs/{id}/check  run the rules (optionally with ship-profile "what-if" overrides)
    GET  /ruletrace/dashboard           dashboard page: engineer view and executive view, by role
    GET  /ruletrace/summary             dashboard data, built from the recorded design checks
    GET  /ruletrace/posture             organisation-wide compliance posture (CISO / compliance lead, engineers)
    GET  /ruletrace/admin               administration: roles, activity, system health (administrators only)
    GET  /ruletrace/audit               every design check with its hash-chain fields (engineer view)

Designs are stored as normalised JSON under VARTIJA_UPLOAD_DIR/ruletrace (git-ignored).
Every check is written to the tamper-evident audit chain: who checked which design (by
SHA-256), which rule pack, and the result counts.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from vartija_core import audit
import yaml
from vartija_core.api import Principal, current_principal

from .admin import build_admin
from .dashboard import activity_entries, build_summary, checked_entries
from .engine import RuleError, check_design, load_rules
from .model import DesignError, design_from_dict, load_design
from .posture import approvals_of, build_posture

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).parent / "samples"
_ID = re.compile(r"^[a-z0-9-]{6,64}$")


class CheckIn(BaseModel):
    ship_overrides: dict = Field(default_factory=dict,
                                 description="What-if changes to the ship profile, e.g. operating_areas, keel_laid")


ACCESS_FILE = Path(__file__).parent / "access.yaml"
VIEWS = ("engineer", "compliance", "executive")   # first match wins for a person with several roles


def load_access(config_dir: Path) -> dict:
    """Role -> view mapping. Vartija's config/access.yaml first (read here so RuleTrace uses no private Vartija
    API), then RuleTrace's own ruletrace/access.yaml, whose views replace the ones of the same name."""
    p = Path(config_dir) / "access.yaml"
    access = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    own = yaml.safe_load(ACCESS_FILE.read_text(encoding="utf-8")) or {}
    access["views"] = {**(access.get("views") or {}), **(own.get("views") or {})}
    access["administrators"] = own.get("administrators") or []
    return access


def _rules_sha(rules) -> str:
    return hashlib.sha256(json.dumps([r.__dict__ for r in rules], default=str, sort_keys=True).encode()).hexdigest()


def make_router(app) -> APIRouter:
    settings = app.state.settings
    store = Path(settings.upload_dir) / "ruletrace" / "designs"
    access = load_access(settings.config_dir)
    router = APIRouter(prefix="/ruletrace", tags=["ruletrace"])

    def view_of(who: Principal) -> str | None:
        """engineer | compliance | executive, or None when the role has no access to RuleTrace."""
        views = access.get("views") or {}
        if who.kind != "human" or not any(views.get(v) for v in VIEWS):
            return "engineer"                      # services, and a deployment with no role map, are not restricted
        for v in VIEWS:
            if set(who.roles) & set(views.get(v) or []):
                return v
        return None

    def engineer(who: Principal = Depends(current_principal)) -> Principal:
        if view_of(who) != "engineer":
            raise HTTPException(403, "your role does not have access to design checking")
        return who

    def is_admin(who: Principal) -> bool:
        return who.kind == "human" and bool(set(who.roles) & set(access.get("administrators") or []))

    def administrator(who: Principal = Depends(current_principal)) -> Principal:
        if not is_admin(who) or view_of(who) != "engineer":
            raise HTTPException(403, "administrators only")
        return who

    def oversight(who: Principal = Depends(current_principal)) -> Principal:
        """Engineers and the compliance lead (CISO) may read the organisation posture and the audit record."""
        if view_of(who) not in ("engineer", "compliance"):
            raise HTTPException(403, "your role does not have access to the organisation posture and audit record")
        return who

    def _path(design_id: str) -> Path:
        if not _ID.match(design_id):
            raise HTTPException(400, "bad design id")
        p = store / f"{design_id}.json"
        if not p.exists():
            raise HTTPException(404, "design not found")
        return p

    def _save(design, original: bytes, filename: str, who: Principal) -> dict:
        store.mkdir(parents=True, exist_ok=True)
        did = uuid.uuid4().hex[:12]
        rec = {"id": did, "filename": filename, "sha256": hashlib.sha256(original).hexdigest(),
               "uploaded_by": who.name, "uploaded_at": datetime.now(timezone.utc).isoformat(),
               "design": design.to_dict()}
        (store / f"{did}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return {k: rec[k] for k in ("id", "filename", "sha256", "uploaded_by", "uploaded_at")} | {
            "title": design.title, "ship": rec["design"]["ship"]["name"], "elements": len(design.elements),
            "format": design.source_format}

    @router.get("", response_class=HTMLResponse, include_in_schema=False)
    def viewer() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    def any_view(who: Principal = Depends(current_principal)) -> Principal:
        if view_of(who) is None:
            raise HTTPException(403, "your role does not have access to the RuleTrace dashboard")
        return who

    @router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_page() -> str:
        return (STATIC / "dashboard.html").read_text(encoding="utf-8")

    @router.get("/summary")
    def summary(who: Principal = Depends(any_view)) -> dict:
        """Dashboard data, built from the design checks recorded in the audit chain."""
        view = view_of(who)
        with app.state.session_factory() as s:
            out = build_summary(s, store, load_rules())
        out["view"] = view
        out["admin"] = is_admin(who) and view == "engineer"
        out["me"] = {"name": who.name, "roles": list(who.roles)}
        if view in ("engineer", "compliance"):   # executives only get the headline count in totals
            out["approvals"] = approvals_of(out["designs"])
        if view != "engineer":  # compliance and executive views get the plain summary, not element-level findings or hashes
            for d in out["designs"]:
                d.pop("fails", None)
            out["activity"] = [{k: a[k] for k in ("ts", "actor", "file", "counts")} for a in out["activity"]]
        return out

    @router.get("/posture")
    def posture(who: Principal = Depends(oversight)) -> dict:
        """Organisation-wide compliance posture for the CISO / compliance lead, from the recorded checks only."""
        rs = load_rules()
        with app.state.session_factory() as s:
            out = build_posture(build_summary(s, store, rs), rs, activity_entries(s))
        out["me"] = {"name": who.name, "roles": list(who.roles)}
        return out

    @router.get("/admin")
    def admin_overview(who: Principal = Depends(administrator)) -> dict:
        """Roles and what they may do, who has been active, and system health. Administrators only."""
        from . import llm_api  # imported here: llm_api imports this module's dependencies
        rs = load_rules()
        try:
            client = llm_api.get_client()
            llm = client.status() if hasattr(client, "status") else {"model": client.name, "reachable": True, "model_pulled": True}
        except Exception as exc:  # the AI being down must never break the admin page
            llm = {"model": None, "reachable": False, "model_pulled": False, "message": str(exc)}
        with app.state.session_factory() as s:
            entries, chain = activity_entries(s), audit.verify_chain(s).as_dict()
        stored = len(list(store.glob("*.json"))) if store.exists() else 0
        out = build_admin(entries, access.get("views") or {}, access.get("administrators") or [], rs, stored, chain, llm)
        out["me"] = {"name": who.name, "roles": list(who.roles)}
        return out

    @router.get("/audit")
    def audit_record(who: Principal = Depends(oversight)) -> dict:
        """Every design check with its hash-chain fields, for an auditor or class surveyor to keep."""
        with app.state.session_factory() as s:
            entries = checked_entries(s)
            return {"chain": audit.verify_chain(s).as_dict(), "entries": [
                {"seq": e.seq, "ts": e.ts, "actor": e.actor, "actor_kind": e.actor_kind, "action": e.action,
                 "payload": e.payload, "prev_hash": e.prev_hash, "entry_hash": e.entry_hash} for e in entries]}

    @router.get("/rules")
    def rules(who: Principal = Depends(oversight)) -> dict:
        rs = load_rules()
        return {"count": len(rs), "sha256": _rules_sha(rs),
                "rules": [{k: v for k, v in r.__dict__.items()} for r in rs]}

    @router.get("/samples")
    def samples(who: Principal = Depends(oversight)) -> list[dict]:
        out = []
        for p in sorted(SAMPLES.glob("*.json")):
            if p.stem.endswith(".expected"):  # answer key for scripts/evaluate.py, not a design
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"name": p.stem, "title": d.get("title", p.stem)})
        if (SAMPLES / "demo_deck_section.ifc").exists():
            out.append({"name": "demo_deck_section", "title": "IFC 3D model: deck section (synthetic)", "ifc": True})
        if (SAMPLES / "demo_ga_drawing.pdf").exists():
            out.append({"name": "demo_ga_drawing", "title": "GA drawing (PDF): research vessel with planted issues (synthetic)"})
        return out

    @router.post("/designs/sample/{name}")
    def load_sample(name: str, who: Principal = Depends(engineer)) -> dict:
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise HTTPException(400, "bad sample name")
        p = next((SAMPLES / f"{name}{ext}" for ext in (".json", ".ifc", ".pdf") if (SAMPLES / f"{name}{ext}").exists()),
                 SAMPLES / f"{name}.json")
        if not p.exists():
            raise HTTPException(404, "no such sample")
        return _save(load_design(p), p.read_bytes(), p.name, who)

    @router.post("/designs")
    async def upload(file: UploadFile = File(...), ship_json: str = Form(default=""),
                     who: Principal = Depends(engineer)) -> dict:
        if who.kind != "human":
            raise HTTPException(403, "only a person can add designs")
        data = await file.read()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"file is larger than {settings.max_upload_mb} MB")
        name = Path(file.filename or "design").name
        try:
            ship = json.loads(ship_json) if ship_json.strip() else None
            if name.lower().endswith(".ifc"):
                from .ifc_import import design_from_ifc
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td) / "model.ifc"
                    tmp.write_bytes(data)
                    design = design_from_ifc(tmp, ship=ship)
                design.source_file = name
            elif name.lower().endswith(".pdf"):
                from .ga_import import design_from_ga_pdf
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td) / "drawing.pdf"
                    tmp.write_bytes(data)
                    design = design_from_ga_pdf(tmp, ship=ship)
                design.source_file = name
                design.interpretation["source"] = name
            elif name.lower().endswith(".json"):
                raw = json.loads(data.decode("utf-8"))
                if ship:
                    raw["ship"] = ship
                design = design_from_dict(raw, source_file=name)
            else:
                raise DesignError("upload an .ifc 3D model, a .pdf general-arrangement drawing or a .json design")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"invalid JSON: {exc}") from exc
        except DesignError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _save(design, data, name, who)

    @router.get("/designs")
    def list_designs(who: Principal = Depends(oversight)) -> list[dict]:
        out = []
        for p in sorted(store.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if store.exists() else []:
            rec = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": rec["id"], "filename": rec["filename"], "title": rec["design"].get("title"),
                        "ship": rec["design"]["ship"]["name"], "uploaded_at": rec["uploaded_at"],
                        "elements": len(rec["design"]["elements"]), "format": rec["design"].get("source_format")})
        return out

    @router.get("/designs/{design_id}")
    def get_design(design_id: str, who: Principal = Depends(oversight)) -> dict:
        return json.loads(_path(design_id).read_text(encoding="utf-8"))

    @router.delete("/designs/{design_id}")
    def delete_design(design_id: str, who: Principal = Depends(engineer)) -> dict:
        _path(design_id).unlink()
        return {"deleted": design_id}

    @router.post("/designs/{design_id}/check")
    def check(design_id: str, body: CheckIn | None = None, who: Principal = Depends(oversight)) -> dict:
        rec = json.loads(_path(design_id).read_text(encoding="utf-8"))
        d = rec["design"]
        overrides = (body.ship_overrides if body else {}) or {}
        allowed = {"operating_areas", "keel_laid", "passengers", "ship_type", "length_m", "breadth_m",
                   "gross_tonnage", "flag", "enclosed_volume_m3"}
        bad = set(overrides) - allowed
        if bad:
            raise HTTPException(400, f"cannot override {sorted(bad)}")
        d["ship"] = {**d["ship"], **overrides}
        try:
            design = design_from_dict(d, source_file=rec["filename"])
            rs = load_rules()
            result = check_design(design, rs)
        except (DesignError, RuleError) as exc:
            raise HTTPException(400, str(exc)) from exc
        result.update(design_id=design_id, filename=rec["filename"], design_sha256=rec["sha256"],
                      interpretation=design.interpretation, source_format=design.source_format,
                      rules_sha256=_rules_sha(rs), what_if=overrides)
        with app.state.session_factory() as s:
            entry = audit.append(s, action="design.checked", actor=who.name, actor_kind=who.kind, payload={
                "design_id": design_id, "file": rec["filename"], "design_sha256": rec["sha256"],
                "rules_sha256": result["rules_sha256"], "counts": result["counts"], "what_if": overrides,
                "failed": [[f["rule_id"], f["element_id"]] for f in result["findings"] if f["status"] == "fail"]})
            s.commit()
            result["audit_seq"] = entry.seq
        return result

    from .llm_api import add_llm_routes  # local-LLM helpers (explain, draft rule, ask)
    add_llm_routes(app, router, engineer, _path)

    from .rulesets_api import add_ruleset_routes  # proposed rule sets: read a regulation document, analyse it
    add_ruleset_routes(app, router, engineer, store, reader=oversight)

    from .revisions import add_revision_routes  # compare two revisions of a design
    add_revision_routes(app, router, engineer, _path, reader=oversight)

    from .reviews import add_review_routes  # human review of findings and four-eyes sign-off
    add_review_routes(app, router, engineer, _path, reader=oversight)

    return router
