"""Compliance check: design values vs. the rule table. Code decides PASS / FAIL / NEEDS REVIEW.

For each design row:
1. candidates  - limits whose unit has the same dimension as the row's unit (pint)
2. match       - semantic similarity between the row's parameter and the limit's parameter + source sentence;
                 keep the best match and any limit within MARGIN of it
3. applicable  - a limit with a qualifier ("crew accommodation", "cabin doors") applies only if the row
                 mentions it; a matched qualified, unconditional limit supersedes a general one in the same direction
4. compare     - value converted to the limit's unit, compared with the comparator
5. verdict     - all unconditional limits pass and no conditional one fails      -> PASS
                 an unconditional limit fails, no conditional relaxation passes  -> FAIL
                 a relaxation passes only under a condition / only conditional limits / nothing matched -> NEEDS REVIEW
The LLM only writes the explanation afterwards; it is checked by the same guardrails and may not contradict the verdict.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from ..config import ROOT, CACHE_DIR
from ..guardrails import check_answer
from ..llm import LLMError
from ..models import Clause
from ..retrieval import tokenize
from .limits import Limit, RuleTable
from .units import UnitError, compatible, convert, parse_number, parse_value

Verdict = Literal["PASS", "FAIL", "NEEDS REVIEW"]
MIN_SIM = 0.70  # below this, no limit is considered to cover the parameter
MARGIN = 0.02  # limits within this similarity of the best match are evaluated together
EXPLAIN_CACHE = CACHE_DIR / "explanations.json"
ABBREVIATIONS = {"mvz": "main vertical zone", "lll": "low-location lighting", "fd": "fire door"}
# what designers write -> the words the rules use; applied before matching a design value to a limit
DESIGNER_TERMS = {
    "desk": "work surface", "vanity": "lavatory counter", "wc": "water closet", "toilet": "water closet",
    "bunk": "berth", "bed": "berth", "cabin": "stateroom", "passage": "walking surface", "aisle": "walking surface",
    "turning circle": "turning space", "door width": "door clear width", "bath": "toilet room",
    "counter": "work surface", "table": "dining surface",
}


def designer_to_rule_terms(text: str) -> str:
    """'desk height' -> 'desk (work surface) height': keeps the designer's word, adds the rule's."""
    for k, v in DESIGNER_TERMS.items():
        text = re.sub(rf"\b{re.escape(k)}\b(?! \()", lambda m, v=v: f"{m.group(0)} ({v})", text, flags=re.I)
    return text
_GENERIC = {"space", "area", "room", "item", "general", "other"}


class DesignRow(BaseModel):
    item_id: str = ""
    space: str = ""
    parameter: str
    value: float | None = None
    unit: str = ""
    notes: str = ""
    raw_value: str = ""
    source: str = ""  # where the design value comes from: drawing / sheet / model element, measured or stated
    source_file: str = ""  # document the value was read from (relative to the project), for showing the evidence
    source_page: int | None = None
    source_bbox: str = ""  # "x0,y0,x1,y1" in PDF points on that page

    @property
    def display(self) -> str:
        """Value as the designer wrote it, plus the parsed value when they differ: 3'-6" (= 42 in)."""
        raw = self.raw_value or (f"{self.value:g}" if self.value is not None else "")
        if self.value is not None and parse_number(raw) is None:
            if re.fullmatch(r"[\d.,\s/]+\s*(mm|cm|m|in|ft|inch(es)?|feet|\"|\u2033)", raw, re.I):
                return raw  # "1500 mm", "30\"": already says it
            return f"{raw} (= {self.value:g} {self.unit})"
        return f"{raw} {self.unit}".strip()

    @property
    def text(self) -> str:
        t = f"{self.space} {self.parameter} {self.notes}"
        for k, v in ABBREVIATIONS.items():
            t = re.sub(rf"\b{k}\b", v, t, flags=re.I)
        return t


class LimitResult(BaseModel):
    limit: Limit
    similarity: float
    value_in_limit_unit: float | None = None
    outcome: Literal["pass", "fail"] | None = None
    role: Literal["applied", "conditional", "superseded", "not applicable"] = "applied"


class RowResult(BaseModel):
    row: DesignRow
    verdict: Verdict
    reason: str  # code-generated, with citations
    limits: list[LimitResult] = Field(default_factory=list)
    explanation: str = ""
    explanation_status: Literal["verified", "code", "rejected"] = "code"
    explanation_violations: list[str] = Field(default_factory=list)
    synthetic: bool = False

    @property
    def applied(self) -> list[LimitResult]:
        return [l for l in self.limits if l.role in ("applied", "conditional")]


