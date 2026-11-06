"""Accuracy check: run every labelled design and compare findings with the expected answers.

    python scripts/evaluate.py            # all samples in ruletrace/samples
    python scripts/evaluate.py my.json    # your own labelled designs

A finding is (rule, element, status) with status fail / warn / unknown. Passes are not listed.
Precision = reported findings that are correct. Recall = expected findings that were reported.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ruletrace.engine import check_design, load_rules  # noqa: E402
from ruletrace.model import load_design  # noqa: E402


def evaluate(paths: list[Path]) -> dict:
    rules = load_rules()
    tp = fp = fn = 0
    rows = []
    for p in paths:
        d = load_design(p)
        side = p.with_name(p.stem + ".expected.json")
        if side.exists():  # labels for drawings/models that can't carry them inline
            import json
            from datetime import date
            spec = json.loads(side.read_text(encoding="utf-8"))
            for k, v in spec.get("ship_overrides", {}).items():
                setattr(d.ship, k, date.fromisoformat(v) if k == "keel_laid" else v)
            d.expected = spec["expected"]
        got = {(f["rule_id"], f["element_id"], f["status"]) for f in check_design(d, rules)["findings"]
               if f["status"] != "pass"}
        exp = {(x["rule_id"], x.get("element_id"), x["status"]) for x in d.expected}
        tp += len(got & exp)
        fp += len(got - exp)
        fn += len(exp - got)
        rows.append({"design": p.name, "expected": len(exp), "correct": len(got & exp),
                     "false_findings": sorted(got - exp), "missed": sorted(exp - got)})
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    return {"designs": rows, "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": round(prec, 3), "recall": round(rec, 3), "rules": len(rules)}


if __name__ == "__main__":
    samples = ROOT / "ruletrace" / "samples"
    paths = [Path(a) for a in sys.argv[1:]] or sorted(
        [p for p in samples.glob("*.json") if not p.name.endswith(".expected.json")]
        + [p for p in samples.glob("*.pdf") if p.with_name(p.stem + ".expected.json").exists()])
    r = evaluate(paths)
    for row in r["designs"]:
        print(f"{row['design']:40} expected {row['expected']:2}  correct {row['correct']:2}"
              f"  false {len(row['false_findings'])}  missed {len(row['missed'])}")
        for x in row["false_findings"]:
            print("   FALSE  ", x)
        for x in row["missed"]:
            print("   MISSED ", x)
    print(f"\nprecision {r['precision']:.0%}   recall {r['recall']:.0%}   ({r['rules']} rules, {len(paths)} designs)")
    print("Note: the samples are synthetic and were labelled by the team that wrote the rules. "
          "Add designs labelled independently before quoting these numbers.")
