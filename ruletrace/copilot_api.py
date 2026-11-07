"""RuleMate Copilot inside RuleTrace: ask the full regulations, read drawings and pictures with Gemini.

Added to the RuleTrace router (same login, roles and audit chain). The Copilot never decides compliance on a
RuleTrace design: findings still come from the rule engine. It adds two things the rule pack cannot do alone:

    GET  /ruletrace/copilot/status          what is installed and ready (index, Gemini key, local model)
    POST /ruletrace/copilot/index           build the clause index of the full regulation texts (once, ~30 s)
    POST /ruletrace/copilot/ask             question -> answer citing clauses of the official texts; every
                                            citation, number and quote checked in code, or "not found"
    GET  /ruletrace/copilot/clause/{uid}    the full text of a cited clause
    POST /ruletrace/copilot/read            a picture (png/jpg) or a drawing page (pdf, also scanned): Gemini reads
                                            it, code confirms every reading on the image; picture values are
                                            checked against the full texts
    POST /ruletrace/copilot/design          rooms read from a drawing -> a RuleTrace design (cabins, hospital,
                                            spaces), checked by the rule engine and shown in 3D

The regulation texts (public domain) are in ruletrace/copilot_rules/: 46 CFR Part 190 and the ADA/ABA technical
requirements (36 CFR 1191 App. D). The index and caches live under VARTIJA_UPLOAD_DIR/copilot (git-ignored).
Needs the optional dependencies: pip install -e ".[copilot]".
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
from pathlib import Path

from fastapi import Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from vartija_core import audit
from vartija_core.api import Principal

RULES = Path(__file__).parent / "copilot_rules"
DEFAULT_DOCS = ["36CFR1191AppD", "46CFR190"]
MAX_PDF_PAGES = 400


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    docs: list[str] | None = Field(default=None, description="Rule documents to search; default all")


class DesignIn(BaseModel):
    readings_id: str = Field(pattern=r"^[0-9a-f]{16}$", description="id returned by /copilot/read for a drawing")
    ship: dict = Field(default_factory=dict, description="Ship profile fields (name, ship_type, length_m, persons_total...)")


def configure(upload_dir: Path) -> Path:
    """Point the RuleMate package at RuleTrace's folders before it is imported."""
    base = Path(upload_dir) / "copilot"
    os.environ.setdefault("RULES_DIR", str(RULES))
    os.environ.setdefault("INDEX_DIR", str(base / "index"))
    os.environ.setdefault("RULEMATE_CACHE_DIR", str(base / "cache"))
    return base


def provider() -> str:
    return os.getenv("COPILOT_LLM_PROVIDER", "gemini,ollama")


def vision_llm():
    from rulemate.llm import make_provider

    llm = make_provider("gemini")
    llm.model = os.getenv("GEMINI_VISION_MODEL", "gemini-3.5-flash")
    llm.timeout = max(llm.timeout, 240)
    return llm


def _png_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _crop_url(img, box, margin=140) -> str:
    from PIL import ImageDraw

    x0, y0, x1, y1 = (int(v) for v in box)
    im = img.copy()
    ImageDraw.Draw(im).rectangle((x0 - 6, y0 - 6, x1 + 6, y1 + 6), outline=(214, 39, 40), width=4)
    im = im.crop((max(0, x0 - margin), max(0, y0 - margin), min(im.width, x1 + margin), min(im.height, y1 + margin)))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return _png_url(buf.getvalue())


def design_from_rooms(rooms: list[dict], page_w: float, page_h: float, ship: dict, filename: str, page: int) -> dict:
    """Confirmed Gemini readings of a GA sheet -> RuleTrace design JSON. Cabins carry berths only when the drawing
    states them (printed count, SINGLE/DOUBLE); positions come from the label position on the sheet, scaled to the
    ship length (approximate, as in RuleTrace's own GA reader)."""
    length = float(ship.get("length_m") or 60.0)
    scale = length / page_w
    elements = []
    seen: set[str] = set()
    for i, r in enumerate(rooms):
        if not r["confirmed"] or r["kind"] not in ("stateroom", "hospital"):
            continue
        key = r.get("room_number") or f"{r['label']}-{i}"
        if key in seen:
            continue
        seen.add(key)
        x0, y0, x1, y1 = r["bbox"]
        cx, cy = (x0 + x1) / 2 * scale, ((y0 + y1) / 2 - page_h / 2) * scale
        name = " ".join(r["label"].split()).title() + (f" {r['room_number']}" if r.get("room_number") else "")
        if r["kind"] == "hospital":
            etype, props = "hospital", {}
        else:
            etype = "cabin"
            props = {"berths": int(r["berths"])} if r.get("berths_confirmed") and r.get("berths") else {}
        props["read_by"] = "Gemini, confirmed in code at the label position"
        elements.append({"id": f"G{page}-{len(elements) + 1:03d}", "type": etype, "name": name,
                         "deck": f"sheet p.{page}", "box": [round(cx - 1.5, 2), round(cy - 1.5, 2), 0, 3, 3, 2.4],
                         "props": props})
    return {"title": f"{filename} p.{page} (read by Gemini)",
            "ship": {"name": ship.get("name") or Path(filename).stem, "ship_type": ship.get("ship_type", "research"),
                     **{k: v for k, v in ship.items() if k not in ("name", "ship_type")}},
            "elements": elements}