# ------------------------------------------------------------------ input


_COLS = {
    "item_id": ["item_id", "id", "item", "ref"],
    "space": ["space", "location", "name", "room", "area"],
    "parameter": ["parameter", "param", "quantity", "property"],
    "value": ["value", "val", "design_value"],
    "unit": ["unit", "units", "uom"],
    "notes": ["notes", "note", "comment", "comments", "remarks"],
    "source": ["source", "design_source", "drawing", "reference", "measured_from", "provenance"],
    "source_file": ["source_file"],
    "source_page": ["source_page"],
    "source_bbox": ["source_bbox"],
}


def read_design_csv(data: str | bytes) -> list[DesignRow]:
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(data))
    fields = {(f or "").strip().lower(): f for f in reader.fieldnames or []}
    colmap = {k: next((fields[a] for a in al if a in fields), None) for k, al in _COLS.items()}
    if not colmap["parameter"] or not colmap["value"]:
        raise ValueError("CSV needs at least 'parameter' and 'value' columns (and preferably 'unit', 'space')")
    rows = []
    for i, r in enumerate(reader, start=1):
        get = lambda k: (r.get(colmap[k]) or "").strip() if colmap[k] else ""  # noqa: E731
        raw = get("value")
        value, unit = parse_value(raw, get("unit"))  # also US feet-inch notation: 3'-6" -> 42 in
        rows.append(DesignRow(item_id=get("item_id") or f"R{i}", space=get("space"), parameter=get("parameter"),
                              value=value, unit=unit, notes=get("notes"), raw_value=raw, source=get("source"),
                              source_file=get("source_file"),
                              source_page=int(get("source_page")) if get("source_page").isdigit() else None,
                              source_bbox=get("source_bbox")))
    return rows


# ------------------------------------------------------------------ matching


class Matcher:
    def __init__(self, limits: list[Limit], encoder=None):
        self.limits = limits
        self.encoder = encoder
        self.texts = [f"{l.parameter}. {l.subject}. {l.context}" for l in limits]
        self.emb = self._encode(self.texts) if (encoder and limits) else None

    def _encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.encoder.encode(texts, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)

    def similarities(self, row: DesignRow) -> np.ndarray:
        query = f"{row.parameter} ({row.space})" if row.space else row.parameter
        query = designer_to_rule_terms(query)
        if self.emb is not None:
            return self.emb @ self._encode([query])[0]
        # lexical fallback: share of the row's parameter words found in the limit text
        q = set(tokenize(row.parameter))
        return np.array([len(q & set(tokenize(t))) / max(1, len(q)) for t in self.texts], dtype=np.float32)


def _qualifier_tokens(l: Limit, row: DesignRow | None = None) -> set[str]:
    """Words that narrow where the limit applies, e.g. 'cabin' in 'cabin doors' for a door-width row."""
    if not l.qualifier:
        return set()
    toks = set(tokenize(l.qualifier)) - _GENERIC
    if row is not None:
        toks -= set(tokenize(row.parameter))
    return toks


def _mentions(text: str, clause_id: str) -> bool:
    """Does ``text`` refer to ``clause_id`` the way rule text does ("in 3.3.1", "[2.1.3]", "13.3.3.1")?"""
    local = clause_id.split("/")[-1]
    bracket = re.search(r"\[([\d.]+)\]", clause_id)
    forms = {clause_id, local} | ({bracket.group(1)} if bracket else set())
    parts = local.split(".")
    forms |= {".".join(parts[k:]) for k in range(1, len(parts) - 1)}  # 13.3.3.1 -> 3.3.1, 3.1
    return any(re.search(rf"(?<![\w.]){re.escape(f)}(?![\w.]|\.\d)", text) for f in forms if f.count(".") >= 1)


def _direction(cmp: str) -> str:
    return "min" if cmp in (">=", ">") else "max"


def _compare(v: float, cmp: str, limit: float) -> bool:
    eps = 1e-9 * max(1.0, abs(limit))
    return {">=": v >= limit - eps, ">": v > limit + eps, "<=": v <= limit + eps, "<": v < limit - eps}[cmp]


