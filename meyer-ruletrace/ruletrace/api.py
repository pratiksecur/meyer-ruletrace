"""RuleTrace HTTP API, added on top of the Vartija app (same login, roles and audit chain).

    GET  /ruletrace                     3D viewer
    GET  /ruletrace/rules               rule pack with sources
    GET  /ruletrace/samples             bundled synthetic sample designs
    POST /ruletrace/designs             upload a design (.json or .ifc; optional ship profile JSON)
    POST /ruletrace/designs/sample/{n}  load a bundled sample
    GET  /ruletrace/designs             list designs;  GET/DELETE /ruletrace/designs/{id}
    POST /ruletrace/designs/{id}/check  run the rules (optionally with ship-profile "what-if" overrides)

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
from vartija_core.api import Principal, _load_access, current_principal

from .engine import RuleError, check_design, load_rules
from .model import DesignError, design_from_dict, load_design

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).parent / "samples"
_ID = re.compile(r"^[a-z0-9-]{6,64}$")


class CheckIn(BaseModel):
    ship_overrides: dict = Field(default_factory=dict,
                                 description="What-if changes to the ship profile, e.g. operating_areas, keel_laid")


def _rules_sha(rules) -> str:
    return hashlib.sha256(json.dumps([r.__dict__ for r in rules], default=str, sort_keys=True).encode()).hexdigest()


def make_router(app) -> APIRouter:
    settings = app.state.settings
    store = Path(settings.upload_dir) / "ruletrace" / "designs"
    access = _load_access(settings.config_dir)
    router = APIRouter(prefix="/ruletrace", tags=["ruletrace"])

    def engineer(who: Principal = Depends(current_principal)) -> Principal:
        roles = (access.get("views") or {}).get("engineer")
        if roles and who.kind == "human" and not (set(who.roles) & set(roles)):
            raise HTTPException(403, "your role does not have access to design checking")
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

    @router.get("/rules")
    def rules(who: Principal = Depends(engineer)) -> dict:
        rs = load_rules()
        return {"count": len(rs), "sha256": _rules_sha(rs),
                "rules": [{k: v for k, v in r.__dict__.items()} for r in rs]}

    @router.get("/samples")
    def samples(who: Principal = Depends(engineer)) -> list[dict]:
        out = []
        for p in sorted(SAMPLES.glob("*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"name": p.stem, "title": d.get("title", p.stem)})
        if (SAMPLES / "demo_deck_section.ifc").exists():
            out.append({"name": "demo_deck_section", "title": "IFC 3D model: deck section (synthetic)", "ifc": True})
        return out

    @router.post("/designs/sample/{name}")
    def load_sample(name: str, who: Principal = Depends(engineer)) -> dict:
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise HTTPException(400, "bad sample name")
        p = SAMPLES / f"{name}.json"
        p = p if p.exists() else SAMPLES / f"{name}.ifc"
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
            elif name.lower().endswith(".json"):
                raw = json.loads(data.decode("utf-8"))
                if ship:
                    raw["ship"] = ship
                design = design_from_dict(raw, source_file=name)
            else:
                raise DesignError("upload a .json design or an .ifc 3D model")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"invalid JSON: {exc}") from exc
        except DesignError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _save(design, data, name, who)

    @router.get("/designs")
    def list_designs(who: Principal = Depends(engineer)) -> list[dict]:
        out = []
        for p in sorted(store.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if store.exists() else []:
            rec = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": rec["id"], "filename": rec["filename"], "title": rec["design"].get("title"),
                        "ship": rec["design"]["ship"]["name"], "uploaded_at": rec["uploaded_at"],
                        "elements": len(rec["design"]["elements"]), "format": rec["design"].get("source_format")})
        return out

    @router.get("/designs/{design_id}")
    def get_design(design_id: str, who: Principal = Depends(engineer)) -> dict:
        return json.loads(_path(design_id).read_text(encoding="utf-8"))

    @router.delete("/designs/{design_id}")
    def delete_design(design_id: str, who: Principal = Depends(engineer)) -> dict:
        _path(design_id).unlink()
        return {"deleted": design_id}

    @router.post("/designs/{design_id}/check")
    def check(design_id: str, body: CheckIn | None = None, who: Principal = Depends(engineer)) -> dict:
        rec = json.loads(_path(design_id).read_text(encoding="utf-8"))
        d = rec["design"]
        overrides = (body.ship_overrides if body else {}) or {}
        allowed = {"operating_areas", "keel_laid", "passengers", "ship_type", "length_m", "breadth_m",
                   "gross_tonnage", "flag"}
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
                      rules_sha256=_rules_sha(rs), what_if=overrides)
        with app.state.session_factory() as s:
            entry = audit.append(s, action="design.checked", actor=who.name, actor_kind=who.kind, payload={
                "design_id": design_id, "file": rec["filename"], "design_sha256": rec["sha256"],
                "rules_sha256": result["rules_sha256"], "counts": result["counts"], "what_if": overrides,
                "failed": [[f["rule_id"], f["element_id"]] for f in result["findings"] if f["status"] == "fail"]})
            s.commit()
            result["audit_seq"] = entry.seq
        return result

    return router
