"""Structured rule table: numeric limits extracted from clauses.

The LLM proposes limits as JSON; code validates every one before it is stored:
* the value appears in the clause text (with the same unit, or the unit appears in a table header)
* the unit is understood by pint
* the comparator is one of >=, <=, >, <
* a quote, if given, is word for word in the clause (otherwise it is dropped)
A regex extractor is the offline fallback (and fills in clauses where the LLM gave nothing valid).

Result: data/index/limits.json
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from ..config import INDEX_DIR
from ..guardrails import UNIT_ALIASES, _norm_text, extract_numbers
from ..models import Clause
from .units import UnitError, compatible, convert, parse_unit

LIMITS_FILE = INDEX_DIR / "limits.json"
Comparator = Literal[">=", "<=", ">", "<"]


class Limit(BaseModel):
    id: str
    clause_uid: str
    doc: str
    clause_id: str
    parameter: str  # generic name of the quantity, e.g. "corridor clear width"
    subject: str = ""  # what it applies to, as stated
    qualifier: str | None = None  # limit only applies to this kind of space/item, e.g. "crew accommodation"
    condition: str | None = None  # extra condition code cannot verify, e.g. "serves not more than 50 persons"
    comparator: Comparator
    value: float
    unit: str
    quote: str | None = None
    method: Literal["llm", "regex", "manual"] = "llm"
    synthetic: bool = False
    context: str = ""  # the clause sentence that states the value (taken by code, used for matching)
    refs: list[str] = Field(default_factory=list)  # clause_ids the source clause refers to
    notes: list[str] = Field(default_factory=list)  # corrections made by validation

    @property
    def citation(self) -> str:
        return f"[{self.doc} § {self.clause_id}]"

    def describe(self) -> str:
        return f"{self.comparator} {self.value:g} {self.unit}"


class RuleTable(BaseModel):
    limits: list[Limit] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)  # proposals that failed validation, with reasons
    clause_hashes: dict[str, str] = Field(default_factory=dict)  # clause uid -> text hash (for incremental rebuilds)
    model: str = ""


# ------------------------------------------------------------------ candidates

_LIMIT_WORDS = re.compile(
    r"\b(not less than|not more than|not exceed(?:ing)?|shall not exceed|at least|minimum|maximum|"
    r"no(?:t)? greater than|no(?:t)? smaller than|up to|min\.|max\.|increased to|reduced to|"
    r"not be (?:less|more) than|more than .{0,60}not (?:be )?permitted|no one of which \w+ \w+ more than)\b",
    re.I,
)


# sections that say where a rule applies, not what a design must meet ("This part applies to vessels of
# 300 gross tons or more") - their numbers are thresholds, never design limits
_SCOPE_TITLES = re.compile(r"^(application|applicability|scope|purpose|intent|definitions?|preemptive effect|general)\b", re.I)


def is_candidate(c: Clause) -> bool:
    if c.kind == "heading" or _SCOPE_TITLES.match(c.title or ""):
        return False
    has_unit_number = any(u for _, u in extract_numbers(c.text))
    return (c.kind == "table" and bool(extract_numbers(c.text))) or (has_unit_number and bool(_LIMIT_WORDS.search(c.text)))


def _hash(c: Clause) -> str:
    return hashlib.sha1(c.text.encode()).hexdigest()[:12]


# ------------------------------------------------------------------ validation

_CMP_WORDS = {
    ">=": ">=", "≥": ">=", "min": ">=", "minimum": ">=", "at least": ">=", "not less than": ">=",
    "<=": "<=", "≤": "<=", "max": "<=", "maximum": "<=", "not more than": "<=", "not exceeding": "<=",
    ">": ">", "<": "<",
}


_MIN_PHRASE = re.compile(
    r"(not less than|not be less than|at least|no less than|minimum(?: (?!and\b|or\b)\w+)?(?: of)?|min\.)\s*$", re.I)
_MAX_PHRASE = re.compile(
    r"(not more than|not be more than|not exceeding|shall not exceed|not exceed|no more than|up to|"
    r"maximum(?: (?!and\b|or\b)\w+)?(?: of)?|max\.|increased to)\s*$", re.I)
_PROHIBITED_ABOVE = re.compile(r"\bmore than\s*$", re.I)  # "more than 40 feet ... shall not be permitted"
_PROHIBITED = re.compile(
    r"\b(?:shall|must|may) not be permitted|\bnot permitted\b|\bprohibited\b|\bno one of which\b", re.I)
# unit words, US customary included; longest first
UNIT_RX = (
    r"(?:persons|person|square (?:feet|foot|met(?:er|re)s?)|cubic (?:feet|foot|met(?:er|re)s?)|centimet(?:er|re)s?|"
    r"millimet(?:er|re)s?|met(?:er|re)s?|inches|inch|feet|foot|ft|mm|cm|m2|m²|m3|m³|m|degrees|°|min|s|kN|N|%)"
)
_NUM_TOKEN = r"\d+(?:\s\d/\d+|\.\d+)?"
_POST_CMP = re.compile(rf"^\s?{UNIT_RX}?(?![A-Za-z])(?:\s*\([^)]*\))?(?:\s+(?:diameter|wide|high|long|deep))?"
                       rf"\s+(minimum|maximum|min\.|max\.)", re.I)
_BAD_QUALIFIERS = {"any", "all", "none", "table", "min", "max", "degrees", "division class", "both sides", "n/a"}


def _num(tok: str) -> float:
    if re.search(r"\s", tok):  # mixed fraction "39 1/2" (any space, incl. narrow no-break)
        whole, frac = tok.split()
        n, d = frac.split("/")
        return float(whole) + float(n) / float(d)
    return float(tok)


def _value_spans(text: str, value: float) -> list[re.Match]:
    return [m for m in re.finditer(rf"(?<![\w./]){_NUM_TOKEN}(?![\d/])", text) if _num(m.group(0)) == value]


_UNIT_NUM = re.compile(rf"{_NUM_TOKEN}\s?{UNIT_RX}(?![A-Za-z])")


def comparator_from_text(text: str, value: float) -> str | None:
    """Direction stated right before the value, if unambiguous."""
    found = set()
    for m in _value_spans(text, value):
        before = text[max(0, m.start() - 30) : m.start()]
        post = _POST_CMP.match(text[m.end() : m.end() + 60])
        if post:  # "36 inches (915 mm) maximum": the word after the value binds tighter than one before it
            found.add(">=" if post.group(1).lower().startswith("min") else "<=")
        elif _MIN_PHRASE.search(before):
            found.add(">=")
        elif _MAX_PHRASE.search(before):
            found.add("<=")
        elif _PROHIBITED_ABOVE.search(before) and _PROHIBITED.search(_sentence_at(text, m.start())):
            found.add("<=")

    return found.pop() if len(found) == 1 else None


def _sentence_at(text: str, pos: int) -> str:
    start = max(text.rfind(". ", 0, pos), text.rfind("\n", 0, pos)) + 1
    end = text.find(". ", pos)
    return text[start : end if end > 0 else len(text)]


_PER_PERSON = re.compile(r"\b(?:for each|per)\s+(?:person|member|occupant|passenger|berth)\b", re.I)


def context_sentence(text: str, value: float) -> str:
    for line in text.splitlines():
        if line.startswith("|") and _value_spans(line, value):
            return line.strip()  # table row
    for sent in re.split(r"(?<=[.;])\s+", text):
        spans = _value_spans(sent, value)
        if spans:
            # several limits in one sentence ("riser ... 200 mm and the tread ... 240 mm"): keep this limit's part
            prev_ends = [m.end() for m in _UNIT_NUM.finditer(sent) if m.end() <= spans[0].start()]
            start = prev_ends[-1] if prev_ends else 0
            end = sent.find(" and ", spans[0].end())
            return sent[start : end if end > 0 else len(sent)].strip(" ,.;")
    return text[:200]


def validate(raw: dict, clause: Clause, n: int, method: str) -> tuple[Limit | None, str]:
    try:
        cmp = _CMP_WORDS.get(str(raw.get("comparator", "")).strip().lower())
        if cmp is None:
            return None, f"bad comparator {raw.get('comparator')!r}"
        value = float(str(raw.get("value")).replace(",", ""))
        unit = str(raw.get("unit") or "").strip()
        parameter = str(raw.get("parameter") or "").strip()
    except (TypeError, ValueError):
        return None, "value is not a number"
    if not parameter:
        return None, "missing parameter"
    if not unit:
        return None, "missing unit"
    try:
        parse_unit(unit)
    except UnitError as e:
        return None, str(e)

    src = extract_numbers(clause.text)
    if value not in {v for v, _ in src}:
        return None, f"value {value:g} does not appear in the clause"
    canon = next((cu for cu, al in UNIT_ALIASES.items() if unit in al or unit.lower() in al), None)
    paired = {(v, u) for v, u in src if u}
    if (value, canon) not in paired:
        # tables give units in the header: accept if the unit word appears anywhere in the clause
        aliases = UNIT_ALIASES.get(canon, [unit]) if canon else [unit]
        if not any(re.search(rf"(?<![A-Za-z]){re.escape(a)}(?![A-Za-z])", clause.text) for a in aliases):
            return None, f"unit {unit} for value {value:g} not found in the clause"

    notes: list[str] = []
    text_cmp = comparator_from_text(clause.text, value)
    if text_cmp and text_cmp != cmp and cmp in (">=", "<="):
        notes.append(f"comparator corrected from {cmp} to {text_cmp} (text)")
        cmp = text_cmp

    if "/" not in unit and any(
        re.match(r"\s?[A-Za-z%°²]*\s+per\s+\w", clause.text[m.end():]) for m in _value_spans(clause.text, value)
    ):
        return None, f"{value:g} is a per-unit rate in the text; unit '{unit}' is not a rate"
    if "/" not in unit and any(_PER_PERSON.search(_sentence_at(clause.text, m.start())) for m in _value_spans(clause.text, value)):
        return None, f"{value:g} is a per-person quantity in the text; unit '{unit}' is not a rate"

    quote = raw.get("quote") or None
    if quote and _norm_text(quote) not in _norm_text(clause.text):
        quote = None  # keep the limit, drop the unverifiable quote

    def opt(k: str) -> str | None:
        v = raw.get(k)
        v = str(v).strip() if v not in (None, "", "null", "none", "None") else None
        return v or None

    qualifier = opt("qualifier")
    if qualifier and (qualifier.lower() in _BAD_QUALIFIERS or _norm_text(qualifier).rstrip("s") not in _norm_text(clause.text)):
        notes.append(f"qualifier '{qualifier}' dropped (not a space/item named in the clause)")
        qualifier = None

    return (
        Limit(
            id=f"{clause.uid}#{n}",
            clause_uid=clause.uid,
            doc=clause.doc,
            clause_id=clause.clause_id,
            parameter=parameter,
            subject=opt("subject") or "",
            qualifier=qualifier,
            condition=opt("condition"),
            comparator=cmp,  # type: ignore[arg-type]
            value=value,
            unit=unit,
            quote=quote,
            method=method,  # type: ignore[arg-type]
            synthetic=clause.synthetic,
            context=context_sentence(clause.text, value),
            refs=list(clause.refs),
            notes=notes,
        ),
        "",
    )


# ------------------------------------------------------------------ regex extractor (offline fallback)

_RX = re.compile(
    r"(?P<cmp>not less than|at least|not more than|not exceeding|shall not exceed|not exceed|"
    r"no more than|no less than|not be less than|not be more than|minimum \w+ of|"
    r"(?:be )?increased to|(?:be )?reduced to(?! not)|more than)\s+"
    rf"(?P<val>{_NUM_TOKEN})\s?(?P<unit>{UNIT_RX})(?![A-Za-z])",
    re.I,
)
_COND = re.compile(r"\b(provided that|where|if|unless|subject to)\b(.*)$", re.I)


_RX_POST = re.compile(
    rf"(?P<val>{_NUM_TOKEN})\s?(?P<unit>{UNIT_RX})(?![A-Za-z])(?:\s*\([^)]*\))?(?:\s+(?:diameter|wide|high|long|deep))?"
    rf"\s+(?P<cmp>minimum|maximum|min\.|max\.)",
    re.I,
)


def extract_regex(c: Clause) -> list[dict]:
    out = []
    for sent in re.split(r"(?<=[.;])\s+", c.text):
        cond = _COND.search(sent)
        last_end, subject = 0, ""
        found = sorted([*_RX.finditer(sent), *_RX_POST.finditer(sent)], key=lambda m: m.start("val"))
        seen_vals: set[int] = set()
        for m in found:
            if m.start("val") in seen_vals:
                continue
            seen_vals.add(m.start("val"))
            words = m.group("cmp").lower()
            if m.re is _RX_POST:
                words = "minimum" if words.startswith("min") else "maximum"
            if words == "more than" and not _PROHIBITED.search(sent):
                continue  # "more than 300 square feet" without a prohibition is a threshold, not a limit
            cmp = ">=" if (words in ("not less than", "at least", "no less than", "not be less than")
                           or words.startswith("minimum") or "reduced" in words) else "<="
            seg = re.sub(r"^\s*(and|or|,)\s+", "", sent[last_end : m.start()], flags=re.I)
            if m.re is _RX_POST:  # "the clear width of walking surfaces shall be 36 inches minimum"
                seg = re.sub(r"\s*\b(shall be|must be|is)\s*$", "", seg, flags=re.I)
            seg = re.sub(r"\b(shall|must|may)\b.*$", "", seg, flags=re.I).strip(" ,")
            subject = seg if len(re.findall(r"[A-Za-z]{3,}", seg)) >= 2 else (subject or c.title)
            last_end = m.end()
            q = re.match(r"^In ([^,]+),", sent)
            out.append(
                {
                    "parameter": f"{c.title} {subject}".strip()[:120],
                    "subject": subject,
                    "qualifier": q.group(1) if q else None,
                    "condition": cond.group(0).strip(" .") if cond else None,
                    "comparator": cmp,
                    "value": _num(m.group("val")),
                    "unit": m.group("unit"),
                    "quote": m.group(0),
                }
            )
    return out


# ------------------------------------------------------------------ LLM extractor

EXTRACT_PROMPT = """Extract every explicit numeric limit from the rule clause below.
Return only JSON: {"limits": [ ... ]}. Each limit has these fields:
- "parameter": short generic name of the measured quantity, e.g. "corridor clear width", "stair inclination angle", \
"fire door fire integrity rating", "fire door closing time", "handrail height", "travel distance to stairway enclosure"
- "subject": what the limit applies to, as stated in the text
- "qualifier": if the limit applies only to one kind of space or item (e.g. "crew accommodation", "cabin doors", \
"stairway enclosures", "main vertical zone bulkheads"), that kind; otherwise null
- "condition": any other condition that must hold for the limit to apply, copied from the text \
(e.g. "the corridor serves not more than 50 persons", "subject to the approval of the Administration"); otherwise null
- "comparator": ">=" for a minimum (not less than, at least), "<=" for a maximum (not more than, not exceeding), ">" or "<" if strict
- "value": the number exactly as written
- "unit": the unit as written (mm, m, degrees, min, s, N, m2, ...)
- "quote": the exact words from the text that state the limit, e.g. "not less than 1200 mm"
Rules: include only limits stated with a number. A number that only says when a rule applies (e.g. "more than 36 \
passengers") is not a limit by itself; put it in "condition" of the limit it restricts. For a table, give one limit \
per row and use the row label as the qualifier. If there are no limits, return {"limits": []}.