def evaluate_row(row: DesignRow, matcher: Matcher) -> RowResult:
    def review(reason: str, limits=None) -> RowResult:
        return RowResult(row=row, verdict="NEEDS REVIEW", reason=reason, limits=limits or [])

    if row.value is None:
        return review(f"Design value '{row.raw_value}' is not a number.")
    cands = [i for i, l in enumerate(matcher.limits) if compatible(row.unit, l.unit)]
    if not cands:
        return review(f"No limit in the rules is stated in a unit compatible with '{row.unit}'."
                      if row.unit else "No unit given for the design value.")

    sims = matcher.similarities(row)
    best = max(float(sims[i]) for i in cands)
    if best < MIN_SIM:
        return review(f"No limit in the rules matches '{row.parameter}' (best similarity {best:.2f}).")
    group = [i for i in cands if float(sims[i]) >= best - MARGIN]
    # keep limits that state the same parameter in the same clause together (min and max of one quantity)
    keys = {(matcher.limits[i].clause_uid, matcher.limits[i].parameter.lower()) for i in group}
    group += [i for i in cands if i not in group and (matcher.limits[i].clause_uid, matcher.limits[i].parameter.lower()) in keys]
    # a limit whose own sentence refers to a matched clause modifies it ("the travel distance in 3.3.1 may be increased to 50 m")
    matched_ids = {(matcher.limits[i].doc, matcher.limits[i].clause_id) for i in group}
    group += [
        i for i in cands
        if i not in group
        and any((l.doc, ref) in matched_ids and _mentions(l.context, ref) for l in [matcher.limits[i]] for ref in l.refs)
    ]

    row_tokens = set(tokenize(row.text))
    results: list[LimitResult] = []
    for i in group:
        l = matcher.limits[i]
        q = _qualifier_tokens(l, row)
        role = "applied" if (not q or q & row_tokens) else "not applicable"
        if role == "applied" and l.condition:
            role = "conditional"
        lr = LimitResult(limit=l, similarity=round(float(sims[i]), 3), role=role)
        if role != "not applicable":
            try:
                lr.value_in_limit_unit = convert(row.value, row.unit, l.unit)
            except UnitError:
                lr.role = "not applicable"
            else:
                lr.outcome = "pass" if _compare(lr.value_in_limit_unit, l.comparator, l.value) else "fail"
        results.append(lr)

    # a matched qualified, unconditional limit supersedes general limits in the same direction
    specific_dirs = {_direction(r.limit.comparator) for r in results if r.role == "applied" and _qualifier_tokens(r.limit, row)}
    for r in results:
        if r.role in ("applied", "conditional") and not _qualifier_tokens(r.limit, row) and _direction(r.limit.comparator) in specific_dirs:
            r.role = "superseded"

    uncond = [r for r in results if r.role == "applied"]
    cond = [r for r in results if r.role == "conditional"]
    if not uncond and not cond:
        return review("Limits exist for this parameter, but none applies to this kind of space or item.", results)

    failed = [r for r in uncond if r.outcome == "fail"]
    if not uncond:
        verdict: Verdict = "NEEDS REVIEW"
        why = "Only conditional limits apply; confirm the condition."
    elif not failed:
        cond_fail = [r for r in cond if r.outcome == "fail"]
        verdict = "NEEDS REVIEW" if cond_fail else "PASS"
        why = "Meets every applicable limit." if not cond_fail else "Meets the general limit, but fails a conditional limit; confirm whether the condition applies."
    else:
        relax = [r for r in cond if r.outcome == "pass" and _direction(r.limit.comparator) in {_direction(f.limit.comparator) for f in failed}]
        verdict = "NEEDS REVIEW" if relax else "FAIL"
        why = ("Fails the general limit, but meets a relaxed limit that applies only under a condition; confirm the condition."
               if relax else "Does not meet an applicable limit.")
    lines = [why] + [_limit_line(row, r) for r in results if r.role in ("applied", "conditional")]
    res = RowResult(row=row, verdict=verdict, reason="\n".join(lines), limits=results)
    res.synthetic = any(r.limit.synthetic for r in res.applied)
    return res


def _limit_line(row: DesignRow, r: LimitResult) -> str:
    l = r.limit
    conv = f" (= {r.value_in_limit_unit:g} {l.unit})" if r.value_in_limit_unit is not None and row.unit != l.unit else ""
    mark = "meets" if r.outcome == "pass" else "does not meet"
    cond = f", only if: {l.condition}" if l.condition else ""
    v = r.value_in_limit_unit
    edge = (" - exactly at the limit, no margin" if r.outcome == "pass" and v is not None and l.value
            and abs(v - l.value) <= 1e-3 * abs(l.value) else "")
    return f"- {row.value:g} {row.unit}{conv} {mark} {l.describe()}{edge}{cond} {l.citation}"


