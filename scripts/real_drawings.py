"""How well does RuleTrace read real drawings? Compares what it reads with what each drawing states.

    python scripts/real_drawings.py dana.pdf atlantic.pdf gosars.pdf
    python scripts/real_drawings.py --json out.json  dana.pdf atlantic.pdf gosars.pdf

The stated values were typed in by hand from the public drawings linked in Meyer's challenge brief
(evaluation/real_drawings.json). Each PDF is matched to its entry by file name. The drawings are not
stored in the repository; download them from the links in that file.

For every stated value it reports: read correctly, read wrongly, or not read. A value that is not read
is never guessed, so the rules that need it report "can't check" instead.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ruletrace.engine import check_design, load_rules  # noqa: E402
from ruletrace.ga_import import design_from_ga_pdf, norm_deck  # noqa: E402

TRUTH = ROOT / "evaluation" / "real_drawings.json"
LABELS = {"loa_m": "Length overall", "lbp_m": "Length between perpendiculars", "breadth_m": "Breadth",
          "frame_spacing_m": "Frame spacing", "persons": "Persons on board", "scale": "Drawing scale"}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def match_entry(pdf: Path, entries: list[dict]) -> dict | None:
    k = _key(pdf.stem)
    for e in entries:
        hint = _key(Path(e["file_hint"]).stem)
        if hint in k or k in hint or ("sars" in k and "sars" in hint) or ("dana" in k and "dana" in e["name"].lower()) \
                or ("9281" in k and "9281" in hint):
            return e
    return None


def compare(pdf: Path, entry: dict) -> dict:
    t = time.perf_counter()
    d = design_from_ga_pdf(pdf)
    seconds = time.perf_counter() - t
    it = d.interpretation
    pp = it.get("principal_particulars", {})
    m = re.search(r"title block says 1:(\d+)", it.get("scale", ""))
    read = {"loa_m": pp.get("loa_m"), "lbp_m": pp.get("lbp_m"), "breadth_m": pp.get("breadth_m"),
            "frame_spacing_m": it.get("frame_spacing_m") if "assumed" not in " ".join(it.get("notes", [])) else None,
            "persons": pp.get("accommodation_persons") or None, "scale": int(m.group(1)) if m else None}
    heights = {norm_deck(x["deck"]): x.get("height_m") for x in it.get("decks", [])}
    rows = []
    st = entry["stated"]
    for k, label in LABELS.items():
        if k in st:
            rows.append(_row(label, st[k], read[k]))
    for deck, h in st.get("deck_heights_m", {}).items():
        rows.append(_row(f"Height of {deck.title()}", h, heights.get(norm_deck(deck))))
    cabins = [e for e in d.elements if e.type == "cabin"]
    for deck, n in st.get("cabins", {}).items():
        on = [e for e in cabins if norm_deck(e.deck or "") == norm_deck(deck)]
        for kind, berths in (("single", 1), ("double", 2)):
            got = sum(1 for e in on if int(e.props.get("berths", 0)) == berths) if cabins else None
            rows.append(_row(f"{kind.title()} cabins, {deck.title()}", n.get(kind, 0), got))
    if "hospital" in st:
        rows.append(_row("Hospital shown", "yes" if st["hospital"] else "no",
                         "yes" if any(e.type == "hospital" for e in d.elements) else None))
    t2 = time.perf_counter()
    result = check_design(d, load_rules())
    check_s = time.perf_counter() - t2
    return {"drawing": entry["name"], "file": pdf.name, "style": entry["yard_style"], "read_with": it.get("read_with"),
            "read_seconds": round(seconds, 2), "check_seconds": round(check_s, 3), "scale_note": it.get("scale"),
            "rows": rows, "correct": sum(r["result"] == "correct" for r in rows),
            "wrong": sum(r["result"] == "wrong" for r in rows), "not_read": sum(r["result"] == "not read" for r in rows),
            "rooms_found": {k: v for k, v in it.get("found", {}).items() if v},
            "rule_counts": result["counts"], "notes": it.get("notes", [])}


def _row(label, stated, read) -> dict:
    if read is None:
        res = "not read"
    elif isinstance(stated, (int, float)) and isinstance(read, (int, float)):
        res = "correct" if abs(float(stated) - float(read)) < 0.006 else "wrong"  # drawings give 2 decimals
    else:
        res = "correct" if str(stated) == str(read) else "wrong"
    return {"item": label, "stated": stated, "read": read, "result": res}


def main(argv: list[str]) -> None:
    out = None
    if argv[:1] == ["--json"]:
        out, argv = Path(argv[1]), argv[2:]
    entries = json.loads(TRUTH.read_text(encoding="utf-8"))["drawings"]
    reports = []
    for a in argv:
        pdf = Path(a)
        entry = match_entry(pdf, entries)
        if not entry:
            print(f"{pdf.name}: not one of the drawings in {TRUTH.name}; skipped")
            continue
        r = compare(pdf, entry)
        reports.append(r)
        print(f"\n{r['drawing']}  ({r['read_with']}, read in {r['read_seconds']} s, rules checked in {r['check_seconds']} s)")
        print(f"  {r['correct']} correct, {r['wrong']} wrong, {r['not_read']} not read, of {len(r['rows'])} stated values")
        for row in r["rows"]:
            print(f"    {row['result']:9} {row['item']:42} stated {row['stated']!s:8} read {row['read']!s}")
        print(f"  rooms read: {r['rooms_found'] or 'none (room names are not text on this drawing)'};  rules: {r['rule_counts']}")
    if reports:
        tot = {k: sum(r[k] for r in reports) for k in ("correct", "wrong", "not_read")}
        n = sum(len(r["rows"]) for r in reports)
        print(f"\nAll drawings: {tot['correct']} of {n} stated values read correctly, {tot['wrong']} wrong, {tot['not_read']} not read")
    if out:
        out.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
