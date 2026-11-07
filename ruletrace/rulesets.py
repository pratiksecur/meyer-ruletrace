"""Proposed rule sets: read a regulation document, turn it into draft checks, and analyse it.

A company (or a class society, flag state, owner) often has its own requirements on top of SOLAS & co.
RuleTrace reads such a document (PDF, Word, text, Markdown, HTML, or RuleTrace YAML) and answers two
questions before anyone relies on it:

1. **What can be checked automatically?** Every "shall / must / is to" sentence with a measurable value
   about a known kind of part (stair, door, corridor, ramp, ventilator, cabin ...) becomes a draft rule,
   quoted word for word and marked ``verified: false``. Sentences that can't be turned into a check are
   listed for an engineer, with the reason, never dropped silently.
2. **Would the new rules work, and what is wrong with them?** The draft rules are run on the designs in
   RuleTrace next to the current rule pack, and each one gets a verdict:

   - *Works*: it checks parts of the designs and gives clear results.
   - *Conflicts*: it can't be met together with a current rule or another new rule
     (e.g. "doors at most 700 mm wide" against PVAG's "at least 815 mm").
   - *Weaker than the current rule*: it would pass parts that the current rule fails (it hides issues).
   - *Stricter than the current rule*: it finds issues the current rules don't (listed, so the cost is visible).
   - *Same as a current rule*: a duplicate.
   - *Can't be checked*: the designs don't give the value it needs (a data gap).
   - *Not tested*: no design has the kind of part it is about.
   - *Broken*: the rule engine rejects it.

The deterministic extraction needs no AI. With ``use_ai`` and a local model running, sentences it could
not turn into a rule are also offered to the model (llm_assist.draft_rule, same checks and fences).
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from html import unescape
from pathlib import Path

import yaml
from vartija_core.docingest import read_document, split_numbered
from vartija_core.quantities import QuantityError, parse_requirement

from .engine import Rule, RuleError, check_design, load_rules

READABLE = {".pdf", ".docx", ".txt", ".md", ".html", ".htm", ".yaml", ".yml"}

# What a sentence is about -> element type, and which property each wording refers to.
# More specific subjects come first ("weathertight door" before "door").
SUBJECTS: list[tuple[str, str, dict, list[tuple[str, str]]]] = [
    (r"collision bulkhead", "bulkhead", {"role": "collision_bulkhead"},
     [(r"forward perpendicular|aft of|distance|located", "distance_from_fp")]),
    (r"weather ?tight doors?|weather doors?|doors? in (?:the )?(?:end bulkheads? of )?superstructures?", "weather_door", {},
     [(r"sill", "sill_height")]),
    (r"stair(?:way)?s?|stair flights?", "stair", {}, [(r"width|wide", "clear_width")]),
    (r"corridors?|passageways?", "corridor", {}, [(r"width|wide", "clear_width")]),
    (r"doors?|doorways?", "door", {}, [(r"threshold|sill", "threshold_height"), (r"width|wide|opening", "clear_width")]),
    (r"ramps?", "ramp", {}, [(r"slope|gradient|steep", "running_slope")]),
    (r"ventilators?", "ventilator", {}, [(r"coaming|height|high", "coaming_height")]),
    (r"air ?pipes?", "air_pipe", {}, [(r"height|high", "height_to_opening")]),
    (r"guard ?rails?|bulwarks?|railings?", "guard_rail", {},
     [(r"opening|gap|below the lowest|lowest course", "lowest_opening"), (r"height|high", "height")]),
    (r"double bottoms?|inner bottom|tank ?top", "double_bottom", {}, [(r"height|depth|above the keel", "geom.height")]),
    (r"main vertical zones?|fire zones?", "fire_zone", {}, [(r"length|long", "geom.length")]),
    (r"cabins?|sleeping rooms?", "cabin", {}, [(r"floor area|area", "floor_area")]),
    (r"helidecks?|helicopter (?:landing )?(?:area|deck)s?", "helideck", {}, [(r"diameter|size|width|d-value", "diameter")]),
    (r"lifeboats?|survival craft", "lifeboat", {}, [(r"capacity|persons", "capacity")]),
]
WHEN_HINTS = [(r"escape routes?|means of escape", "escape_route", True), (r"accessible|wheelchair|persons with disabilit", "accessible_route", True),
              (r"position\s*1\b", "position", "1"), (r"position\s*2\b", "position", "2"),
              (r"freeboard deck", "deck_kind", "freeboard"), (r"superstructure deck", "deck_kind", "superstructure"),
              (r"\bsingle(?:-berth)?\b", "berths", 1), (r"\bdouble\b|two-berth|2-berth", "berths", 2)]
WHEN_FOR = {"escape_route": {"stair", "corridor", "door", "ramp"}, "accessible_route": {"corridor", "door", "ramp"},
            "position": {"ventilator"}, "deck_kind": {"air_pipe"}, "berths": {"cabin"}}
MODAL = re.compile(r"\b(shall|must|is to|are to|should|required|minimum|maximum|not less than|not more than|at least|at most)\b", re.I)
METRIC = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mm|cm|m²|m2|m|min)\b(?!\w)")
SLOPE = re.compile(r"\b1\s*:\s*(\d+(?:[.,]\d+)?)\b")
OPS = [(r"not less than|no less than|at least|a minimum of|minimum|min\.|not be less than|≥", "at least"),
       (r"not more than|no more than|not exceed(?:ing)?|at most|a maximum of|maximum|max\.|up to|not be more than|≤|less than", "at most")]


# -- reading documents ------------------------------------------------------------------------------

def read_text(path: Path) -> str:
    """Plain text of a regulation document."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        return unescape(re.sub(r"<[^>]+>", "", xml))
    if suffix == ".pdf":
        try:
            return read_document(path)
        except RuntimeError:  # pypdf not installed: use PDFium (the drawings extra)
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(str(path))
            text = "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
            doc.close()
            return text.replace("\r", "")
    if suffix in {".txt", ".md", ".html", ".htm"}:
        return read_document(path)
    raise ValueError(f"can't read {suffix} files; use one of {', '.join(sorted(READABLE))}")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", s.upper()).strip("-")[:40] or "RULE"


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z(])", text) if s.strip()]


