"""Measure the local-LLM helpers, so the claims you make to the company are numbers, not adjectives.

    python scripts/evaluate_llm.py                  # all three tests (needs Ollama running)
    python scripts/evaluate_llm.py --no-llm         # retrieval only, works offline
    python scripts/evaluate_llm.py --json out.json  # also save the raw results
    python scripts/evaluate_llm.py --limit 5        # first 5 rules only (quick smoke test)

Three measurements:

  1. RETRIEVAL (no model): for each rule, ask a question built from its title. Is that rule in the top 5
     retrieved? This is what the chatbot can possibly cite.
  2. EXPLANATION FAITHFULNESS: run the engine on every sample design and ask the model to explain each failing
     finding. What share of answers pass the faithfulness checks (numbers, required and actual values,
     paragraph named, no compliance claim) with no fallback to the deterministic text?
  3. RULE-DRAFTING ROUND TRIP: give the model only the verbatim quote of each existing regulation rule
     (not the drawing-consistency rules, which quote nothing) and ask it to draft the rule. How close is the draft to the rule your team wrote by hand? (target type, property,
     check kind, requirement number and direction)

Be honest when you present these: round trip uses quotes from your own rule pack, so it measures how well the
model can restate a requirement, not how well it handles a regulation it has never seen. Say that.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ruletrace import llm_assist as la  # noqa: E402
from ruletrace.engine import check_design, load_rules  # noqa: E402
from ruletrace.model import load_design  # noqa: E402


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "n/a"


def retrieval(rules: list[dict]) -> dict:
    hit = 0
    misses = []
    for r in rules:
        top = [x["id"] for x in la.retrieve_rules(f"What does the rule on {r['title']} require?", rules, k=5)]
        if r["id"] in top:
            hit += 1
        else:
            misses.append(r["id"])
    return {"recall_at_5": pct(hit, len(rules)), "misses": misses}


def explanations(client, limit_per_design: int) -> dict:
    ok = total = 0
    reasons: dict[str, int] = {}
    samples = [p for p in sorted((ROOT / "ruletrace" / "samples").glob("*.json")) if not p.stem.endswith(".expected")]
    for p in samples:
        findings = check_design(load_design(p))["findings"]
        for item in la.explain_findings(client, findings, limit=limit_per_design):
            total += 1
            if item["source"] != "rules":
                ok += 1
            else:
                reasons[item.get("note", "?")] = reasons.get(item.get("note", "?"), 0) + 1
    return {"accepted_without_fallback": pct(ok, total), "fallback_reasons": reasons}


def _req_dir_num(req: str):
    m = re.match(r"\s*(at least|at most|exactly)\s+([\d.,]+)", str(req))
    return (m.group(1), float(m.group(2).replace(",", "."))) if m else None


def round_trip(client, rules: list[dict], limit: int | None) -> dict:
    vocab = la.rule_vocabulary(rules)
    # Only rules that quote a regulation and test one element; drawing-consistency rules have no quote to restate.
    rules = [r for r in rules if r.get("quote") and (r.get("target") or {}).get("type") not in (None, "ship")]
    rows, counts = [], dict(declined=0, produced=0, quote_ok=0, type=0, prop=0, kind=0, req=0, req_total=0)
    for r in (rules[:limit] if limit else rules):
        row = {"rule_id": r["id"]}
        try:
            out = la.draft_rule(client, r["quote"], regime=r["regime"], ref=r["ref"], vocabulary=vocab, existing_ids=set())
        except la.LLMError as exc:
            row["error"] = str(exc)
            rows.append(row)
            continue
        good = [d for d in out["drafts"] if d["ok"]]
        if not out["drafts"]:
            row["declined"] = out["unclear"] or "no rule drafted"   # the model chose not to draft: counted separately
            counts["declined"] += 1
            rows.append(row)
            continue
        if not good:
            row["problems"] = [p for d in out["drafts"] for p in d["problems"]] or [out["unclear"] or "no rule drafted"]
            rows.append(row)
            continue
        d = good[0]["rule"]
        counts["produced"] += 1
        counts["quote_ok"] += 1  # draft_rule only returns ok drafts with a verbatim quote
        gt, gc = r["target"], r["check"]
        counts["type"] += d["target"].get("type") == gt.get("type")
        kinds = lambda c: sorted(set(c) - {"property"})  # noqa: E731
        counts["kind"] += kinds(d["check"]) == kinds(gc)
        counts["prop"] += d["check"].get("property") == gc.get("property")
        if "requirement" in gc:
            counts["req_total"] += 1
            counts["req"] += _req_dir_num(d["check"].get("requirement", "")) == _req_dir_num(gc["requirement"])
        rows.append({"rule_id": r["id"], "draft_check": d["check"], "gold_check": gc})
    n = len(rules[:limit] if limit else rules)
    return {"rules_tried": n, "usable_draft": pct(counts["produced"], n),
            "declined_by_model": pct(counts["declined"], n),
            "rejected_by_checks": pct(n - counts["produced"] - counts["declined"], n),
            "target_type_matches_gold": pct(counts["type"], counts["produced"]),
            "property_matches_gold": pct(counts["prop"], counts["produced"]),
            "check_kind_matches_gold": pct(counts["kind"], counts["produced"]),
            "requirement_value_and_direction_match": pct(counts["req"], counts["req_total"]), "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", choices=["explain", "draft"], help="run just one model test (faster to iterate)")
    a = ap.parse_args()
    rules = [r.__dict__ for r in load_rules()]
    report = {"rules_in_pack": len(rules), "retrieval": retrieval(rules)}
    if not a.no_llm:
        client = la.OllamaClient()
        st = client.status()
        print("model:", client.name, "-", st["message"])
        if not (st["reachable"] and st["model_pulled"]):
            sys.exit("Ollama is not ready; fix that or use --no-llm")
        t = time.time()
        report["model"] = client.name
        if a.only in (None, "explain"):
            report["explanations"] = explanations(client, limit_per_design=a.limit or 6)
        if a.only in (None, "draft"):
            report["rule_drafting_round_trip"] = round_trip(client, rules, a.limit)
        report["seconds"] = round(time.time() - t)
    print(json.dumps({k: v for k, v in report.items() if k != "rule_drafting_round_trip"}, indent=2))
    if "rule_drafting_round_trip" in report:
        rt = {k: v for k, v in report["rule_drafting_round_trip"].items() if k != "rows"}
        print(json.dumps({"rule_drafting_round_trip": rt}, indent=2))
        for row in report["rule_drafting_round_trip"]["rows"]:      # why drafts were rejected, or what differed
            if "declined" in row:
                print("DECLINED", row["rule_id"], "->", row["declined"])
            elif "problems" in row or "error" in row:
                print("REJECTED", row["rule_id"], "->", row.get("problems") or row.get("error"))
            elif row["draft_check"] != row["gold_check"]:
                print("DIFFERS ", row["rule_id"], "\n   draft:", row["draft_check"], "\n   gold: ", row["gold_check"])
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("saved", a.json)


if __name__ == "__main__":
    main()
