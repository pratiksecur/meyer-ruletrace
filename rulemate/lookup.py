"""Exact clause-ID lookup that tolerates how people type clause references."""

from __future__ import annotations

import re

from .models import Clause


def normalize_id(s: str) -> str:
    s = s.strip().strip(" .,;:?!\"'")
    if s[:1] == "(" and s[-1:] == ")" and s.count("(") == 1:
        s = s[1:-1]
    while s[:1] == "(" and s.count("(") > s.count(")"):
        s = s[1:]
    while s[-1:] == ")" and s.count(")") > s.count("("):
        s = s[:-1]
    s = re.sub(r"^\d+\s*C\.?F\.?R\.?\s*", "", s, flags=re.I)  # "46 CFR 190.10-25" -> "190.10-25"
    if s.startswith("[") and s.endswith("]") and s.count("[") == 1:
        s = s[1:-1]
    s = re.sub(r"^(?:reg(?:ulation)?\.?|§|clause|section|para(?:graph)?\.?)\s*", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bpart\s*(\d+)", r"Pt.\1", s, flags=re.I)
    s = re.sub(r"\bpt\.?\s*(\d+)", r"Pt.\1", s, flags=re.I)
    s = re.sub(r"\bch(?:apter)?\.?\s*(\d+)", r"Ch.\1", s, flags=re.I)
    s = re.sub(r"\bsec(?:tion)?\.?\s*(\d+)", r"Sec.\1", s, flags=re.I)
    return s.lower()


def find_clause(clauses: list[Clause], query: str, doc: str | None = None) -> list[Clause]:
    """Return clauses whose ID matches ``query``: exact first, then unambiguous suffix matches.

    "Reg. II-2/13.3.1" -> II-2/13.3.1;  "13.3.1" -> II-2/13.3.1;  "[2.1.3]" -> "... Sec.4 [2.1.3]"
    """
    q = normalize_id(query)
    if not q:
        return []
    pool = [c for c in clauses if doc is None or c.doc == doc]
    exact = [c for c in pool if normalize_id(c.clause_id) == q]
    if exact:
        return exact
    bracket = q if q.startswith("[") else f"[{q}]"
    suffix = [
        c
        for c in pool
        if (cid := normalize_id(c.clause_id)).endswith(("/" + q, " " + q, "." + q, " " + bracket))
        or cid == bracket.lower()
    ]
    return suffix