def _requirement(sentence: str, prop: str) -> tuple[dict | None, str]:
    """('at least 1000 mm' check dict) or (None, why not)."""
    low = sentence.lower()
    if prop == "running_slope":
        m = SLOPE.search(sentence)
        return ({"property": prop, "slope_max": f"1:{m.group(1)}"}, "") if m else (None, "no slope written as 1:N")
    m = METRIC.search(sentence)
    if not m:
        return None, ("the value is not in metric units" if re.search(r"\d+\s*(?:in|inch|inches|ft|feet)\b", low)
                      else "no measurable value")
    before = low[:m.start()]
    op = None
    for rx, word in OPS:
        hits = [h for h in re.finditer(rx, before)]
        if hits and (op is None or hits[-1].end() > op[1]):
            op = (word, hits[-1].end())
    if op is None:
        return None, "no 'at least' / 'at most' wording before the value"
    unit = {"m²": "m2"}.get(m.group(2), m.group(2))
    return {"property": prop, "requirement": f"{op[0]} {m.group(1).replace(',', '.')} {unit}"}, ""


def extract_rules(text: str, name: str, source: str = "") -> tuple[list[dict], list[dict], int]:
    """Draft rules, sentences needing an engineer, and the number of paragraphs read."""
    paras = split_numbered(text, prefix="doc")
    rules, manual, seen = [], [], set()
    prefix = _slug(name)[:16]
    for p in paras:
        body = (p.title + ". " + p.text) if p.title and p.title != p.num and p.title not in p.text else p.text
        for sent in _sentences(body):
            if not MODAL.search(sent):
                continue
            low = sent.lower()
            subj = next(((rx, t, when, props) for rx, t, when, props in SUBJECTS if re.search(r"\b(?:" + rx + r")\b", low)), None)
            if not subj:
                manual.append({"ref": p.num, "text": sent, "reason": "the part it is about is not one RuleTrace models yet"})
                continue
            rx, etype, when, props = subj
            prop = next((pp for wrx, pp in props if re.search(wrx, low)), None)
            if not prop:
                manual.append({"ref": p.num, "text": sent, "reason": f"about {etype.replace('_', ' ')}s, but not a property RuleTrace checks"})
                continue
            check, why_not = _requirement(sent, prop)
            if not check:
                manual.append({"ref": p.num, "text": sent, "reason": why_not})
                continue
            when = dict(when)
            for hrx, flag, val in WHEN_HINTS:
                if re.search(hrx, low) and etype in WHEN_FOR[flag]:
                    when[flag] = val
            rid = f"{prefix}-{_slug(p.num if p.num != 'preamble' else 'P')}-{_slug(etype)}-{_slug(prop.split('.')[-1])}"
            k = 2
            while rid in seen:
                rid, k = f"{rid.rsplit('~', 1)[0]}~{k}", k + 1
            seen.add(rid)
            applies = {"passenger_ship": True} if re.search(r"passenger ships?", low) else {}
            label = prop.split(".")[-1].replace("_", " ")
            rules.append({"id": rid, "title": f"{etype.replace('_', ' ').capitalize()} {label}", "regime": name,
                          "ref": f"{name} {p.num}" if p.num != "preamble" else name, "quote": sent[:400],
                          "source_url": source, "verified": False, "applies_if": applies,
                          "target": {"type": etype, **({"when": when} if when else {})}, "check": check,
                          "severity": "major", "why": f"Requirement of {name}.",
                          "fix": f"Change the {label} to {check.get('requirement') or 'not steeper than ' + check.get('slope_max', '')}.",
                          "extracted_by": "rules"})
    return rules, manual, len(paras)


