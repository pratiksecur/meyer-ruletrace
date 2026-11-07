"""Post-generation checks. The LLM writes; this code decides whether the text may be shown.

Checks (each failure is a Violation with a message the LLM can act on in its retry):

1. citation    - every sentence carries at least one [doc § clause] citation
2. retrieved   - every cited clause was actually retrieved for this question
3. number      - every number (and its unit) appears in a clause cited by that sentence
4. quote       - quoted text appears word for word in a cited clause
5. hedge       - no guessing words ("probably", "typically", ...) unless the cited source uses them

Sentences exempt from the citation rule: lead-ins ending in ":" , markdown headings, and
statements that something is *not* specified (they carry no claim) as long as they contain no numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .lookup import find_clause, normalize_id
from .models import Clause

# [doc § clause]; clause IDs may contain one level of brackets, e.g. "Pt.3 Ch.2 Sec.4 [2.1.3]"
CITATION_RE = re.compile(r"\[\s*([^\[\]§]+?)\s*§\s*((?:[^\[\]]|\[[^\[\]]*\])+?)\s*\]")
_PLACEHOLDER = "⟦C{}⟧"  # ⟦C0⟧ – survives sentence splitting
_PH_RE = re.compile(r"⟦C(\d+)⟧")

HEDGES = [
    "probably", "typically", "usually", "likely", "generally", "normally", "perhaps", "maybe",
    "might", "presumably", "in most cases", "often", "approximately", "roughly", "around",
    "i think", "i believe", "it seems", "should be fine", "in general", "commonly",
]
_HEDGE_RE = re.compile(r"\b(" + "|".join(re.escape(h) for h in HEDGES) + r")\b", re.I)

_ABSENCE_RE = re.compile(
    r"\b(not (?:specified|found|covered|stated|addressed|defined|given|mentioned)|"
    r"do(?:es)? not (?:specify|state|cover|address|mention|define|give|contain))\b",
    re.I,
)

UNIT_ALIASES = {
    "mm": ["mm", "millimetres", "millimetre", "millimeters", "millimeter"],
    "cm": ["cm", "centimetres", "centimetre", "centimeters", "centimeter"],
    "m2": ["m2", "m²", "sq m", "square metres", "square metre", "square meters", "square meter"],
    "m3": ["m3", "m³", "cubic metres", "cubic metre", "cubic meters", "cubic meter"],
    "ft2": ["ft2", "ft²", "sq ft", "square feet", "square foot"],
    "ft3": ["ft3", "ft³", "cu ft", "cubic feet", "cubic foot"],
    "in": ["inches", "inch"],
    "ft": ["feet", "foot", "ft"],
    "m": ["m", "metres", "metre", "meters", "meter"],
    "deg": ["°", "degrees", "degree", "deg"],
    "min": ["minutes", "minute", "mins", "min"],
    "s": ["seconds", "second", "secs", "sec", "s"],
    "kN": ["kN", "kilonewtons", "kilonewton"],
    "N": ["N", "newtons", "newton"],
    "%": ["%", "percent", "per cent"],
    "persons": ["persons", "person", "people"],
    "passengers": ["passengers", "passenger"],
}
_ALIAS_TO_UNIT = {a.lower() if a not in ("N", "kN") else a: u for u, al in UNIT_ALIASES.items() for a in al}
_UNIT_ALT = "|".join(sorted((re.escape(a) for a in _ALIAS_TO_UNIT), key=len, reverse=True))
# number, optional unit. Clause-ID-like numbers (two or more dots) are skipped later.
_NUM_RE = re.compile(
    rf"(?<![\w.\-/])(\d+\s\d/\d+|\d{{1,3}}(?:,\d{{3}})+|\d+(?:\.\d+)*)(?:\s?({_UNIT_ALT})(?![A-Za-z0-9²³]))?"
)
_ID_CONTEXT_RE = re.compile(r"(?:regulations?|reg\.|paragraphs?|clauses?|sections?|sec\.|table|chapter|part|pt\.|ch\.|§)\s*$", re.I)
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


@dataclass
class Violation:
    kind: str  # citation | retrieved | number | quote | hedge | format
    message: str
    sentence: str = ""

    def __str__(self) -> str:
        s = f' in: "{self.sentence[:120]}"' if self.sentence else ""
        return f"[{self.kind}] {self.message}{s}"


@dataclass
class CheckResult:
    ok: bool
    violations: list[Violation]
    cited: list[Clause]  # retrieved clauses the answer cites, in order of first use
    text: str = ""  # the answer with every resolved citation rewritten to its canonical [doc § clause] form


# ------------------------------------------------------------------ helpers


def _norm_text(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def extract_numbers(text: str) -> list[tuple[float, str | None]]:
    """(value, canonical unit or None) for every number in ``text`` that is not a clause/table ID."""
    out = []
    for m in _NUM_RE.finditer(text):
        raw, unit = m.group(1), m.group(2)
        if raw.count(".") >= 2:
            continue  # 3.1.1 is a clause number
        if _ID_CONTEXT_RE.search(text[max(0, m.start() - 14) : m.start()]):
            continue  # "Table 13.1", "regulation 9"
        if re.search(r"\s", raw):  # mixed fraction "39 1/2" (any space, incl. narrow no-break)
            whole, frac = raw.split()
            n, d = frac.split("/")
            value = float(whole) + float(n) / float(d)
        else:
            value = float(raw.replace(",", ""))
        canon = None
        if unit:
            canon = _ALIAS_TO_UNIT.get(unit) or _ALIAS_TO_UNIT.get(unit.lower())
        out.append((value, canon))
    return out


def split_sentences(text: str) -> list[str]:
    """Split on sentence ends and line breaks. Expects citations already replaced by placeholders."""
    protected = re.sub(r"\b(e\.g|i\.e|etc|Reg|Pt|Ch|Sec|No|approx|cf)\.", lambda m: m.group(0).replace(".", "․"), text)
    parts: list[str] = []
    for line in protected.splitlines():
        line = line.strip()
        if not line:
            continue
        parts += re.split(r"(?<=[.!?])\s+(?=[\"“(\[A-Z0-9*⟦-])", line)
    out: list[str] = []
    for p in parts:
        p = p.replace("․", ".").strip()
        if not p:
            continue
        # citations at the start of a fragment belong to the previous sentence ("... 1200 mm. [doc § x] Next ...")
        lead = re.match(r"^((?:\s*⟦C\d+⟧[\s.;,]*)+)(.*)$", p)
        if out and lead:
            out[-1] = f"{out[-1]} {lead.group(1).strip()}"
            p = lead.group(2).strip()
            if not p:
                continue
        out.append(p)
    return out


def _is_exempt(sentence_wo_cites: str) -> bool:
    s = _LIST_MARKER_RE.sub("", sentence_wo_cites).strip()
    if not s or s.startswith("#") or s.endswith(":"):
        return True
    if len(re.findall(r"[A-Za-z]+", s)) < 3:
        return True
    if _ABSENCE_RE.search(s) and not re.search(r"\d", s):
        return True
    return False


# ------------------------------------------------------------------ main check


_UNICODE_SPACES = re.compile(r"[\u00a0\u2007\u2009\u200a\u202f]")

# a cited clause must contain at least this share of its sentence's content words (clause text + title + path,
# limit wording like "maximum" ignored). On the eval answers correct citations scored >= 0.58 and wrong ones
# (a number taken from another document, a claim the clause does not make) <= 0.38.
MIN_SUPPORT = 0.45


# words that only say "this is a limit" and are phrased many ways ("maximum" = "not exceeding")
_LIMIT_WORDS = set(
    "maximum minimum max min least most exceed exceeding exceeds less more greater smaller lower higher limit "
    "limits value required require requires allowed permitted permissible need needs needed has have also "
    "both only each every one two three ".split()
)


def _covered(w: str, cw: set[str]) -> bool:
    if w in cw:
        return True
    if len(w) >= 4:  # word forms: stair / stairway, thick / thickness
        return any(len(c) >= 4 and (c.startswith(w) or w.startswith(c)) for c in cw)
    return False


def _support(words: set[str], clause: Clause, extra: set[str]) -> float:
    from .retrieval import tokenize  # lazy: keeps this module light

    words = words - _LIMIT_WORDS
    if not words:
        return 1.0
    cw = set(tokenize(f"{clause.title} {' '.join(clause.path)} {clause.text}")) | extra
    return sum(_covered(w, cw) for w in words) / len(words)


def check_answer(
    answer: str, retrieved: list[Clause], extra_numbers: list[tuple[float, str | None]] | None = None,
    extra_text: str = "",
) -> CheckResult:
    """``extra_numbers``: (value, unit) pairs the answer may state besides the sources, e.g. a design value.
    ``extra_text``: words the answer may use besides the sources, e.g. the design row's space and parameter."""
    from .retrieval import tokenize

    answer = _UNICODE_SPACES.sub(" ", answer)  # hosted models write "10\u202fmm"; check and show plain spaces
    extra_words = set(tokenize(extra_text))
    extra = [(float(v), _ALIAS_TO_UNIT.get(u or "") or _ALIAS_TO_UNIT.get((u or "").lower())) for v, u in (extra_numbers or [])]
    violations: list[Violation] = []
    by_key: dict[tuple[str, str], Clause] = {(c.doc.lower(), normalize_id(c.clause_id)): c for c in retrieved}
    by_id: dict[str, list[Clause]] = {}
    for c in retrieved:
        by_id.setdefault(normalize_id(c.clause_id), []).append(c)

    # 1. pull out citations, resolve them, replace with placeholders
    cites: list[tuple[str, str, Clause | None]] = []

    def repl(m: re.Match) -> str:
        doc, cid = m.group(1).strip(), m.group(2).strip()
        clause = by_key.get((doc.lower(), normalize_id(cid)))
        if clause is None and len(by_id.get(normalize_id(cid), [])) == 1:
            clause = by_id[normalize_id(cid)][0]  # right clause, slightly mangled doc name
        if clause is None:
            short = find_clause(retrieved, cid)  # "5.2" -> II-2/13.5.2, only if unambiguous among retrieved
            clause = short[0] if len(short) == 1 else None
        cites.append((doc, cid, clause))
        return _PLACEHOLDER.format(len(cites) - 1)

    body = CITATION_RE.sub(repl, answer)

    cited: list[Clause] = []
    for doc, cid, clause in cites:
        if clause is None:
            violations.append(Violation("retrieved", f"cites [{doc} § {cid}], which is not one of the provided clauses"))
        elif clause not in cited:
            cited.append(clause)

    if not cites:
        violations.append(Violation("citation", "the answer contains no citations in the form [doc § clause]"))

    # 2. per-sentence checks
    for sent in split_sentences(body):
        idx = [int(i) for i in _PH_RE.findall(sent)]
        sent_clauses = [cites[i][2] for i in idx if cites[i][2] is not None]
        plain = _PH_RE.sub("", sent).strip()
        shown = _PH_RE.sub(lambda m: f"[{cites[int(m.group(1))][0]} § {cites[int(m.group(1))][1]}]", sent)

        if not idx:
            if not _is_exempt(plain):
                violations.append(Violation("citation", "sentence has no citation", shown))
            elif not re.search(r"\d", plain):
                continue  # exempt and nothing else to check
        source = " ".join(c.text for c in sent_clauses) if sent_clauses else ""
        source_n = _norm_text(source)

        # content: a sentence must say something, and every clause it cites must support it
        if idx:
            words = {w for w in tokenize(plain) if not w.replace(".", "").replace("/", "").isdigit()}
            if len(words) < 2:
                violations.append(Violation("format", "sentence has no content besides the citation", shown))
            for c in {c.uid: c for c in sent_clauses}.values():
                sup = _support(words, c, extra_words)
                if sup < MIN_SUPPORT:
                    violations.append(Violation(
                        "support", f"{c.citation} does not support this sentence (only {sup:.0%} of its words are in "
                        "that clause); cite the clause that states it, or leave the claim out", shown))

        # numbers and units
        if sent_clauses:
            src_nums = extract_numbers(source) + extra
            src_values = {v for v, _ in src_nums}
            src_pairs = {(v, u) for v, u in src_nums if u}
            for value, unit in extract_numbers(_LIST_MARKER_RE.sub("", plain)):
                num_s = f"{value:g}{' ' + unit if unit else ''}"
                if value not in src_values:
                    violations.append(Violation("number", f"the number {num_s} does not appear in the cited clause(s)", shown))
                elif unit and (value, unit) not in src_pairs:
                    units = sorted({u for v, u in src_nums if v == value and u})
                    hint = f" (source gives it in {', '.join(units)})" if units else ""
                    violations.append(Violation("number", f"{num_s}: the unit does not match the cited clause(s){hint}", shown))

        # quotes
        for q in re.findall(r"[\"“]([^\"”]{4,})[\"”]", plain):
            pieces = [p for p in re.split(r"\s*(?:\.\.\.|…)\s*", q) if p.strip()]
            if not all(_norm_text(p) in source_n for p in pieces):
                violations.append(Violation("quote", f'quoted text "{q[:80]}" is not word for word in the cited clause(s)', shown))

        # hedges
        for h in {m.group(1).lower() for m in _HEDGE_RE.finditer(plain)}:
            if not re.search(rf"\b{re.escape(h)}\b", source_n):
                violations.append(Violation("hedge", f'guessing word "{h}" is not used by the cited source', shown))

    def canonical(m: re.Match) -> str:
        doc, cid, clause = cites[int(m.group(1))]
        return clause.citation if clause is not None else f"[{doc} § {cid}]"

    return CheckResult(ok=not violations, violations=violations, cited=cited, text=_PH_RE.sub(canonical, body))