# ------------------------------------------------------------------ explanation (LLM, guardrailed)

EXPLAIN_PROMPT = """You explain the result of an automated compliance check of a ship design value. \
The verdict is already decided by code and is final: do not change it, question it, or recompute it.

Write 1 to 3 short sentences. Every sentence must cite the rule clause it relies on in the exact form [doc § clause] \
copied from the clause header. Copy limit values and units exactly as in the clause. {condition_rule}\
Do not use words like "probably", "typically" or "usually".

Design item: {item}
Design value: {value}
Verdict (final): {verdict}
Reason (from code): {reason}

Rule clauses:
{clauses}"""

_POS = re.compile(r"(?<!not )(?<!n't )\b(complies|is compliant|passes|meets (?:the|all|every))\b", re.I)
_NEG = re.compile(r"\b(fails|does not comply|is not compliant|non-compliant|does not meet)\b", re.I)


def verdict_violations(text: str, verdict: str) -> list[str]:
    if verdict == "PASS" and _NEG.search(text):
        return [f"[verdict] the explanation says '{_NEG.search(text).group(0)}' but the verdict is PASS"]
    if verdict == "FAIL" and _POS.search(text):
        return [f"[verdict] the explanation says '{_POS.search(text).group(0)}' but the verdict is FAIL"]
    if verdict == "NEEDS REVIEW" and _POS.search(text) and not re.search(r"\b(if|only|condition|confirm|review)\b", text, re.I):
        return ["[verdict] the explanation states compliance but the verdict is NEEDS REVIEW"]
    return []


_CLAIM_NEG = re.compile(r"\b(does not meet|do not meet|doesn't meet|did not meet|fails?|failing|does not satisfy|does not comply|non-compliant)\b", re.I)
_CLAIM_POS = re.compile(r"(?<!not )(?<!n't )\b(meets|satisfies|complies with|is within|is compliant)\b", re.I)
_HEDGED = re.compile(r"\b(if|only|provided|unless|condition|confirm|confirmed|verify|verified|depends|whether|subject to)\b", re.I)


def claim_violations(text: str, res: RowResult) -> list[str]:
    """Explanations may not contradict a computed outcome or state an unverified condition as fact."""
    from ..guardrails import CITATION_RE, extract_numbers, split_sentences

    out = []
    for sent in split_sentences(CITATION_RE.sub("", text)):
        nums = {v for v, _ in extract_numbers(sent)}
        for r in res.applied:
            if r.limit.value not in nums or r.outcome is None:
                continue
            neg, pos = bool(_CLAIM_NEG.search(sent)), bool(_CLAIM_POS.search(sent))
            if neg and not pos and r.outcome == "pass":
                out.append(f"[claim] says the value does not meet {r.limit.describe()}, but code computed that it meets it")
            if pos and not neg and r.outcome == "fail":
                out.append(f"[claim] says the value meets {r.limit.describe()}, but code computed that it does not")
        for r in res.applied:
            cond_nums = {v for v, _ in extract_numbers(r.limit.condition or "")}
            if cond_nums & nums and not _HEDGED.search(sent):
                out.append(f"[condition] states the condition ({r.limit.condition[:60]}) as a fact; it has not been verified")
    return sorted(set(out))


