"""Rule engine: which rules apply to this ship, and does each design element comply?

Design choices that serve accuracy:
- Every comparison is done in code (``vartija_core.quantities``), never by a language model.
- A property the design doesn't state gives status ``unknown`` ("can't check"), never a guess.
- Every finding carries the rule's source (regime, paragraph, quoted text, link) and says in
  plain words what was required, what the design has, and how to fix it.
- Applicability is explicit: the report lists the rules that do NOT apply and why.
"""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from vartija_core.quantities import QuantityError, check_requirement, parse_quantity

from .model import Design, Element, Ship

RULES_DIR = Path(__file__).parent / "rules"
FIRE_CLASS_ORDER = ["C", "B-0", "B-15", "A-0", "A-15", "A-30", "A-60"]
STATUS_ORDER = {"fail": 0, "warn": 1, "unknown": 2, "pass": 3}


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    id: str
    title: str
    regime: str
    ref: str
    quote: str
    source_url: str
    verified: bool
    target: dict
    check: dict
    applies_if: dict = field(default_factory=dict)
    severity: str = "major"         # major | minor | advisory (advisory failures are warnings)
    fix: str = ""
    why: str = ""
    pack: str = ""


def load_rules(paths: list[Path] | None = None) -> list[Rule]:
    files = paths or sorted(RULES_DIR.glob("*.yaml"))
    rules: list[Rule] = []
    seen: set[str] = set()
    for f in files:
        for raw in yaml.safe_load(Path(f).read_text(encoding="utf-8")) or []:
            try:
                r = Rule(id=raw["id"], title=raw["title"], regime=raw["regime"], ref=raw["ref"],
                         quote=raw.get("quote", ""), source_url=raw.get("source_url", ""),
                         verified=bool(raw.get("verified", False)), target=raw["target"], check=raw["check"],
                         applies_if=raw.get("applies_if") or {}, severity=raw.get("severity", "major"),
                         fix=raw.get("fix", ""), why=raw.get("why", ""), pack=Path(f).stem)
            except KeyError as exc:
                raise RuleError(f"{f}: rule {raw.get('id', '?')} is missing {exc}") from exc
            if r.id in seen:
                raise RuleError(f"duplicate rule id {r.id}")
            seen.add(r.id)
            rules.append(r)
    return rules


# -- applicability --------------------------------------------------------------------------

def applicability(rule: Rule, ship: Ship) -> tuple[bool, list[str]]:
    """(applies, reasons). Reasons explain the decision either way."""
    a, why, ok = rule.applies_if, [], True

    def test(cond: bool, yes: str, no: str) -> None:
        nonlocal ok
        why.append(yes if cond else no)
        ok = ok and cond

    if "ship_types" in a:
        test(ship.ship_type in a["ship_types"], f"ship type is {ship.ship_type}",
             f"only for {', '.join(a['ship_types'])} ships (this is {ship.ship_type})")
    if a.get("passenger_ship"):
        test(ship.is_passenger_ship, "passenger ship (more than 12 passengers)", "not a passenger ship")
    if "passengers_more_than" in a:
        n = a["passengers_more_than"]
        test(ship.passengers > n, f"carries {ship.passengers} passengers (> {n})",
             f"only for ships carrying more than {n} passengers (this carries {ship.passengers})")
    if "keel_laid_on_or_after" in a:
        d = a["keel_laid_on_or_after"]
        d = d if isinstance(d, date) else date.fromisoformat(str(d))
        if ship.keel_laid is None:
            test(False, "", f"keel-laying date not given; needed to decide (rule applies from {d})")
        else:
            test(ship.keel_laid >= d, f"keel laid {ship.keel_laid} (on or after {d})",
                 f"only for ships with keel laid on or after {d} (this: {ship.keel_laid})")
    if a.get("complete_arrangement"):
        test(ship.complete_arrangement, "the design covers the whole ship",
             "the design is a partial model; 'is it on board?' checks need the complete arrangement")
    if "persons_at_least" in a:
        n = a["persons_at_least"]
        test(ship.persons_total >= n, f"{ship.persons_total} persons on board (≥ {n})",
             f"only for ships with {n} or more persons on board (this: {ship.persons_total})")
    if "length_m_at_least" in a:
        n = a["length_m_at_least"]
        test(ship.length_m >= n, f"length {ship.length_m} m (≥ {n} m)", f"only for ships of {n} m or more")
    if "gross_tonnage_at_least" in a:
        n = a["gross_tonnage_at_least"]
        test(ship.gross_tonnage >= n, f"{ship.gross_tonnage:g} GT (≥ {n})", f"only for ships of {n} GT or more")
    if "operating_areas_any" in a:
        hit = sorted(set(a["operating_areas_any"]) & set(ship.operating_areas))
        test(bool(hit), f"operates in {', '.join(hit)}",
             f"only for ships operating in {', '.join(a['operating_areas_any'])}")
    return ok, why


