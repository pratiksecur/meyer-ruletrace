"""Compare two revisions of a design: what got fixed, what is still open, and what the change broke.

    POST /ruletrace/designs/{older}/compare/{newer}

Both revisions are checked with the same rule pack, and findings are matched by (rule, part). A part is
matched by its ID, so the comparison works best when the design keeps part IDs between revisions (as IFC
GlobalIds and drawing labels usually do). Parts that were added, removed or changed are listed with the
values that changed, so an engineer can see *why* a result moved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from vartija_core import audit
from vartija_core.api import Principal

from .engine import RuleError, check_design, load_rules
from .model import DesignError, design_from_dict

PROBLEM = {"fail", "warn", "unknown"}
SHIP_EDITABLE = {"passengers", "persons_total", "length_m", "breadth_m", "gross_tonnage", "enclosed_volume_m3", "flag",
                 "keel_laid", "ship_type", "operating_areas"}


class CompareIn(BaseModel):
    ship_overrides: dict = Field(default_factory=dict, description="the same what-if profile for both revisions")


class Change(BaseModel):
    element_id: str = Field(max_length=120)
    prop: str = Field(max_length=60, description="a property name, or 'length' / 'width' / 'height' for the size")
    value: str | float | int | bool | None = None


class ReviseIn(BaseModel):
    changes: list[Change] = Field(default_factory=list, max_length=200)
    ship: dict = Field(default_factory=dict)
    note: str = Field(default="", max_length=200)


def apply_changes(design: dict, changes: list[Change], ship: dict) -> tuple[dict, list[dict]]:
    """A copy of a stored design dict with the engineer's changed measurements, and a log of what changed."""
    d = copy.deepcopy(design)
    els = {e["id"]: e for e in d["elements"]}
    log = []
    for c in changes:
        el = els.get(c.element_id)
        if el is None:
            raise HTTPException(400, f"no part '{c.element_id}' in this design")
        if c.prop in {"length", "width", "height"}:
            if not el.get("box"):
                raise HTTPException(400, f"{c.element_id} has no size to change")
            try:
                v = float(str(c.value).replace(",", ".").split()[0])
            except (ValueError, IndexError) as exc:
                raise HTTPException(400, f"{c.element_id} {c.prop}: give a number of metres") from exc
            if not 0 < v < 2000:
                raise HTTPException(400, f"{c.element_id} {c.prop}: {v} m is not a plausible size")
            i = {"length": 3, "width": 4, "height": 5}[c.prop]
            before = el["box"][i]
            el["box"] = list(el["box"])
            el["box"][i] = v
            log.append({"element_id": c.element_id, "what": c.prop, "before": f"{before:g} m", "after": f"{v:g} m"})
        else:
            if c.prop in {"id", "type"}:
                raise HTTPException(400, "a part's id and kind can't be changed here")
            props = el.setdefault("props", {})
            before = props.get(c.prop)
            props[c.prop] = c.value
            log.append({"element_id": c.element_id, "what": c.prop, "before": before, "after": c.value})
            # rooms read from a drawing are drawn as a square of their stated area: redraw it at the new area
            if c.prop == "floor_area" and el.get("box") and "approximate" in str(props.get("position", "")):
                try:
                    area = float(str(c.value).replace(",", ".").split()[0].rstrip("m2²"))
                except (ValueError, IndexError):
                    area = 0
                if area > 0:
                    x, y, z, lx, wy, hz = el["box"]
                    side = round(area ** 0.5, 2)
                    el["box"] = [round(x + lx / 2 - side / 2, 2), round(y + wy / 2 - side / 2, 2), z, side, side, hz]
    bad = set(ship) - SHIP_EDITABLE
    if bad:
        raise HTTPException(400, f"can't change {sorted(bad)} of the ship")
    for k, v in ship.items():
        log.append({"element_id": None, "what": k, "before": d["ship"].get(k), "after": v})
        d["ship"][k] = v
    return d, log


def _key(f: dict) -> tuple:
    return (f["rule_id"], f.get("element_id"))