def _cache_load() -> dict:
    try:
        return json.loads(EXPLAIN_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def explain_row(res: RowResult, clauses: dict[str, Clause], llm, cache: dict | None = None) -> RowResult:
    used = [clauses[r.limit.clause_uid] for r in res.applied if r.limit.clause_uid in clauses]
    used = list({c.uid: c for c in used}.values())
    if not used or llm is None:
        return res
    row = res.row
    key = hashlib.sha1(json.dumps([row.model_dump(), res.verdict, [c.uid for c in used], llm.label]).encode()).hexdigest()
    if cache is not None and key in cache:
        res.explanation, res.explanation_status = cache[key], "verified"
        return res
    messages = [{"role": "user", "content": EXPLAIN_PROMPT.format(
        item=f"{row.item_id} {row.space} - {row.parameter}".strip(),
        value=f"{row.value:g} {row.unit}",
        verdict=res.verdict,
        reason=res.reason,
        condition_rule=(
            "The verdict depends on a condition that has NOT been checked: say that it must be confirmed, "
            "and never say whether it is met. " if res.verdict == "NEEDS REVIEW" else
            "Only state what the reason above states; do not add conditions or requirements. "
        ),
        clauses="\n\n".join(f"{c.citation} {c.title}\n{c.text}" for c in used),
    )}]
    from ..guardrails import extract_numbers

    allowed = [(row.value, row.unit)] + [(r.value_in_limit_unit, r.limit.unit) for r in res.applied if r.value_in_limit_unit is not None]
    allowed += extract_numbers(f"{row.item_id} {row.space} {row.notes} {row.source}")  # deck numbers, item IDs, notes
    for _ in range(2):
        try:
            text = llm.complete(messages, max_tokens=800).strip()
        except LLMError:
            return res
        chk = check_answer(text, used, extra_numbers=allowed,
                           extra_text=f"{row.item_id} {row.space} {row.parameter} {row.notes} {row.source} {row.unit} "
                                      "design value meets does not meet limit pass fail review condition")
        problems = [str(v) for v in chk.violations] + verdict_violations(text, res.verdict) + claim_violations(chk.text, res)
        if not problems:
            res.explanation, res.explanation_status, res.explanation_violations = chk.text, "verified", []
            if cache is not None:
                cache[key] = chk.text
            return res
        res.explanation_status, res.explanation_violations = "rejected", problems
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "Your explanation failed these checks:\n- " + "\n- ".join(problems)
                      + "\nRewrite it and fix every problem."}]
    return res


# ------------------------------------------------------------------ entry point


def run_check(
    rows: list[DesignRow],
    table: RuleTable,
    clauses: list[Clause],
    encoder=None,
    llm=None,
    explain: bool = False,
    workers: int = 4,
) -> list[RowResult]:
    matcher = Matcher(table.limits, encoder)
    results = [evaluate_row(r, matcher) for r in rows]
    if explain and llm is not None:
        by_uid = {c.uid: c for c in clauses}
        cache = _cache_load()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda r: explain_row(r, by_uid, llm, cache), results))
        EXPLAIN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EXPLAIN_CACHE.write_text(json.dumps(cache, indent=1))
    return results


def results_to_csv(results: list[RowResult]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["item_id", "space", "parameter", "value", "unit", "design_source", "verdict", "limits", "clauses", "reason",
                "explanation", "synthetic"])
    for r in results:
        w.writerow([
            r.row.item_id, r.row.space, r.row.parameter, r.row.raw_value or r.row.value, r.row.unit, r.row.source, r.verdict,
            "; ".join(f"{x.limit.describe()}{' if ' + x.limit.condition if x.limit.condition else ''}" for x in r.applied),
            "; ".join(sorted({f'{x.limit.doc} § {x.limit.clause_id}' for x in r.applied})),
            r.reason.replace("\n", " "), r.explanation, "yes" if r.synthetic else "",
        ])
    return out.getvalue()


# ------------------------------------------------------------------ change impact

class Change(BaseModel):
    item_id: str
    kind: Literal["new", "removed", "verdict", "value"]
    before: RowResult | None = None
    after: RowResult | None = None

    @property
    def summary(self) -> str:
        b, a = self.before, self.after
        if self.kind == "new":
            return f"new item: {a.verdict}"
        if self.kind == "removed":
            return f"removed (was {b.verdict})"
        if self.kind == "verdict":
            return f"{b.verdict} -> {a.verdict} ({b.row.display} -> {a.row.display})"
        return f"value {b.row.display} -> {a.row.display}, still {a.verdict}"


def diff_results(before: list[RowResult], after: list[RowResult]) -> list[Change]:
    """What a design change did to compliance, by item_id. Verdict changes first, worst (new FAILs) on top."""
    old = {r.row.item_id: r for r in before}
    new = {r.row.item_id: r for r in after}
    out = []
    for k, a in new.items():
        b = old.get(k)
        if b is None:
            out.append(Change(item_id=k, kind="new", after=a))
        elif a.verdict != b.verdict:
            out.append(Change(item_id=k, kind="verdict", before=b, after=a))
        elif (a.row.value, a.row.unit) != (b.row.value, b.row.unit):
            out.append(Change(item_id=k, kind="value", before=b, after=a))
    out += [Change(item_id=k, kind="removed", before=b) for k, b in old.items() if k not in new]
    rank = {"FAIL": 0, "NEEDS REVIEW": 1, "PASS": 2}
    order = {"verdict": 0, "new": 1, "removed": 2, "value": 3}
    return sorted(out, key=lambda c: (order[c.kind], rank[(c.after or c.before).verdict]))