def rules_from_yaml(text: str, name: str) -> list[dict]:
    raw = yaml.safe_load(text) or []
    if not isinstance(raw, list):
        raise ValueError("a RuleTrace rule file is a YAML list of rules")
    for r in raw:
        if isinstance(r, dict):
            r.setdefault("regime", name)
            r.setdefault("ref", name)
            r.setdefault("extracted_by", "yaml")
    return [r for r in raw if isinstance(r, dict)]


def ai_drafts(manual: list[dict], name: str, existing_ids: set[str], limit: int = 8) -> tuple[list[dict], str]:
    """Offer the sentences the rules couldn't handle to the local model. Returns (rules, note)."""
    from . import llm_assist
    client = llm_assist.OllamaClient()
    st = client.status()
    if not (st.get("reachable") and st.get("model_pulled")):
        return [], "AI helper not available, so only the rule-based extraction was used."
    vocab = llm_assist.rule_vocabulary([r.__dict__ for r in load_rules()])
    out = []
    for m in manual[:limit]:
        try:
            res = llm_assist.draft_rule(client, m["text"], regime=name, ref=m["ref"], existing_ids=existing_ids, vocabulary=vocab)
        except Exception:  # noqa: BLE001 - one bad paragraph must not stop the analysis
            continue
        for d in res["drafts"]:
            if d["ok"]:
                d["rule"]["extracted_by"] = client.name
                out.append(d["rule"])
                m["ai_drafted"] = d["rule"]["id"]
    return out, f"AI helper ({client.name}) drafted {len(out)} more rule(s) from sentences the rules couldn't handle."


# -- analysis ---------------------------------------------------------------------------------------------

def _load_one(r: dict) -> tuple[Rule | None, str]:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rule.yaml"
        clean = {k: v for k, v in r.items() if k != "extracted_by"}
        p.write_text(yaml.safe_dump([clean], sort_keys=False, allow_unicode=True), encoding="utf-8")
        try:
            return load_rules([p])[0], ""
        except (RuleError, KeyError, TypeError, ValueError) as exc:
            return None, str(exc)


def _key(r: Rule) -> tuple:
    return (r.target.get("type"), r.check.get("property"))


def _overlap(a: Rule, b: Rule) -> bool:
    """Same kind of part and property, and conditions that can hold together ("doors" overlaps "accessible doors")."""
    if _key(a) != _key(b):
        return False
    wa, wb = a.target.get("when") or {}, b.target.get("when") or {}
    return all(wa[k] == wb[k] for k in set(wa) & set(wb))


def _bound(r: Rule):
    """('>=' | '<=', base value, dimension) for a fixed quantity requirement, else None."""
    c = r.check
    if "slope_max" in c:
        n = float(str(c["slope_max"]).split(":")[1])
        return ("<=", 1 / n, "slope")
    if "requirement" not in c:
        return None
    try:
        op, q = parse_requirement(str(c["requirement"]))
    except QuantityError:
        return None
    op = {">": ">=", "<": "<="}.get(op, op)
    return (op, q.base_value, q.dimension) if op in {">=", "<="} else None


