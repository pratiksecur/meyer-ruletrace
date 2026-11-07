"""Local-LLM helpers for RuleTrace (Ollama, nothing leaves the machine).

The language model NEVER decides whether a design complies. The rule engine does that, in code.
The model only does three jobs where wording matters, and each one is fenced in:

  1. explain_findings  - turn an engine finding into 2-3 plain sentences for an engineer.
                         Numbers in the answer must already appear in the finding, otherwise the
                         deterministic explanation is used instead.
  2. draft_rule        - turn pasted regulation text into a DRAFT rule (YAML). The quote must be a
                         verbatim part of the pasted text, the structure is validated, and the rule
                         is always marked ``verified: false`` for a human to check.
  3. answer_question   - chatbot answers grounded in retrieved rules and findings, with [RULE-ID]
                         citations that are checked against what was actually provided.

This module needs only the standard library, PyYAML and (lazily) httpx, so it is easy to test with a
fake client.

Environment variables:
  RULETRACE_LLM_BASE_URL   default http://localhost:11434
  RULETRACE_LLM_MODEL      default mistral
  RULETRACE_LLM_TIMEOUT    seconds, default 180
  RULETRACE_LLM_NUM_CTX    context window sent to Ollama, default 8192
"""

from __future__ import annotations

import hashlib
import json
import os
import re

import yaml


class LLMError(Exception):
    pass


# -- client -----------------------------------------------------------------------------------