# -- safe expressions -------------------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg}


def _to_unit(q_text: str, unit: str) -> float:
    """Value of q_text expressed in unit, using only Vartija's public quantities API."""
    q, ref = parse_quantity(str(q_text)), parse_quantity(f"1 {unit}")
    if ref.dimension != q.dimension:
        raise QuantityError(f"cannot express {q_text} in {unit}")
    return q.base_value / ref.base_value


class _Ctx:
    def __init__(self, design: Design, element: Element | None):
        self.design, self.element = design, element

    def find(self, role: str) -> Element:
        el = next((e for e in self.design.elements if e.props.get("role") == role), None)
        if el is None:
            raise LookupError(f"the design doesn't include the {role.replace('_', ' ')} (not shown on this drawing or model)")
        return el

    def call(self, name: str, args: list):
        if name in {"min", "max"}:
            return {"min": min, "max": max}[name](*args)
        if name == "round":
            return round(*args)
        if name == "ref":           # ref(role, property, unit) -> number
            role, prop, unit = args
            el = self.find(role)
            if prop not in el.props:
                raise LookupError(f"{el.id} has no '{prop}'")
            return _to_unit(el.props[prop], unit)
        if name == "count":         # count(type) -> number of elements of that type
            return sum(1 for e in self.design.elements if e.type == args[0])
        if name == "sum_prop":      # sum_prop(type, prop) -> sum of a numeric property
            return sum(int(e.props.get(args[1], 0)) for e in self.design.elements if e.type == args[0])
        if name == "count_on_deck":  # count_on_deck(type, prop, value) on this element's deck
            t, prop, val = args
            return sum(1 for e in self.design.elements
                       if e.type == t and e.deck == self.element.deck and e.props.get(prop) == val)
        if name == "prop":          # prop(name) of the element being checked
            if args[0] not in self.element.props:
                raise LookupError(f"{self.element.id} has no '{args[0]}'")
            return self.element.props[args[0]]
        if name == "gt_band":       # gt_band(gt, limit1, limit2, v1, v2, v3); unknown GT -> strictest (v3)
            gt, l1, l2, v1, v2, v3 = args
            return v3 if gt <= 0 else (v1 if gt < l1 else (v2 if gt < l2 else v3))
        if name == "geom":          # geom(role, key) -> metres
            role, key = args
            v = self.find(role).geom(key)
            if v is None:
                raise LookupError(f"element with role '{role}' has no geometry")
            return v
        raise RuleError(f"function {name}() is not allowed")

    def eval(self, src: str) -> float:
        def ev(n):
            if isinstance(n, ast.Expression):
                return ev(n.body)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, str)):
                return n.value
            if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
                return _OPS[type(n.op)](ev(n.left), ev(n.right))
            if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
                return _OPS[type(n.op)](ev(n.operand))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "ship":
                return getattr(self.design.ship, n.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                return self.call(n.func.id, [ev(a) for a in n.args])
            raise RuleError(f"expression not allowed: {ast.dump(n)[:60]}")
        return ev(ast.parse(src, mode="eval"))

    def fill(self, template: str) -> str:
        def rep(m):
            v = self.eval(m.group(1))
            return f"{round(v, 3):g}" if isinstance(v, float) else str(v)
        return re.sub(r"\{([^{}]+)\}", rep, template)


# -- checking ----------------------------------------------------------------------------------

def _targets(rule: Rule, design: Design) -> list[Element | None]:
    t = rule.target
    if t.get("type") == "ship":
        return [None]
    out = []
    for e in design.elements:
        if e.type != t.get("type"):
            continue
        when = t.get("when") or {}
        if all(e.props.get(k) == v for k, v in when.items()):
            out.append(e)
    return out


def _slope(text: str) -> float:
    m = re.fullmatch(r"\s*1\s*:\s*(\d+(?:\.\d+)?)\s*", str(text))
    if not m:
        raise QuantityError(f"slope must be written as 1:N, got '{text}'")
    return 1 / float(m.group(1))


def _value(rule: Rule, el: Element | None, ctx: _Ctx) -> tuple[str | None, str]:
    c = rule.check
    if "actual_expr" in c:
        return ctx.fill(c["actual_expr"]), "computed from the model"
    prop = c["property"]
    if prop.startswith("geom."):
        v = el.geom(prop[5:]) if el else None
        return (f"{v:g} m" if v is not None else None), "measured from the 3D geometry"
    v = el.props.get(prop) if el else None
    return (None if v is None else str(v)), "from the design data"


_OP_WORDS = [(">=", "at least"), ("<=", "at most"), (">", "more than"), ("<", "less than"), ("==", "exactly")]


def _plain(required: str, actual, ok: bool) -> str:
    """'>= 12 m' and '11 m' -> 'The design has 11 m; the rule needs at least 12 m.'"""
    req = str(required).strip()
    for op, words in _OP_WORDS:
        if req.startswith(op):
            need = f"{words} {req[len(op):].strip()}"
            break
    else:
        need = req
    return f"The design has {actual}; the rule needs {need}." + ("" if ok else " Not met.")


def check_rule(rule: Rule, design: Design) -> list[dict]:
    findings = []
    for el in _targets(rule, design):
        ctx = _Ctx(design, el)
        base = {"rule_id": rule.id, "title": rule.title, "regime": rule.regime, "ref": rule.ref,
                "quote": rule.quote, "source_url": rule.source_url, "verified": rule.verified,
                "severity": rule.severity, "why_rule": rule.why, "property": rule.check.get("property"),
                "element_id": el.id if el else None, "element_name": el.name if el else design.ship.name,
                "element_type": el.type if el else "ship", "deck": el.deck if el else "",
                "zone": el.zone if el else "", "box": list(el.box) if el and el.box else None,
                "highlight": [ctx.find(r).id for r in rule.target.get("highlight_roles", [])
                              if any(e.props.get("role") == r for e in design.elements)]}
        c = rule.check
        try:
            actual, how = _value(rule, el, ctx)
            if actual is None:
                findings.append({**base, "status": "unknown", "required": c.get("requirement", ""),
                                 "actual": None, "measured": "",
                                 "message": f"Can't check: the design doesn't give '{c.get('property')}' for this "
                                            f"{base['element_type']}. Add it to the model.", "fix": ""})
                continue
            if "fire_class_at_least" in c:
                need, have = c["fire_class_at_least"], actual.strip().upper()
                if have not in FIRE_CLASS_ORDER:
                    raise QuantityError(f"unknown fire class '{actual}'")
                ok = FIRE_CLASS_ORDER.index(have) >= FIRE_CLASS_ORDER.index(need)
                required, msg = f"{need} or better", f"The design has {have}; the rule needs {need} or better." + ("" if ok else " Not met.")
            elif "slope_max" in c:
                ok = _slope(actual) <= _slope(c["slope_max"]) + 1e-12
                required = f"not steeper than {c['slope_max']}"
                msg = f"The slope is {actual}; the rule allows {c['slope_max']} at the steepest." + ("" if ok else " Too steep.")
            elif "equals_expr" in c:
                want = ctx.fill(c["equals_expr"])
                ok = str(actual).strip() == want.strip()
                required, msg = want, f"The plans show {actual}, but the drawing's own text states {want}."
            elif "equals" in c:
                ok = str(actual).lower() == str(c["equals"]).lower()
                required, msg = str(c["equals"]), f"The design has '{actual}'; the rule needs '{c['equals']}'."
            else:
                required = ctx.fill(c["requirement_expr"]) if "requirement_expr" in c else c["requirement"]
                res = check_requirement(required, actual)
                if res.ok is None:
                    raise QuantityError(res.message)
                ok, msg = res.ok, _plain(required, actual, res.ok)
        except (QuantityError, LookupError) as exc:
            findings.append({**base, "status": "unknown", "required": c.get("requirement", ""), "actual": None,
                             "measured": "", "message": f"Can't check: {exc}", "fix": ""})
            continue
        status = "pass" if ok else ("warn" if rule.severity == "advisory" else "fail")
        findings.append({**base, "status": status, "required": required, "actual": actual, "measured": how,
                         "message": msg, "fix": "" if ok else rule.fix})
    return findings


def check_design(design: Design, rules: list[Rule] | None = None) -> dict:
    rules = rules if rules is not None else load_rules()
    findings, applied, skipped = [], [], []
    for r in rules:
        ok, why = applicability(r, design.ship)
        if not ok:
            skipped.append({"rule_id": r.id, "title": r.title, "regime": r.regime, "ref": r.ref, "reasons": why})
            continue
        fs = check_rule(r, design)
        applied.append({"rule_id": r.id, "title": r.title, "regime": r.regime, "ref": r.ref, "reasons": why,
                        "checked": len(fs)})
        findings.extend(fs)
    findings.sort(key=lambda f: (STATUS_ORDER[f["status"]], f["regime"], f["rule_id"], f["element_id"] or ""))
    counts = {s: sum(1 for f in findings if f["status"] == s) for s in STATUS_ORDER}
    return {"ship": design.to_dict()["ship"], "title": design.title, "counts": counts, "findings": findings,
            "rules_applied": applied, "rules_not_applicable": skipped,
            "elements": [{"id": e.id, "type": e.type, "name": e.name, "deck": e.deck, "zone": e.zone,
                          "box": list(e.box) if e.box else None, "props": e.props, "source": e.source,
                          **({"mesh": e.mesh} if e.mesh else {})} for e in design.elements]}
