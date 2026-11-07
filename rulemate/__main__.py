"""Command line: python -m rulemate <command>"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .config import CLAUSES_FILE, RULES_DIR


def cmd_ingest(args) -> None:
    from .ingest import ingest_dir

    res = ingest_dir(Path(args.rules_dir))
    if not res.documents:
        print(f"No rule documents found in {args.rules_dir}")
        return
    print(f"{'document':40} {'style':9} {'clauses':>7} {'tables':>6} {'refs':>5} {'unres.':>6}  synthetic")
    for d in res.documents:
        cl = [c for c in res.clauses if c.doc == d.doc]
        refs = sum(len(c.refs) for c in cl)
        unres = sum(len(c.unresolved_refs) for c in cl)
        flag = "YES - dev only" if d.synthetic else ""
        print(f"{d.doc[:40]:40} {d.style:9} {d.n_clauses:>7} {d.n_tables:>6} {refs:>5} {unres:>6}  {flag}")
    print(f"\n{len(res.clauses)} clauses written to {CLAUSES_FILE}")


def _load():
    from .ingest import load_index

    if not CLAUSES_FILE.exists():
        sys.exit("No index yet. Run: python -m rulemate ingest")
    return load_index()


def cmd_show(args) -> None:
    from .lookup import find_clause

    res = _load()
    hits = find_clause(res.clauses, args.clause_id, args.doc)
    if not hits:
        print(f"No clause matching '{args.clause_id}'.")
        return
    for c in hits:
        print("=" * 80)
        if c.synthetic:
            print("[SYNTHETIC SAMPLE - not a real rule]")
        print(c.display())
        if c.path:
            print(f"  path: {' > '.join(c.path)}")
        if c.refs:
            print(f"  refers to: {', '.join(c.refs)}")
        if c.unresolved_refs:
            print(f"  unresolved refs: {', '.join(c.unresolved_refs)}")


def cmd_list(args) -> None:
    res = _load()
    for c in res.clauses:
        if args.doc and c.doc != args.doc:
            continue
        preview = c.text.replace("\n", " ")[:70]
        print(f"{c.clause_id:28} {c.kind:8} p.{c.page or '-':<3} {c.title[:30]:30} | {preview}")
    print(f"\n{Counter(c.kind for c in res.clauses)}")


def cmd_index(args) -> None:
    import time

    from .retrieval import Retriever

    _load()
    t = time.perf_counter()
    r = Retriever(_load().clauses, use_dense=True)
    print(f"mode={r.mode}  searchable={len(r.searchable)}  built in {time.perf_counter() - t:.1f}s")


def cmd_search(args) -> None:
    import time

    from .retrieval import get_retriever

    _load()
    r = get_retriever(use_dense=not args.bm25)
    t = time.perf_counter()
    res = r.search(args.query, k=args.k, follow_refs=not args.no_xref)
    ms = (time.perf_counter() - t) * 1000
    verdict = {"high": "RELEVANT", "low": "WEAK MATCH (LLM must confirm or say not found)",
               "none": "NOTHING RELEVANT -> 'Not found in the provided rules'"}[res.confidence]
    print(f"mode={res.mode}  top_score={res.top_score}  confidence={res.confidence}: {verdict}  ({ms:.0f} ms)")
    if res.id_queries:
        print(f"clause IDs in query: {res.id_queries}")
    print(f"{'source':7} {'score':>6} {'cos':>6} {'bm25':>4} {'dense':>5}  clause")
    for h in res.hits:
        via = f"  (via {h.via})" if h.via else ""
        cos = f"{h.cosine:.3f}" if h.cosine is not None else "-"
        bmr = "-" if h.bm25_rank is None else h.bm25_rank + 1
        dr = "-" if h.dense_rank is None else h.dense_rank + 1
        print(f"{h.source:7} {h.score:6.3f} {cos:>6} {bmr!s:>4} {dr!s:>5}  {h.clause.clause_id}{via}")
        if args.text:
            print("        " + h.clause.text.replace("\n", "\n        ")[:400])


def cmd_ask(args) -> None:
    from .answer import answer_question
    from .llm import get_llm
    from .retrieval import get_retriever

    _load()
    ans = answer_question(args.question, retriever=get_retriever(), llm=get_llm(args.provider), use_cache=not args.no_cache,
                          docs=args.rules.split(",") if args.rules else None)
    badge = {"verified": "VERIFIED", "excerpts": "EXCERPTS ONLY", "not_found": "NOT FOUND"}[ans.status]
    extra = " (cached)" if ans.cached else ""
    print(f"[{badge}]{extra}  confidence={ans.confidence} top_score={ans.top_score}  "
          f"attempts={ans.attempts} rejected={ans.rejected_attempts}  {ans.model}  {ans.latency_ms:.0f} ms")
    if ans.synthetic:
        print("[SYNTHETIC SAMPLE DATA - not a real rule]")
    print("-" * 80)
    print(ans.text)
    print("-" * 80)
    if ans.citations:
        print("cited: " + ", ".join(f"{c.clause_id} (p.{c.page})" for c in ans.citations))
    if ans.note:
        print(f"note: {ans.note}")
    if ans.violations and args.verbose:
        print("guardrail violations on last rejected draft:")
        for v in ans.violations:
            print(f"  - {v}")


def cmd_limits(args) -> None:
    from .compliance.service import get_rule_table, resolve_method

    _load()
    t = get_rule_table(method=args.method, provider=args.provider, force=args.force)
    print(f"method={resolve_method(args.method, args.provider)}  model={t.model}  limits={len(t.limits)}  rejected proposals={len(t.rejected)}")
    for l in t.limits:
        extra = []
        if l.qualifier:
            extra.append(f"only: {l.qualifier}")
        if l.condition:
            extra.append(f"if: {l.condition[:50]}")
        if l.notes:
            extra.append("; ".join(l.notes))
        print(f"  {l.clause_id:18} {l.describe():14} {l.parameter[:40]:40} {l.method:5} {' | '.join(extra)}")
    if args.verbose:
        for r in t.rejected:
            print(f"  rejected {r['clause']}: {r['reason']}")


def cmd_picture(args) -> None:
    import os

    from .compliance.service import check_rows
    from .design.picture import read_picture, to_rows
    from .llm import make_provider

    llm = make_provider("gemini")
    llm.model = os.getenv("GEMINI_VISION_MODEL", llm.model)
    llm.timeout = max(llm.timeout, 240)
    path = Path(args.image)
    values, _ = read_picture(path.read_bytes(), llm)
    print(f"Gemini ({llm.model}) read {len(values)} values; code confirmed {sum(v.confirmed for v in values)}:")
    for v in values:
        print(f"  {'OK ' if v.confirmed else '-- '} {v.space[:30]:30} {v.parameter[:34]:34} {v.value_text:>10}  {v.evidence}")
    rows = to_rows(values, path.name)
    colour = {"PASS": "\033[32m", "FAIL": "\033[31m", "NEEDS REVIEW": "\033[33m"}
    print()
    for r in check_rows(rows, docs=args.rules.split(",") if args.rules else None):
        print(f"{colour[r.verdict]}{r.verdict:12}\033[0m {r.row.parameter[:34]:34} {r.row.display:>12}")
        for line in r.reason.splitlines()[1:3]:
            print(f"{'':13}{line}")


def cmd_design(args) -> None:
    import json

    from .design.extract import extract, facts_to_csv

    pages = None
    if args.pages:
        pages = []
        for part in args.pages.split(","):
            lo, _, hi = part.partition("-")
            pages += list(range(int(lo), int(hi or lo) + 1))
    src = Path(args.pdf)
    context = [(Path(c.split("=", 1)[0]), c.split("=", 1)[1] if "=" in c else "") for c in (args.with_ or [])]
    vision_llm = None
    if args.gemini:
        import os

        from .llm import make_provider

        os.environ.setdefault("GEMINI_TIMEOUT", "180")
        vision_llm = make_provider("gemini")
        vision_llm.model = os.getenv("GEMINI_VISION_MODEL", vision_llm.model)
        print(f"Gemini reads each GA sheet ({vision_llm.model}); code confirms every reading")
    ex = extract(src, pages=pages, doc_label=args.label, dpi=args.dpi, context=context, vision_llm=vision_llm,
                 progress=lambda p: print(f"  reading page {p} ...", flush=True))
    for d in ex.drawings:
        print(f"GA p.{d['page']} Dwg {d['drawing'] or '?'} {d['title']}: {d['rooms']} berth labels, {d['berths']} berths")
    for f in ex.facts:
        mark = "" if f.checkable else "  (context)"
        print(f"  {f.item_id:4} {f.parameter:34} {f.value:g} {f.unit:8} p.{f.source_page}{mark}")
    print("\nFindings for an engineer:")
    for x in ex.findings:
        print(f"  - {x}")
    if args.out:
        out = Path(args.out)
        out.write_text(facts_to_csv(ex.facts))
        out.with_suffix(".findings.json").write_text(json.dumps(
            {"document": args.label or src.stem, "file": ex.file, "drawings": [{k: v for k, v in d.items() if k != "words"}
                                                                             for d in ex.drawings],
             "facts": [f.__dict__ for f in ex.facts], "findings": ex.findings}, indent=1))
        print(f"\nwrote {out} (checkable values) and {out.with_suffix('.findings.json')}")
        print(f"next: python -m rulemate check {out} --rules 46CFR190")


def cmd_check(args) -> None:
    from .compliance.check import read_design_csv, results_to_csv
    from .compliance.service import check_rows

    _load()
    rows = read_design_csv(Path(args.csv).read_bytes())
    docs = args.rules.split(",") if args.rules else None
    results = check_rows(rows, explain=args.explain, provider=args.provider, docs=docs)
    colour = {"PASS": "\033[32m", "FAIL": "\033[31m", "NEEDS REVIEW": "\033[33m"}
    for r in results:
        print(f"{colour[r.verdict]}{r.verdict:12}\033[0m {r.row.item_id:5} {r.row.parameter[:28]:28} "
              f"{r.row.display:>14} {r.row.space[:40]}")
        if r.row.source:
            print(f"{'':19}design value from: {r.row.source}")
        for line in r.reason.splitlines():
            print(f"{'':19}{line}")
        if r.explanation:
            print(f"{'':19}LLM ({r.explanation_status}): {r.explanation}")
    counts = {v: sum(r.verdict == v for r in results) for v in ("PASS", "FAIL", "NEEDS REVIEW")}
    print(f"\n{counts}")
    if args.out:
        Path(args.out).write_text(results_to_csv(results))
    if args.baseline:
        from .compliance.check import diff_results

        before = check_rows(read_design_csv(Path(args.baseline).read_bytes()), explain=False, provider=args.provider,
                            docs=docs)
        changes = diff_results(before, results)
        print(f"\nChange impact vs {args.baseline}: {len(changes)} item(s) changed")
        for c in changes:
            v = (c.after or c.before).verdict
            print(f"  {colour[v]}{c.item_id:5}\033[0m {c.summary}")
        print(f"written {args.out}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rulemate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="parse rule documents into clauses")
    p.add_argument("--rules-dir", default=str(RULES_DIR))
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("show", help="show one clause by ID, e.g. 'II-2/13.3.1' or '13.3.1'")
    p.add_argument("clause_id")
    p.add_argument("--doc")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("list", help="list all parsed clauses")
    p.add_argument("--doc")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("index", help="build / refresh the embedding cache")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="hybrid search over clauses")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--text", action="store_true", help="print clause text")
    p.add_argument("--no-xref", action="store_true", help="do not follow cross-references")
    p.add_argument("--bm25", action="store_true", help="BM25 + ID lookup only (no embeddings)")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("ask", help="answer a question with verified citations")
    p.add_argument("question")
    p.add_argument("--provider", help="e.g. groq | openai | ollama | groq,ollama (default: LLM_PROVIDER)")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--rules", help="only these rule documents, comma-separated (default: all), e.g. 46CFR190")
    p.add_argument("-v", "--verbose", action="store_true", help="show guardrail violations")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("limits", help="extract / show the rule table of numeric limits")
    p.add_argument("--method", choices=["auto", "llm", "regex"], default="auto")
    p.add_argument("--provider")
    p.add_argument("--force", action="store_true", help="re-extract everything")
    p.add_argument("-v", "--verbose", action="store_true", help="show rejected proposals")
    p.set_defaults(fn=cmd_limits)

    p = sub.add_parser("picture", help="check a picture of a layout (Gemini reads the values, code confirms and checks)")
    p.add_argument("image")
    p.add_argument("--rules", default="36CFR1191AppD", help="rule documents, comma-separated")
    p.set_defaults(fn=cmd_picture)

    p = sub.add_parser("design", help="read design values from a design document (GA drawings by OCR, report text)")
    p.add_argument("pdf")
    p.add_argument("--pages", help="pages to read as drawings, e.g. 41-43 (default: GA / scanned pages)")
    p.add_argument("--label", help="document name used in the evidence")
    p.add_argument("--with", dest="with_", action="append", metavar="PDF[=LABEL]",
                   help="another document of the same ship (report, particulars) to read stated facts from")
    p.add_argument("--dpi", type=int, default=200, help="OCR resolution; 300 for half-size CAD sheets")
    p.add_argument("--gemini", action="store_true",
                   help="also let Gemini read each GA sheet (GEMINI_API_KEY); readings are confirmed in code")
    p.add_argument("--out", help="write the checkable values as a design CSV (+ .findings.json)")
    p.set_defaults(fn=cmd_design)

    p = sub.add_parser("check", help="check a design CSV against the rule table")
    p.add_argument("csv")
    p.add_argument("--explain", action="store_true", help="add LLM explanations (guardrailed)")
    p.add_argument("--provider")
    p.add_argument("--out", help="write results CSV")
    p.add_argument("--rules", help="rule documents that apply, comma-separated (default: all), e.g. 46CFR190")
    p.add_argument("--baseline", help="earlier design CSV: also report which items changed verdict")
    p.set_defaults(fn=cmd_check)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