def compare_results(old: dict, new: dict) -> dict:
    """Diff two check results (from check_design) of the same ship."""
    a = {_key(f): f for f in old["findings"]}
    b = {_key(f): f for f in new["findings"]}

    def item(f: dict, was: str | None, now: str | None) -> dict:
        return {"rule_id": f["rule_id"], "title": f["title"], "ref": f["ref"], "element_id": f.get("element_id"),
                "element_name": f.get("element_name"), "was": was, "now": now,
                "actual_before": (a.get(_key(f)) or {}).get("actual"), "actual_after": (b.get(_key(f)) or {}).get("actual"),
                "required": f.get("required"), "message": f.get("message")}

    fixed, still, broken, other = [], [], [], []
    for k in sorted(set(a) | set(b), key=str):
        fa, fb = a.get(k), b.get(k)
        sa, sb = (fa or {}).get("status"), (fb or {}).get("status")
        f = fb or fa
        if sa in PROBLEM and (sb == "pass" or sb is None):
            fixed.append(item(f, sa, sb or "no longer applies"))
        elif sa in PROBLEM and sb in PROBLEM:
            still.append(item(f, sa, sb))
        elif sb in PROBLEM and (sa == "pass" or sa is None):
            broken.append(item(f, sa or "not checked before", sb))
        elif sa != sb:
            other.append(item(f, sa, sb))

    ea = {e["id"]: e for e in old["elements"]}
    eb = {e["id"]: e for e in new["elements"]}
    changed = []
    for i in sorted(set(ea) & set(eb)):
        diffs = []
        pa, pb = ea[i].get("props") or {}, eb[i].get("props") or {}
        for k in sorted(set(pa) | set(pb)):
            if pa.get(k) != pb.get(k):
                diffs.append({"what": k, "before": pa.get(k), "after": pb.get(k)})
        if (ea[i].get("box") or []) != (eb[i].get("box") or []):
            diffs.append({"what": "position / size", "before": ea[i].get("box"), "after": eb[i].get("box")})
        if diffs:
            changed.append({"id": i, "name": eb[i].get("name") or i, "type": eb[i]["type"], "changes": diffs})
    ship_changes = [{"what": k, "before": old["ship"].get(k), "after": new["ship"].get(k)}
                    for k in sorted(set(old["ship"]) | set(new["ship"]))
                    if k != "notes" and old["ship"].get(k) != new["ship"].get(k)]
    added = [{"id": i, "name": eb[i].get("name") or i, "type": eb[i]["type"]} for i in sorted(set(eb) - set(ea))]
    removed = [{"id": i, "name": ea[i].get("name") or i, "type": ea[i]["type"]} for i in sorted(set(ea) - set(eb))]
    open_before = sum(1 for f in old["findings"] if f["status"] in PROBLEM)
    open_after = sum(1 for f in new["findings"] if f["status"] in PROBLEM)
    if broken:
        verdict = f"{len(fixed)} fixed, but the change broke {len(broken)} other check(s). Look at those before you continue."
    elif fixed and not still:
        verdict = f"All {len(fixed)} open problem(s) fixed, and nothing else broke."
    elif fixed:
        verdict = f"{len(fixed)} fixed, nothing new broke, {len(still)} still open."
    else:
        verdict = "No problem was fixed by this revision." + (f" {len(still)} still open." if still else "")
    return {"verdict": verdict, "open_before": open_before, "open_after": open_after,
            "fixed": fixed, "still_open": still, "broken": broken, "other_changes": other,
            "parts_changed": changed, "parts_added": added, "parts_removed": removed, "ship_changes": ship_changes}


def add_revision_routes(app, router, engineer, path_of, reader=None) -> None:
    reader = reader or engineer
    def _check(design_id: str, overrides: dict | None = None) -> tuple[dict, dict]:
        rec = json.loads(path_of(design_id).read_text(encoding="utf-8"))
        bad = set(overrides or {}) - SHIP_EDITABLE
        if bad:
            raise HTTPException(400, f"cannot override {sorted(bad)}")
        d = {**rec["design"], "ship": {**rec["design"]["ship"], **(overrides or {})}}
        try:
            return rec, check_design(design_from_dict(d, source_file=rec["filename"]), load_rules())
        except (DesignError, RuleError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/designs/{design_id}/revise")
    def revise(design_id: str, body: ReviseIn, who: Principal = Depends(engineer)) -> dict:
        """Save the design with the engineer's changed measurements as a new revision (the original is kept)."""
        if not body.changes and not body.ship:
            raise HTTPException(400, "nothing changed")
        p = path_of(design_id)
        rec = json.loads(p.read_text(encoding="utf-8"))
        new_design, log = apply_changes(rec["design"], body.changes, body.ship)
        try:
            design_from_dict(new_design, source_file=rec["filename"])        # must still be a valid design
        except DesignError as exc:
            raise HTTPException(400, str(exc)) from exc
        n = sum(1 for _ in p.parent.glob("*.json"))
        new_design["title"] = f"{rec['design'].get('title') or rec['filename']} (edited: {len(log)} change{'s' if len(log) != 1 else ''})"
        blob = json.dumps(new_design, sort_keys=True).encode()
        nid = uuid.uuid4().hex[:12]
        new = {"id": nid, "filename": f"{rec['filename']} (edited)", "sha256": hashlib.sha256(blob).hexdigest(),
               "uploaded_by": who.name, "uploaded_at": datetime.now(timezone.utc).isoformat(), "design": new_design,
               "revision_of": design_id, "changes": log, "note": body.note}
        (p.parent / f"{nid}.json").write_text(json.dumps(new, indent=2), encoding="utf-8")
        with app.state.session_factory() as s:
            audit.append(s, action="design.revised", actor=who.name, actor_kind=who.kind, payload={
                "from": design_id, "from_sha256": rec["sha256"], "to": nid, "to_sha256": new["sha256"],
                "changes": log[:50], "note": body.note})
            s.commit()
        return {"id": nid, "revision_of": design_id, "changes": log, "title": new_design["title"], "existing_designs": n}

    @router.post("/designs/{older}/compare/{newer}")
    def compare(older: str, newer: str, body: CompareIn | None = None, who: Principal = Depends(reader)) -> dict:
        if older == newer:
            raise HTTPException(400, "pick two different revisions")
        ov = (body.ship_overrides if body else {}) or {}
        ra, a = _check(older, ov)
        rb, b = _check(newer, ov)
        out = compare_results(a, b)
        out.update(older={"id": older, "file": ra["filename"], "title": ra["design"].get("title")},
                   newer={"id": newer, "file": rb["filename"], "title": rb["design"].get("title")})
        with app.state.session_factory() as s:
            audit.append(s, action="design.compared", actor=who.name, actor_kind=who.kind, payload={
                "older": older, "newer": newer, "older_sha256": ra["sha256"], "newer_sha256": rb["sha256"],
                "fixed": len(out["fixed"]), "still_open": len(out["still_open"]), "broken": len(out["broken"])})
            s.commit()
        return out