def _relation(new: Rule, old: Rule) -> str | None:
    """'conflict' | 'stricter' | 'looser' | 'same' | None (not comparable)."""
    a, b = _bound(new), _bound(old)
    if not a or not b or a[2] != b[2]:
        return None
    (oa, va, _), (ob, vb, _) = a, b
    eps = 1e-9 * max(1.0, abs(va), abs(vb))
    if oa != ob:
        lo, hi = (va, vb) if oa == ">=" else (vb, va)  # at least lo, at most hi
        return "conflict" if lo > hi + eps else None
    if abs(va - vb) <= eps:
        return "same"
    return ("stricter" if va > vb else "looser") if oa == ">=" else ("stricter" if va < vb else "looser")


def _limit(r: Rule) -> dict | None:
    """The fixed limit of a rule, for charts: {op, value, unit, base} (slopes as 1:N)."""
    c = r.check
    if "slope_max" in c:
        n = float(str(c["slope_max"]).split(":")[1])
        return {"op": "<=", "value": n, "unit": "1:N", "base": 1 / n, "label": f"1:{c['slope_max'].split(':')[1]}"}
    if "requirement" not in c:
        return None
    try:
        op, q = parse_requirement(str(c["requirement"]))
    except QuantityError:
        return None
    return {"op": op, "value": q.value, "unit": q.unit, "base": q.base_value, "label": str(q)}


def _short_ref(r: Rule) -> str:
    """A short source label: 'FSS Code ch. 13, 2.1.1 (via SOLAS ...)' -> 'FSS Code ch. 13, 2.1.1'."""
    ref = re.split(r"\s+\(|;", r.ref)[0].strip()
    named = re.search(r"ICLL|FSS|ADA|MLC|COLREG|MARPOL|SOLAS|PVAG|Drawing|DNV|IMO", ref)
    out = ref if named else f"{r.regime.split('/')[0].strip()} {ref}"
    return out if len(out) <= 36 else out[:35].rstrip(" ,") + "…"


def _req_text(r: Rule) -> str:
    c = r.check
    return c.get("requirement") or c.get("requirement_expr") or (f"not steeper than {c['slope_max']}" if "slope_max" in c else "") \
        or c.get("fire_class_at_least", "")