class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float | None = None, num_ctx: int | None = None) -> None:
        self.base_url = (base_url or os.getenv("RULETRACE_LLM_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("RULETRACE_LLM_MODEL", "mistral")
        self.timeout = float(timeout or os.getenv("RULETRACE_LLM_TIMEOUT", "180"))
        self.num_ctx = int(num_ctx or os.getenv("RULETRACE_LLM_NUM_CTX", "8192"))
        self.name = f"ollama:{self.model}"

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        import httpx
        body = {"model": self.model, "stream": False,
                "options": {"temperature": 0, "num_ctx": self.num_ctx},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["format"] = "json"
        try:
            r = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
            r.raise_for_status()
            return r.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMError(f"ollama call failed: {exc}") from exc

    def status(self) -> dict:
        """Is Ollama reachable and is the model pulled?"""
        import httpx
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError) as exc:
            return {"model": self.model, "reachable": False, "model_pulled": False,
                    "message": f"Ollama not reachable at {self.base_url}: {exc}"}
        pulled = any(n == self.model or n == f"{self.model}:latest" for n in names)
        return {"model": self.model, "reachable": True, "model_pulled": pulled,
                "message": "ok" if pulled else f"model '{self.model}' not pulled; run: ollama pull {self.model}"}


def parse_json_object(text: str) -> dict:
    """Models sometimes wrap JSON in prose or code fences; take the outermost object."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("model did not return a JSON object")
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMError("model returned JSON that is not an object")
    return value


# -- grounding helpers -------------------------------------------------------------------------

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_in(text: str) -> set[float]:
    return {float(n.replace(",", ".")) for n in _NUM.findall(text or "")}


def numbers_grounded(answer: str, source: str) -> bool:
    """True when every number in ``answer`` also appears in ``source``."""
    return numbers_in(answer) <= numbers_in(source)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


# -- 1. explain findings -----------------------------------------------------------------------

SYSTEM_EXPLAIN = (
    "You explain ship-design compliance findings to a naval architect. "
    "Use ONLY the facts in the JSON you are given. Do not add numbers, rules, paragraphs or requirements "
    "that are not in it, and do not do arithmetic. The compliance decision was already made by software: "
    "do not re-decide it. Write 2 to 3 plain sentences: what the rule requires (name the paragraph in 'ref'), "
    "what the design has, why it matters, and how to fix it. Plain text only, no lists."
)

_EXPLAIN_FIELDS = ("rule_id", "title", "regime", "ref", "quote", "element_name", "deck", "required",
                   "actual", "message", "why_rule", "fix")


def fallback_explanation(f: dict) -> str:
    parts = [f"{f.get('title', f.get('rule_id'))} ({f.get('regime', '')} {f.get('ref', '')}).",
             f"Required: {f.get('required')}. The design has: {f.get('actual')}."]
    if f.get("why_rule"):
        parts.append(str(f["why_rule"]))
    if f.get("fix"):
        parts.append(f"Fix: {f['fix']}")
    return " ".join(p for p in parts if p)


def explain_findings(client, findings: list[dict], limit: int = 8) -> list[dict]:
    chosen = [f for f in findings if f.get("status") in ("fail", "warn")][:limit]
    out = []
    for f in chosen:
        facts = {k: f.get(k) for k in _EXPLAIN_FIELDS}
        source = json.dumps(facts, ensure_ascii=False)
        item = {"rule_id": f["rule_id"], "element_id": f.get("element_id"), "status": f["status"]}
        try:
            text = client.complete(SYSTEM_EXPLAIN, source).strip()
            if not text:
                raise LLMError("empty answer")
            if not numbers_grounded(text, source):
                raise LLMError("answer contained numbers that are not in the finding")
            out.append({**item, "explanation": text, "source": client.name})
        except LLMError as exc:
            out.append({**item, "explanation": fallback_explanation(f), "source": "rules", "note": str(exc)})
    return out


# -- 2. draft a rule from regulation text ------------------------------------------------------

CHECK_KEYS = {"property", "requirement", "requirement_expr", "actual_expr", "fire_class_at_least",
              "slope_max", "equals", "equals_expr"}
APPLIES_KEYS = {"ship_types", "passenger_ship", "passengers_more_than", "keel_laid_on_or_after",
                "complete_arrangement", "persons_at_least", "length_m_at_least", "gross_tonnage_at_least",
                "operating_areas_any"}
REQUIRED_KEYS = ("id", "title", "ref", "quote", "target", "check")

SYSTEM_DRAFT = """You turn ONE regulation paragraph into draft machine-checkable rules for a ship-design checker.
Return ONLY a JSON object: {"rules": [ ... ], "unclear": "<anything you could not express, or empty>"}.
Each rule has exactly these keys:
  id        short UPPER-CASE id such as FSS-13.2.1.1-STAIR-WIDTH
  title     short title
  ref       the paragraph reference, taken from the text or the hint
  quote     an EXACT, unchanged sentence copied from the text (do not paraphrase)
  applies_if  object, optional keys only from: ship_types, passenger_ship, passengers_more_than,
            keel_laid_on_or_after (YYYY-MM-DD), length_m_at_least, gross_tonnage_at_least, operating_areas_any
  target    {"type": "<element type e.g. stair, door, corridor, nav_light, bulkhead>", "when": {"<property>": <value>}}
  check     ONE of: {"property": "<name>", "requirement": "at least 900 mm"}   (also "at most ...")
                    {"property": "<name>", "fire_class_at_least": "A-60"}
                    {"property": "<name>", "slope_max": "1:12"}
                    {"property": "<name>", "equals": "<value>"}
  severity  "major" or "minor" or "advisory"
  why       one sentence: what the rule protects
  fix       one sentence: how a designer would fix a failure
Use ONLY numbers and units that are in the text. If the paragraph cannot be expressed with these checks,
return {"rules": [], "unclear": "<why>"}. Do not invent requirements."""


def _slug(s: str) -> str:
    return re.sub(r"[^A-Z0-9.\-]+", "-", str(s).upper()).strip("-")[:80] or "DRAFT-RULE"


def validate_draft(rule: dict, source_text: str, existing_ids: set[str]) -> tuple[list[str], list[str]]:
    problems, warnings = [], []
    for k in REQUIRED_KEYS:
        if not rule.get(k):
            problems.append(f"missing '{k}'")
    quote = str(rule.get("quote", ""))
    if quote and _norm(quote) not in _norm(source_text):
        problems.append("quote is not a verbatim part of the pasted regulation text")
    check = rule.get("check")
    if isinstance(check, dict):
        bad = set(check) - CHECK_KEYS
        if bad:
            problems.append(f"unknown check keys {sorted(bad)}")
        if not (set(check) & (CHECK_KEYS - {"property"})):
            problems.append("check has no requirement")
        if "actual_expr" not in check and "property" not in check:
            problems.append("check has no 'property'")
        shown = " ".join(str(v) for v in check.values())
        extra = sorted(numbers_in(shown) - numbers_in(quote))
        if extra:
            warnings.append(f"numbers in the check that are not in the quote: {extra}; verify by hand")
    elif check:
        problems.append("'check' must be an object")
    target = rule.get("target")
    if target is not None and (not isinstance(target, dict) or not target.get("type")):
        problems.append("'target' needs a 'type'")
    ap = rule.get("applies_if") or {}
    if not isinstance(ap, dict):
        problems.append("'applies_if' must be an object")
    elif set(ap) - APPLIES_KEYS:
        problems.append(f"unknown applies_if keys {sorted(set(ap) - APPLIES_KEYS)}")
    if rule.get("id") in existing_ids:
        problems.append(f"id {rule['id']} already exists in the rule pack")
    return problems, warnings


def draft_rule(client, text: str, *, regime: str = "", ref: str = "", source_url: str = "",
               existing_ids: set[str] | None = None) -> dict:
    existing_ids = existing_ids or set()
    hint = f"Regime: {regime or 'unknown'}\nReference hint: {ref or 'none'}\n\nREGULATION TEXT:\n{text}"
    raw = parse_json_object(client.complete(SYSTEM_DRAFT, hint, json_mode=True))
    drafts = []
    for r in raw.get("rules") or []:
        if not isinstance(r, dict):
            continue
        r = {k: r.get(k) for k in ("id", "title", "ref", "quote", "applies_if", "target", "check",
                                    "severity", "why", "fix") if r.get(k) not in (None, "", {})}
        r["id"] = _slug(r.get("id", "DRAFT-RULE"))
        if ref and not r.get("ref"):
            r["ref"] = ref
        problems, warnings = validate_draft(r, text, existing_ids)
        full = {"id": r["id"], "title": r.get("title", ""), "regime": regime or "Unspecified",
                "ref": r.get("ref", ref), "quote": r.get("quote", ""), "source_url": source_url,
                "verified": False,                       # a human must tick this after checking the official text
                **{k: r[k] for k in ("applies_if", "target", "check") if k in r},
                "severity": r.get("severity") if r.get("severity") in {"major", "minor", "advisory"} else "major",
                "why": r.get("why", ""), "fix": r.get("fix", "")}
        drafts.append({"rule": full, "ok": not problems, "problems": problems, "warnings": warnings})
    ok_rules = [d["rule"] for d in drafts if d["ok"]]
    return {"model": client.name, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "drafts": drafts, "unclear": str(raw.get("unclear") or ""),
            "yaml": yaml.safe_dump(ok_rules, sort_keys=False, allow_unicode=True, width=100) if ok_rules else "",
            "note": "DRAFT ONLY. Check against the official text, set verified: true, then add to ruletrace/rules/*.yaml "
                    "with a labelled sample case and run the tests."}


# -- 3. grounded question answering ------------------------------------------------------------

_STOP = set("a an the of to is are in on for and or what which does do how why when where with by be it this that "
            "my our should must shall can i we you".split())


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP and len(t) > 1}


def retrieve_rules(question: str, rules: list[dict], k: int = 5) -> list[dict]:
    q = _tokens(question)
    scored = []
    for r in rules:
        score = 3 * len(q & _tokens(r.get("title", ""))) + 2 * len(q & _tokens(f"{r.get('regime', '')} {r.get('ref', '')} {r.get('id', '')}")) \
            + len(q & _tokens(f"{r.get('quote', '')} {r.get('why', '')}"))
        if score:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:k]]


SYSTEM_ASK = (
    "You are an assistant for ship-design engineers using a compliance checker. Answer ONLY from the CONTEXT "
    "(rules and findings from the checker). Cite every claim with the rule id in square brackets, e.g. "
    "[COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT]. If the context does not answer the question, say: "
    "'The loaded rules do not cover this.' Never invent regulation text, paragraph numbers or numbers. "
    "Keep it short. You do not decide compliance; the findings in the context already did."
)


def answer_question(client, question: str, rules: list[dict], findings: list[dict] | None = None) -> dict:
    hits = retrieve_rules(question, rules)
    fails = [f for f in (findings or []) if f.get("status") in ("fail", "warn")]
    if not hits and not fails:
        return {"answer": "The loaded rules do not cover this.", "citations": [], "unsupported_citations": [],
                "warnings": [], "model": None}
    ids = {r["id"] for r in hits} | {f["rule_id"] for f in fails}
    ctx = [f"[{r['id']}] {r.get('regime', '')} {r.get('ref', '')}: \"{r.get('quote', '')}\" "
           f"(why: {r.get('why', '')}) (verified: {r.get('verified')})" for r in hits]
    ctx += [f"FINDING [{f['rule_id']}] {f['status']} on {f.get('element_name')}: {f.get('message')} "
            f"(required {f.get('required')}, actual {f.get('actual')}). Fix: {f.get('fix')}" for f in fails[:10]]
    context = "\n".join(ctx)
    answer = client.complete(SYSTEM_ASK, f"CONTEXT:\n{context}\n\nQUESTION: {question}").strip()
    cited = list(dict.fromkeys(re.findall(r"\[([A-Za-z0-9._\-]+)\]", answer)))
    valid = [c for c in cited if c in ids]
    by_id = {r["id"]: r for r in rules}
    warnings = []
    if not valid and answer != "The loaded rules do not cover this.":
        warnings.append("the answer cites no rule from the checker; treat it as unverified")
    if not numbers_grounded(answer, context + " " + question):
        warnings.append("the answer contains numbers that are not in the retrieved rules or findings")
    return {"answer": answer, "model": client.name, "warnings": warnings,
            "unsupported_citations": [c for c in cited if c not in ids],
            "citations": [{"rule_id": c, "regime": by_id[c].get("regime"), "ref": by_id[c].get("ref"),
                           "source_url": by_id[c].get("source_url"), "verified": by_id[c].get("verified")}
                          for c in valid if c in by_id]}