def add_copilot_routes(app, router, engineer, path_of, save) -> None:
    base = configure(app.state.settings.upload_dir)
    readings_dir = base / "readings"

    def _audit(who: Principal, action: str, payload: dict) -> None:
        with app.state.session_factory() as s:
            audit.append(s, action=action, actor=who.name, actor_kind=who.kind, payload=payload)
            s.commit()

    def _require():
        try:
            import rulemate  # noqa: F401
            import rank_bm25  # noqa: F401
            import pint  # noqa: F401
        except ImportError as exc:
            raise HTTPException(501, f'Copilot not installed ({exc.name}): pip install -e ".[copilot]"') from exc

    def _index_ready() -> bool:
        from rulemate.config import CLAUSES_FILE

        return CLAUSES_FILE.exists()

    @router.get("/copilot/status")
    def status(who: Principal = Depends(engineer)) -> dict:
        try:
            _require()
        except HTTPException as e:
            return {"installed": False, "message": e.detail}
        out = {"installed": True, "index": _index_ready(), "provider": provider(),
               "gemini": bool(os.getenv("GEMINI_API_KEY")), "documents": []}
        if out["index"]:
            from rulemate.ingest import load_index

            out["documents"] = [{"doc": d.doc, "title": d.title, "clauses": d.n_clauses}
                                for d in load_index().documents]
        return out

    @router.post("/copilot/index")
    def build_index(who: Principal = Depends(engineer)) -> dict:
        _require()
        from rulemate.compliance.service import get_rule_table
        from rulemate.ingest import ingest_dir
        from rulemate.retrieval import get_retriever

        res = ingest_dir()
        r = get_retriever()
        table = get_rule_table(method="regex", force=True)
        out = {"documents": [d.doc for d in res.documents], "clauses": len(res.clauses), "retrieval": r.mode,
               "limits": len(table.limits)}
        _audit(who, "copilot.indexed", out)
        return out

    @router.post("/copilot/ask")
    def ask(body: AskIn, who: Principal = Depends(engineer)) -> dict:
        _require()
        if not _index_ready():
            raise HTTPException(409, "build the Copilot index first (POST /ruletrace/copilot/index)")
        from rulemate.answer import answer_question
        from rulemate.llm import get_llm
        from rulemate.retrieval import get_retriever

        a = answer_question(body.question, retriever=get_retriever(), llm=get_llm(provider()), use_cache=True,
                            docs=body.docs)
        _audit(who, "copilot.asked", {"question": body.question, "status": a.status, "model": a.model,
                                      "cited": [c.uid for c in a.citations]})
        return {"question": a.question, "status": a.status, "badge": a.badge, "text": a.text, "model": a.model,
                "note": a.note, "latency_ms": a.latency_ms, "rejected_attempts": a.rejected_attempts,
                "citations": [{"uid": c.uid, "doc": c.doc, "clause_id": c.clause_id, "title": c.title,
                               "page": c.page} for c in a.citations]}

    @router.get("/copilot/clause/{uid:path}")
    def clause(uid: str, who: Principal = Depends(engineer)) -> dict:
        _require()
        from rulemate.retrieval import get_retriever

        c = get_retriever().by_uid.get(uid)
        if c is None:
            raise HTTPException(404, "no such clause")
        return {"uid": c.uid, "doc": c.doc, "clause_id": c.clause_id, "title": c.title, "text": c.text,
                "path": c.path, "refs": c.refs}

    @router.post("/copilot/read")
    async def read(file: UploadFile = File(...), page: int = Form(default=1), docs: str = Form(default=""),
                   who: Principal = Depends(engineer)) -> dict:
        _require()
        if not os.getenv("GEMINI_API_KEY"):
            raise HTTPException(503, "set GEMINI_API_KEY in .env to read drawings and pictures")
        data = await file.read()
        if len(data) > app.state.settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "file too large")
        name = Path(file.filename or "upload").name
        sha = hashlib.sha256(data).hexdigest()
        llm = vision_llm()
        try:
            if name.lower().endswith(".pdf"):
                out = _read_drawing(data, name, page, llm)
            elif name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                out = _read_picture(data, name, llm, [d for d in docs.split(",") if d] or DEFAULT_DOCS)
            else:
                raise HTTPException(400, "upload a picture (.png, .jpg) or a drawing (.pdf)")
        except HTTPException:
            raise
        except Exception as exc:  # Gemini quota, unreadable file
            raise HTTPException(502, f"could not read {name}: {str(exc)[:200]}") from exc
        _audit(who, "copilot.read", {"file": name, "sha256": sha, "kind": out["kind"], "model": llm.model,
                                     "readings": out["n_readings"], "confirmed": out["n_confirmed"]})
        return out

    def _read_picture(data: bytes, name: str, llm, docs: list[str]) -> dict:
        from PIL import Image
        from rulemate.compliance.service import check_rows
        from rulemate.design.picture import read_picture, to_rows

        values, png = read_picture(data, llm)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        results = check_rows(to_rows(values, name), docs=docs) if _index_ready() else []
        res_by_text = {(r.row.parameter, r.row.raw_value): r for r in results}
        items = []
        for v in values:
            r = res_by_text.get((v.parameter, v.value_text))
            items.append({
                "space": v.space, "parameter": v.parameter, "written": v.value_text, "unit": v.unit,
                "confirmed": v.confirmed, "evidence": v.evidence, "notes": v.notes,
                "verdict": r.verdict if r else None,
                "reason": r.reason if r else ("" if v.confirmed else "not found on the picture: not checked"),
                "clauses": sorted({f"{x.limit.doc} § {x.limit.clause_id}" for x in r.applied}) if r else [],
                "crop": _crop_url(img, v.bbox_px)})
        small = img.copy()
        small.thumbnail((900, 900))
        buf = io.BytesIO()
        small.save(buf, format="PNG")
        return {"kind": "picture", "file": name, "preview": _png_url(buf.getvalue()), "items": items,
                "n_readings": len(values), "n_confirmed": sum(v.confirmed for v in values), "docs": docs}

    def _read_drawing(data: bytes, name: str, page: int, llm) -> dict:
        import json

        import pdfplumber
        from rulemate.design.extract import ocr_page
        from rulemate.design.vision import read_sheet, render_png

        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "drawing.pdf"
            pdf.write_bytes(data)
            with pdfplumber.open(pdf) as doc:
                n = len(doc.pages)
            if not 1 <= page <= min(n, MAX_PDF_PAGES):
                raise HTTPException(400, f"page must be between 1 and {n}")
            words = ocr_page(pdf, page, 300)
            rooms = read_sheet(pdf, page, llm, words)
            png, w, h = render_png(pdf, page, 110)
        rid = hashlib.sha256(data + str(page).encode()).hexdigest()[:16]
        rows = [r.__dict__ for r in rooms]
        readings_dir.mkdir(parents=True, exist_ok=True)
        (readings_dir / f"{rid}.json").write_text(json.dumps({"file": name, "page": page, "w": w, "h": h,
                                                               "rooms": rows}), encoding="utf-8")
        cabins = [r for r in rows if r["confirmed"] and r["kind"] == "stateroom"]
        return {"kind": "drawing", "file": name, "page": page, "pages": n, "readings_id": rid,
                "preview": _png_url(png), "items": rows, "n_readings": len(rows),
                "n_confirmed": sum(r["confirmed"] for r in rows),
                "summary": {"cabins": len(cabins),
                            "berths_confirmed": sum(r["berths"] or 0 for r in cabins if r["berths_confirmed"]),
                            "hospital": any(r["confirmed"] and r["kind"] == "hospital" for r in rows)}}

    @router.post("/copilot/design")
    def to_design(body: DesignIn, who: Principal = Depends(engineer)) -> dict:
        import json

        from .model import DesignError, design_from_dict

        p = readings_dir / f"{body.readings_id}.json"
        if not p.exists():
            raise HTTPException(404, "read the drawing first (POST /ruletrace/copilot/read)")
        rec = json.loads(p.read_text(encoding="utf-8"))
        raw = design_from_rooms(rec["rooms"], rec["w"], rec["h"], body.ship, rec["file"], rec["page"])
        if not raw["elements"]:
            raise HTTPException(400, "no confirmed cabins or hospital on this sheet")
        try:
            design = design_from_dict(raw, source_file=rec["file"])
        except DesignError as exc:
            raise HTTPException(400, str(exc)) from exc
        design.interpretation["source"] = f"{rec['file']} p.{rec['page']}"
        design.interpretation["read_by"] = ("Gemini, every room confirmed in code at its label position; positions "
                                            "approximate (label position scaled to the ship length)")
        return save(design, json.dumps(raw).encode(), f"{Path(rec['file']).stem}_p{rec['page']}_gemini.json", who)
