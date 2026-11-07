"""Shared design-check helper: apply ship-profile overrides to a stored design and run the rule engine.

Used by the check endpoint (api.py) and by the AI actions (llm_actions.py), so a "what-if" or a proposed fix
is checked in exactly the same way as a normal check.
"""

from __future__ import annotations

import copy
from datetime import date

from .engine import check_design, load_rules
from .model import design_from_dict

# Ship-profile fields an engineer may change for a what-if check.
ALLOWED_OVERRIDES = {"operating_areas", "keel_laid", "passengers", "ship_type", "length_m", "breadth_m",
                     "gross_tonnage", "flag"}
SHIP_TYPES = {"passenger", "cargo", "special_purpose", "research", "ro_ro_passenger"}
# Operating areas the rule pack and the viewer know about.
KNOWN_AREAS = {"us_ports", "north_american_eca", "us_caribbean_eca", "baltic_sea", "north_sea", "mediterranean",
               "canadian_arctic"}


class OverrideError(ValueError):
    pass


def known_areas(rules=None) -> set[str]:
    """Areas used by the viewer plus any area a rule in the pack refers to."""
    rules = rules if rules is not None else load_rules()
    return KNOWN_AREAS | {a for r in rules for a in (r.applies_if or {}).get("operating_areas_any", [])}


def check_record(rec: dict, overrides: dict | None = None, rules=None) -> tuple[object, list, dict]:
    """Run the rules on a stored design record (as saved by api.py), optionally with ship overrides.

    Raises OverrideError for override keys that are not allowed; DesignError / RuleError as the engine does.
    Does not modify ``rec``.
    """
    overrides = overrides or {}
    bad = set(overrides) - ALLOWED_OVERRIDES
    if bad:
        raise OverrideError(f"cannot override {sorted(bad)}")
    d = copy.deepcopy(rec["design"])
    d["ship"] = {**d["ship"], **overrides}
    design = design_from_dict(d, source_file=rec["filename"])
    rs = rules if rules is not None else load_rules()
    return design, rs, check_design(design, rs)


def fail_set(result: dict) -> set[tuple[str, str | None]]:
    return {(f["rule_id"], f["element_id"]) for f in result["findings"] if f["status"] == "fail"}


def status_map(result: dict) -> dict[tuple[str, str | None], str]:
    return {(f["rule_id"], f["element_id"]): f["status"] for f in result["findings"]}


def diff_results(before: dict, after: dict) -> dict:
    """What changed between two checks of the same design: findings and which rules apply."""
    b, a = status_map(before), status_map(after)
    applied_b = {r["rule_id"] for r in before["rules_applied"]}
    applied_a = {r["rule_id"] for r in after["rules_applied"]}

    def rows(keys, src):
        out = []
        for k in sorted(keys, key=lambda k: (k[0], k[1] or "")):
            f = next(x for x in src["findings"] if (x["rule_id"], x["element_id"]) == k)
            out.append({"rule_id": k[0], "element_id": k[1], "title": f["title"], "regime": f["regime"],
                        "ref": f["ref"], "element_name": f["element_name"], "status": f["status"],
                        "message": f["message"]})
        return out

    return {
        "newly_failing": rows({k for k, s in a.items() if s == "fail" and b.get(k) != "fail"}, after),
        "newly_passing": rows({k for k, s in a.items() if s == "pass" and b.get(k) in ("fail", "warn", "unknown")}, after),
        "rules_switched_on": sorted(applied_a - applied_b),
        "rules_switched_off": sorted(applied_b - applied_a),
        "counts_before": before["counts"], "counts_after": after["counts"],
    }


def iso_date(value) -> str:
    """'2027-03-01' -> same; raises OverrideError for anything else."""
    try:
        d = date.fromisoformat(str(value))
    except ValueError as exc:
        raise OverrideError(f"keel_laid must be an ISO date (YYYY-MM-DD), got '{value}'") from exc
    if not 1990 <= d.year <= 2100:
        raise OverrideError(f"keel_laid year {d.year} is outside 1990-2100")
    return d.isoformat()