Clause {citation} {title}:
{text}"""


def _parse_json(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    limits = data.get("limits", []) if isinstance(data, dict) else []
    return [x for x in limits if isinstance(x, dict)]


def extract_llm(c: Clause, llm) -> list[dict]:
    prompt = EXTRACT_PROMPT.replace("{citation}", c.citation).replace("{title}", c.title).replace("{text}", c.text)
    raw = llm.complete([{"role": "user", "content": prompt}], max_tokens=1500)
    return _parse_json(raw)


# ------------------------------------------------------------------ build


def _merge_equivalents(limits: list[Limit], clause: Clause) -> list[Limit]:
    """'190 centimeters (75 inches)' is one limit stated twice. Keep the value written first; record the other."""
    def pos(l: Limit) -> int:
        spans = _value_spans(clause.text, l.value)
        return spans[0].start() if spans else 10**9

    kept: list[Limit] = []
    for l in sorted(limits, key=pos):
        twin = None
        for k in kept:
            if k.comparator == l.comparator and compatible(k.unit, l.unit) and k.unit != l.unit:
                try:
                    v = convert(l.value, l.unit, k.unit)
                except UnitError:
                    continue
                if k.value and abs(v - k.value) / abs(k.value) <= 0.02:
                    twin = k
                    break
        if twin is None:
            kept.append(l)
        else:
            twin.notes.append(f"also stated as {l.value:g} {l.unit} (equivalent, not checked separately)")
    return kept


def build_rule_table(
    clauses: list[Clause],
    llm=None,
    method: Literal["auto", "llm", "regex"] = "auto",
    previous: RuleTable | None = None,
    workers: int = 4,
) -> RuleTable:
    """Extract limits from every candidate clause. Unchanged clauses are reused from ``previous``."""
    cands = [c for c in clauses if is_candidate(c)]
    prev_ok = {}
    if previous is not None:
        for c in cands:
            if previous.clause_hashes.get(c.uid) == _hash(c):
                prev_ok[c.uid] = [l for l in previous.limits if l.clause_uid == c.uid]

    def work(c: Clause) -> tuple[list[Limit], list[dict]]:
        if c.uid in prev_ok:
            return prev_ok[c.uid], []
        limits, rejected = [], []
        proposals: list[tuple[dict, str]] = []
        if method in ("auto", "llm") and llm is not None:
            try:
                proposals = [(p, "llm") for p in extract_llm(c, llm)]
            except Exception as e:  # LLM down: fall back below
                rejected.append({"clause": c.uid, "reason": f"LLM error: {str(e)[:120]}"})
        for p, how in proposals:
            lim, why = validate(p, c, len(limits), how)
            if lim:
                limits.append(lim)
            else:
                rejected.append({"clause": c.uid, "proposal": p, "reason": why})
        if not limits and method in ("auto", "regex"):
            for p in extract_regex(c):
                lim, why = validate(p, c, len(limits), "regex")
                if lim:
                    limits.append(lim)
                else:
                    rejected.append({"clause": c.uid, "proposal": p, "reason": why})
        return limits, rejected

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(work, cands))

    table = RuleTable(model=getattr(llm, "label", "") if llm else "regex")
    by_uid = {c.uid: c for c in cands}
    results = [(_merge_equivalents(lims, by_uid[c.uid]), rej) for c, (lims, rej) in zip(cands, results)]
    for c, (lims, rej) in zip(cands, results):
        table.limits.extend(lims)
        table.rejected.extend(rej)
        table.clause_hashes[c.uid] = _hash(c)
    return table


def save_rule_table(t: RuleTable, path=LIMITS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(t.model_dump_json(indent=1), encoding="utf-8")


def load_rule_table(path=LIMITS_FILE) -> RuleTable | None:
    if not path.exists():
        return None
    return RuleTable.model_validate_json(path.read_text(encoding="utf-8"))
