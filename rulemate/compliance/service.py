"""Glue used by the CLI, API and UI: load or build the rule table, run checks."""

from __future__ import annotations

import os
from typing import Literal

from ..ingest.pipeline import load_index
from ..llm import get_llm
from ..retrieval import get_retriever
from .check import DesignRow, RowResult, run_check
from .limits import LIMITS_FILE, RuleTable, _hash, build_rule_table, is_candidate, load_rule_table, save_rule_table

Method = Literal["auto", "llm", "regex"]


def _has_hosted_llm(spec: str | None) -> bool:
    names = [n.strip() for n in (spec or os.getenv("LLM_PROVIDER", "groq,ollama")).split(",")]
    return any(
        (n == "groq" and os.getenv("GROQ_API_KEY")) or (n == "openai" and os.getenv("OPENAI_API_KEY")) for n in names
    )


def resolve_method(method: Method, provider: str | None) -> str:
    """'auto' means the deterministic regex extractor. Measured on the two labelled example designs, an LLM-built
    rule table (Groq gpt-oss-120b, 6 Nov 2026) got 79 % / 87 % of verdicts right against 100 % / 100 % for the
    regex table: it dropped conditions ("where two tiers of berths are fitted") and added relaxations. LLM
    extraction stays available with method="llm" to propose limits, which an engineer then reviews."""
    if method != "auto":
        return method
    return "regex"


def get_rule_table(method: Method = "auto", provider: str | None = None, force: bool = False) -> RuleTable:
    clauses = load_index().clauses
    table = None if force else load_rule_table()
    if table is not None:
        cands = {c.uid: _hash(c) for c in clauses if is_candidate(c)}
        if cands == table.clause_hashes:
            return table  # up to date
    m = resolve_method(method, provider)
    llm = get_llm(provider) if m in ("auto", "llm") else None
    table = build_rule_table(clauses, llm=llm, method=m, previous=None if force else table)  # type: ignore[arg-type]
    save_rule_table(table)
    return table


def save_edited_table(table: RuleTable) -> None:
    save_rule_table(table)


def get_encoder():
    r = get_retriever()
    return r._model_obj() if r.mode == "hybrid" else None


def rule_documents() -> list[str]:
    return sorted({c.doc for c in load_index().clauses})


def check_rows(rows: list[DesignRow], explain: bool = False, provider: str | None = None,
               docs: list[str] | None = None) -> list[RowResult]:
    """``docs`` = the rule sets that apply to this vessel (e.g. ["46CFR190"] for a US research vessel);
    None applies every indexed document."""
    table = get_rule_table(provider=provider)
    if docs:
        unknown = set(docs) - set(rule_documents())
        if unknown:
            raise ValueError(f"unknown rule document(s): {', '.join(sorted(unknown))}; have {', '.join(rule_documents())}")
        table = table.model_copy(update={"limits": [l for l in table.limits if l.doc in docs]})
    return run_check(
        rows,
        table,
        load_index().clauses,
        encoder=get_encoder(),
        llm=get_llm(provider) if explain else None,
        explain=explain,
    )


__all__ = ["get_rule_table", "check_rows", "rule_documents", "save_edited_table", "LIMITS_FILE"]