def analyze(drafts: list[dict], designs: list, current: list[Rule] | None = None) -> dict:
    """Run the proposed rules on the designs next to the current rule pack and give each a verdict."""
    ids = {d.get("id") for d in drafts}
    current = [r for r in (current if current is not None else load_rules()) if r.id not in ids]  # not against itself once active
    loaded, broken = [], []
    for d in drafts:
        r, err = _load_one(d)
        (loaded.append((r, d)) if r else broken.append({"id": d.get("id", "?"), "title": d.get("title", ""),
                                                        "quote": d.get("quote", ""), "problem": err}))
    new_rules = [r for r, _ in loaded]
    related = {r.id: [o for o in current if _overlap(r, o)] for r in new_rules}

    per_rule = {r.id: {"checked": 0, "fail": 0, "pass": 0, "unknown": 0, "designs": set(), "hides": [], "new_issues": [],
                       "missing": set()} for r in new_rules}
    design_rows = []
    for name, design in designs:
        cur = check_design(design, current)
        new = check_design(design, new_rules)
        cur_status = {(f["rule_id"], f["element_id"]): f["status"] for f in cur["findings"]}
        types = {e.type for e in design.elements}
        row = {"design": name, "new_fail": 0, "new_unknown": 0, "hidden": 0, "extra_issues": 0}
        for f in new["findings"]:
            s = per_rule[f["rule_id"]]
            s["checked"] += 1
            s[f["status"] if f["status"] in ("fail", "pass", "unknown") else "fail"] += 1
            s["designs"].add(name)
            if f["status"] == "unknown":
                s["missing"].add(f["message"].replace("Can't check: ", ""))
                row["new_unknown"] += 1
            if f["status"] == "fail":
                row["new_fail"] += 1
            rule = next(r for r in new_rules if r.id == f["rule_id"])
            for old in related[rule.id]:
                st_old = cur_status.get((old.id, f["element_id"]))
                part = {"design": name, "element": f["element_name"], "rule": old.id}
                if st_old == "fail" and f["status"] == "pass" and not any(
                        h["design"] == name and h["element"] == part["element"] for h in s["hides"]):
                    s["hides"].append(part)
                    row["hidden"] += 1
                if st_old == "pass" and f["status"] == "fail" and not any(
                        h["design"] == name and h["element"] == part["element"] for h in s["new_issues"]):
                    s["new_issues"].append(part)
                    row["extra_issues"] += 1
        row["types"] = types
        design_rows.append(row)
    all_types = set().union(*(r["types"] for r in design_rows)) if design_rows else set()
    for r in design_rows:
        r.pop("types")

    verdicts = []
    for r, d in loaded:
        s = per_rule[r.id]
        rel = [(old, _relation(r, old)) for old in related[r.id]]
        rel = [(o, k) for o, k in rel if k]
        internal = [o for o in new_rules if o.id != r.id and _overlap(r, o) and _relation(r, o) == "conflict"]
        conflicts = [o for o, k in rel if k == "conflict"] + internal
        looser = [o for o, k in rel if k == "looser"]
        stricter = [o for o, k in rel if k == "stricter"]
        same = [o for o, k in rel if k == "same"]
        etype = r.target.get("type")
        if conflicts:
            v, kind = (f"Conflicts with {', '.join(o.id for o in conflicts)}: no design can meet both "
                       f"({_req_text(r)} vs {', '.join(_req_text(o) for o in conflicts)})."), "flaw"
        elif looser and s["hides"]:
            v, kind = (f"Weaker than {looser[0].id} ({_req_text(r)} vs {_req_text(looser[0])}): it would pass "
                       f"{len(s['hides'])} part(s) the current rule fails."), "flaw"
        elif looser:
            v, kind = f"Weaker than {looser[0].id} ({_req_text(r)} vs {_req_text(looser[0])}). No current design is affected.", "warn"
        elif s["checked"] and s["unknown"] == s["checked"]:
            v, kind = f"Can't be checked on these designs: {sorted(s['missing'])[0]}", "gap"
        elif not s["checked"] and etype not in all_types:
            v, kind = f"Not tested: none of the designs has a {etype.replace('_', ' ')}.", "gap"
        elif not s["checked"]:
            v, kind = "Doesn't apply to any of these ships (check its conditions).", "gap"
        elif same:
            v, kind = f"Same as the current rule {same[0].id}: a duplicate.", "info"
        else:
            extra = f" Stricter than {stricter[0].id}: {len(s['new_issues'])} new issue(s)." if stricter else ""
            v, kind = (f"Works: checked {s['checked']} part(s) in {len(s['designs'])} design(s), "
                       f"{s['fail']} fail, {s['pass']} pass, {s['unknown']} can't check.{extra}"), "ok"
        verdicts.append({"id": r.id, "title": r.title, "ref": r.ref, "quote": r.quote, "requirement": _req_text(r),
                         "target": etype, "extracted_by": d.get("extracted_by", ""), "kind": kind, "verdict": v,
                         "checked": s["checked"], "fail": s["fail"], "pass": s["pass"], "unknown": s["unknown"],
                         "hides": s["hides"][:10], "new_issues": s["new_issues"][:10],
                         "related": [{"id": o.id, "relation": k, "requirement": _req_text(o)} for o, k in rel],
                         "limit": _limit(r),
                         "compare": [{"id": o.id, "relation": k, "rule": _short_ref(o), **(_limit(o) or {})}
                                     for o, k in rel if _limit(o)]
                                    + [{"id": o.id, "relation": "conflict", "new_rule": True, "rule": o.ref, **(_limit(o) or {})}
                                       for o in internal if _limit(o)]})
    for b in broken:
        verdicts.append({**b, "kind": "flaw", "verdict": f"Broken: {b['problem']}", "checked": 0})
    counts = {k: sum(1 for v in verdicts if v["kind"] == k) for k in ("ok", "info", "warn", "gap", "flaw")}
    if not verdicts:
        overall = "No checkable rules were found in this document."
    elif counts["flaw"]:
        overall = f"Not ready: {counts['flaw']} rule(s) have flaws (conflicts, weaker than today's rules, or broken)."
    elif counts["gap"] or counts["warn"]:
        overall = f"Usable with care: {counts['gap']} rule(s) can't be tested on these designs, {counts['warn']} weaker than today's rules."
    else:
        overall = "Ready to review: every rule works on these designs and none conflicts with the current rules."
    return {"overall": overall, "counts": counts, "verdicts": verdicts, "designs": design_rows,
            "designs_used": [n for n, _ in designs]}
