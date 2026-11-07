"""HTTP endpoints for proposed rule sets (added to the RuleTrace router; same login, roles and audit chain).

    GET    /ruletrace/rulesets/page              the rule-set analysis page
    GET    /ruletrace/rulesets                   rule sets uploaded so far
    POST   /ruletrace/rulesets                   upload a regulation document (.pdf .docx .txt .md .html .yaml)
    POST   /ruletrace/rulesets/sample/{name}     load a bundled sample rule document
    GET    /ruletrace/rulesets/{id}              the rule set and a fresh analysis against the current designs
    GET    /ruletrace/rulesets/{id}/yaml         the draft rules as RuleTrace YAML (verified: false)
    DELETE /ruletrace/rulesets/{id}

Nothing here changes the live rule pack: draft rules only become real after an engineer checks them,
copies them into ruletrace/rules/ and adds a test case.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from vartija_core import audit
from vartija_core.api import Principal

from . import rulesets as rs
from .model import DesignError, design_from_dict, load_design

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).parent / "samples"
RULE_SAMPLES = SAMPLES / "rules"
_ID = re.compile(r"^[a-z0-9]{6,32}$")


def sample_designs() -> list[tuple[str, object]]:
    out = []
    for p in sorted(SAMPLES.glob("*.json")):
        if not p.name.endswith(".expected.json"):
            out.append((p.name, load_design(p)))
    for p in sorted(SAMPLES.glob("*.ifc")) + sorted(SAMPLES.glob("*.pdf")):
        try:
            d = load_design(p)
        except (DesignError, ImportError):
            continue  # optional extras not installed
        side = p.with_name(p.stem + ".expected.json")
        if side.exists():
            for k, v in json.loads(side.read_text(encoding="utf-8")).get("ship_overrides", {}).items():
                setattr(d.ship, k, date.fromisoformat(v) if k == "keel_laid" else v)
        out.append((p.name, d))
    return out


def add_ruleset_routes(app, router, engineer, design_store: Path) -> None:
    store = Path(app.state.settings.upload_dir) / "ruletrace" / "rulesets"

    def _designs() -> tuple[list, str]:
        seen, out = set(), []
        for p in sorted(design_store.glob("*.json"), key=lambda p: p.stat().st_mtime) if design_store.exists() else []:
            rec = json.loads(p.read_text(encoding="utf-8"))
            if rec["sha256"] in seen:
                continue
            seen.add(rec["sha256"])
            try:
                out.append((rec["filename"], design_from_dict(rec["design"], source_file=rec["filename"])))
            except DesignError:
                continue
        return (out, "the designs loaded in RuleTrace") if out else (sample_designs(), "RuleTrace's sample designs")

    def _path(rid: str) -> Path:
        if not _ID.match(rid):
            raise HTTPException(400, "bad rule set id")
        p = store / f"{rid}.json"
        if not p.exists():
            raise HTTPException(404, "rule set not found")
        return p

    def _summary(rec: dict) -> dict:
        return {k: rec[k] for k in ("id", "name", "filename", "uploaded_by", "uploaded_at", "paragraphs")} | {
            "rules": len(rec["rules"]), "manual": len(rec["manual"])}

    def _analyse(rec: dict, who: Principal) -> dict:
        designs, used = _designs()
        a = rs.analyze(rec["rules"], designs)
        with app.state.session_factory() as s:
            audit.append(s, action="ruleset.analysed", actor=who.name, actor_kind=who.kind, payload={
                "ruleset_id": rec["id"], "file": rec["filename"], "sha256": rec["sha256"], "rules": len(rec["rules"]),
                "counts": a["counts"], "designs": a["designs_used"]})
            s.commit()
        return {**_summary(rec), "notes": rec.get("notes", []), "manual_items": rec["manual"],
                "designs_source": used, **a}

    def _ingest(data: bytes, filename: str, name: str, use_ai: bool, who: Principal) -> dict:
        suffix = Path(filename).suffix.lower()
        if suffix not in rs.READABLE:
            raise HTTPException(400, f"upload a regulation document: {', '.join(sorted(rs.READABLE))}")
        name = (name or Path(filename).stem.replace("_", " ")).strip()[:80]
        notes = []
        try:
            if suffix in {".yaml", ".yml"}:
                rules, manual, paras = rs.rules_from_yaml(data.decode("utf-8"), name), [], 0
            else:
                with tempfile.TemporaryDirectory() as td:
                    p = Path(td) / f"doc{suffix}"
                    p.write_bytes(data)
                    text = rs.read_text(p)
                if len(text.strip()) < 20:
                    raise HTTPException(400, "no readable text in this document (a scan? export it with text)")
                rules, manual, paras = rs.extract_rules(text, name, source=filename)
        except (ValueError, KeyError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise HTTPException(400, f"could not read the document: {exc}") from exc
        if use_ai and manual:
            more, note = rs.ai_drafts(manual, name, {r["id"] for r in rules})
            rules += more
            notes.append(note)
        store.mkdir(parents=True, exist_ok=True)
        rid = uuid.uuid4().hex[:12]
        rec = {"id": rid, "name": name, "filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
               "uploaded_by": who.name, "uploaded_at": datetime.now(timezone.utc).isoformat(), "paragraphs": paras,
               "rules": rules, "manual": manual, "notes": notes}
        (store / f"{rid}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        return _analyse(rec, who)

    @router.get("/rulesets/page", response_class=HTMLResponse, include_in_schema=False)
    def ruleset_page() -> str:
        return (STATIC / "rulesets.html").read_text(encoding="utf-8")

    @router.get("/rulesets")
    def list_rulesets(who: Principal = Depends(engineer)) -> dict:
        recs = [json.loads(p.read_text(encoding="utf-8")) for p in
                sorted(store.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)] if store.exists() else []
        return {"rulesets": [_summary(r) for r in recs],
                "samples": [{"name": p.stem, "title": p.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")}
                            for p in sorted(RULE_SAMPLES.glob("*.md"))]}

    @router.post("/rulesets")
    async def upload_ruleset(file: UploadFile = File(...), name: str = Form(default=""), use_ai: bool = Form(default=False),
                             who: Principal = Depends(engineer)) -> dict:
        if who.kind != "human":
            raise HTTPException(403, "only a person can add rule sets")
        data = await file.read()
        if len(data) > app.state.settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "file too large")
        return _ingest(data, Path(file.filename or "rules.txt").name, name, use_ai, who)

    @router.post("/rulesets/sample/{name}")
    def sample_ruleset(name: str, who: Principal = Depends(engineer)) -> dict:
        if not re.fullmatch(r"[a-z0-9_]+", name) or not (RULE_SAMPLES / f"{name}.md").exists():
            raise HTTPException(404, "no such sample")
        p = RULE_SAMPLES / f"{name}.md"
        return _ingest(p.read_bytes(), p.name, p.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").split(":")[0], False, who)

    @router.get("/rulesets/{rid}")
    def get_ruleset(rid: str, who: Principal = Depends(engineer)) -> dict:
        return _analyse(json.loads(_path(rid).read_text(encoding="utf-8")), who)

    @router.get("/rulesets/{rid}/yaml", response_class=PlainTextResponse)
    def ruleset_yaml(rid: str, who: Principal = Depends(engineer)) -> str:
        rec = json.loads(_path(rid).read_text(encoding="utf-8"))
        clean = [{k: v for k, v in r.items() if k != "extracted_by"} for r in rec["rules"]]
        return (f"# Draft rules from {rec['filename']} ({rec['name']}). DRAFT: verified: false on every rule.\n"
                f"# Check each against the official text, fix what the analysis flagged, add a labelled test case,\n"
                f"# then copy into ruletrace/rules/.\n") + yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=110)

    @router.delete("/rulesets/{rid}")
    def delete_ruleset(rid: str, who: Principal = Depends(engineer)) -> dict:
        _path(rid).unlink()
        return {"deleted": rid}
