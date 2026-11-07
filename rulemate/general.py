"""Answers to questions the loaded rule texts do not cover (general ship design, how-to, definitions...).

Kept strictly apart from rule answers. Safety comes first, so a general answer is never presented as fact:
  * it is labelled "general knowledge - not from the loaded rules, not verified" and never gives a PASS / FAIL;
  * the model must name its sources; code then checks every source it names:
      - a clause of a LOADED text (e.g. 46 CFR 190.10-1, ADA 404.2.3): looked up in the index; it must exist, and
        every number it is cited for must appear in that clause: the sentence is shown (a number can belong to
        another requirement of the same clause, so the engineer judges it); a missing number is flagged;
      - a known instrument that is not loaded (SOLAS, MLC 2006, LSA Code, class rules, ISO ...): shown as "named
        source, not loaded: open it and confirm"; never as verified;
      - anything else: "could not be identified", i.e. treat the statement as unsupported;
      - links are kept only on official domains (IMO, ILO, eCFR, EUR-Lex, classification societies); never fetched;
  * every number in the answer that no loaded clause confirms is listed as "not verified".
Only the bot providers are used (Groq, then the local Ollama model); Gemini is kept for reading drawings.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

from .llm import LLMError, get_llm

LABEL = "General knowledge - not from the loaded rules, not verified"

PROMPT = """You are RuleMate, an assistant for ship designers (interior designers and engineers) at a shipyard.
The question below is NOT answered by the rule texts loaded in the tool, so answer from general knowledge.
People's safety depends on what you say, so:
- Answer clearly and briefly (at most 6 sentences or a short list), in plain language a non-expert understands.
- If the question is about a requirement, name the instrument that governs it (e.g. SOLAS Chapter II-2
  Regulation 13, MLC 2006 Regulation 3.1, the LSA Code, class rules) and say the exact value must be confirmed
  there. Never state that a design passes or fails.
- Only give a number (dimension, count, date) if you are certain of it AND you name the exact source it comes from.
- Never invent a regulation, clause number, value, quote or link. If you are not sure, say so plainly.
- For every reference give the instrument and the exact part (chapter, regulation, paragraph). Give a link only if
  it is the official page (imo.org, ilo.org, ecfr.gov, eur-lex.europa.eu, a classification society's site).
Return only JSON: {"answer": "...", "references": [{"source": "SOLAS Chapter II-2, Regulation 13.3",
"supports": "what this source is cited for, with any number exactly as in the answer", "url": "" }],
"certainty": "high | medium | low"}"""

BOT_PROVIDERS = "groq,ollama"

# instruments a shipyard engineer can look up; anything else is "not identified"
KNOWN = [
    (r"\bSOLAS\b", "SOLAS (IMO)"), (r"\bMARPOL\b", "MARPOL (IMO)"), (r"\bMLC\b|Maritime Labour Convention", "MLC 2006 (ILO)"),
    (r"\bCOLREG", "COLREG (IMO)"), (r"Load ?Lines?|\bICLL\b", "Load Line Convention (IMO)"),
    (r"\bLSA Code\b|Life-Saving Appliances", "LSA Code (IMO)"), (r"\bFSS Code\b|Fire Safety Systems", "FSS Code (IMO)"),
    (r"\bFTP Code\b|Fire Test Procedures", "FTP Code (IMO)"), (r"\bHSC Code\b", "HSC Code (IMO)"),
    (r"\bIS Code\b|Intact Stability", "IS Code (IMO)"), (r"\bISM Code\b", "ISM Code (IMO)"),
    (r"\bISPS\b", "ISPS Code (IMO)"), (r"\bSTCW\b", "STCW (IMO)"), (r"\bTonnage\b", "Tonnage Convention (IMO)"),
    (r"\bIMO\b|\bMSC[ ./]|\bMEPC\b|\bA\.\d+", "IMO resolution or circular"), (r"\bILO\b", "ILO instrument"),
    (r"\bISO\s?\d+", "ISO standard"), (r"\bIEC\s?\d+", "IEC standard"), (r"\bEN\s?\d+", "European standard"),
    (r"\bCFR\b|Code of Federal Regulations", "US Code of Federal Regulations"),
    (r"\bADA\b|ABA\b|Americans with Disabilities", "ADA / ABA standards (US)"),
    (r"\bDNV\b|Lloyd'?s Register|\bLR\b|\bABS\b|Bureau Veritas|\bBV\b|ClassNK|\bRINA\b|\bKR\b|\bIACS\b",
     "Classification society rules"),
    (r"Directive|Regulation \(EU\)|\bEU\b", "EU legislation"), (r"\bUSCG\b|Coast Guard|NVIC", "US Coast Guard"),
    (r"Traficom|Finnish", "Finnish authority"), (r"\bPVAG\b|Passenger Vessel Accessibility", "PVAG (US Access Board)"),
]
OFFICIAL_DOMAINS = ("imo.org", "ilo.org", "ecfr.gov", "govinfo.gov", "access-board.gov", "eur-lex.europa.eu",
                    "europa.eu", "uscg.mil", "dnv.com", "lr.org", "eagle.org", "bureauveritas.com",
                    "veristar.com", "classnk.or.jp", "iacs.org.uk", "iso.org", "traficom.fi")
# clause numbers of the loaded texts
_CFR = re.compile(r"(?:46\s*CFR\s*)?\b(190\.\d{2}(?:-\d+)?)\b", re.I)
_ADA = re.compile(r"(?:ADA|ABA|36\s*CFR\s*1191|Appendix D|§)\D{0,20}?\b([2-9]\d{2}(?:\.\d+){0,4})\b", re.I)
_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?:\s?(?:mm|cm|m|in|inches|ft|feet|deg|degrees|°|%|persons|kN|kg|t))?\b")


def bot_provider() -> str:
    """Providers for the chat bot: Groq and Ollama only. Gemini is never used for answers."""
    spec = os.getenv("COPILOT_LLM_PROVIDER", BOT_PROVIDERS)
    names = [n.strip() for n in spec.split(",") if n.strip() and n.strip().lower() != "gemini"]
    return ",".join(names) or BOT_PROVIDERS


def _parse(raw: str) -> dict:
    from .answer import strip_reasoning

    raw = strip_reasoning(raw).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and str(d.get("answer") or "").strip():
                return d
        except ValueError:
            pass
    return {"answer": raw, "references": [], "certainty": "low"}


def _numbers(text: str) -> list[str]:
    out = []
    for m in _NUM.finditer(text or ""):
        n = m.group(0).strip()
        if re.fullmatch(r"(19|20)\d{2}", n) or re.fullmatch(r"\d", n):  # years and list numbers are not values
            continue
        if n not in out:
            out.append(n)
    return out


def _digits(n: str) -> str:
    return re.match(r"\d+(?:[.,]\d+)?", n).group(0).replace(",", ".")


def _loaded_clause(source: str, retriever):
    """A clause of a loaded text that the source names, if it exists in the index."""
    if retriever is None:
        return None
    for rx, doc in ((_CFR, "46CFR190"), (_ADA, "36CFR1191AppD")):
        for m in rx.finditer(source):
            c = retriever.by_uid.get(f"{doc}::{m.group(1)}")
            if c is not None and (doc != "36CFR1191AppD" or re.search(r"ADA|ABA|1191|Appendix D|§", source, re.I)):
                return c
    return None


def check_references(answer: str, refs: list, retriever=None) -> tuple[list[dict], list[str]]:
    """Each named source -> status; numbers of the answer that no loaded clause confirms."""
    out, confirmed_nums = [], set()
    for r in refs if isinstance(refs, list) else []:
        if not isinstance(r, dict):
            continue
        src = " ".join(str(r.get("source") or "").split())[:200]
        if not src:
            continue
        supports = " ".join(str(r.get("supports") or "").split())[:300]
        url = str(r.get("url") or "").strip()
        host = (urlparse(url).hostname or "") if url.startswith(("http://", "https://")) else ""
        official = bool(host) and any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS)
        item = {"source": src, "supports": supports, "url": url if official else "",
                "url_dropped": bool(url) and not official}
        clause = _loaded_clause(src, retriever)
        if clause is not None:
            # a number "found" in a clause may belong to another requirement of the same clause, so the sentence
            # it stands in is shown for the engineer to judge; nothing in a general answer is called verified
            sentences = re.split(r"(?<=[.;])\s+", clause.text)
            found, missing = [], []
            for n in _numbers(supports):
                d = re.escape(_digits(n))
                hit = next((x for x in sentences if re.search(rf"(?<![\d.]){d}(?![\d])", x.replace(",", "."))), None)
                (found.append((n, hit.strip()[:240])) if hit else missing.append(n))
            confirmed_nums.update(_digits(n) for n, _ in found)
            item.update(uid=clause.uid,
                        status="number NOT in this clause" if missing else "loaded clause: read the sentence",
                        evidence=[{"number": n, "sentence": x} for n, x in found],
                        why=(f"{clause.doc} § {clause.clause_id} exists in the loaded rules"
                             + (f"; the clause says: \"{found[0][1]}\"" if found else "")
                             + (f"; NOT in this clause: {', '.join(missing)}" if missing else "")))
        else:
            kind = next((name for rx, name in KNOWN if re.search(rx, src, re.I)), None)
            item.update(uid="", status="named source, not loaded" if kind else "not identified",
                        why=(f"{kind}: not loaded in RuleMate, so not checked. Open it and confirm before use."
                             if kind else "This source could not be identified: treat the statement as unsupported."))
        out.append(item)
    unverified = [n for n in _numbers(answer) if _digits(n) not in confirmed_nums]
    return out, unverified


def general_answer(question: str, llm=None, retriever=None) -> dict:
    llm = llm or get_llm(bot_provider())
    try:
        raw = llm.complete([{"role": "system", "content": PROMPT}, {"role": "user", "content": question}],
                           temperature=0.0, max_tokens=1500)
    except LLMError as e:
        return {"ok": False, "label": LABEL, "text": "", "model": "", "references": [], "unverified_numbers": [],
                "error": f"No bot model answered (Groq / Ollama): {str(e)[:160]}"}
    d = _parse(raw)
    text = str(d.get("answer") or "").strip()
    refs, unverified = check_references(text, d.get("references") or [], retriever)
    certainty = str(d.get("certainty") or "low").lower()
    warnings = []
    if not refs:
        warnings.append("The model named no source for this answer: do not rely on it.")
    if any(r["status"] == "not identified" for r in refs):
        warnings.append("At least one named source could not be identified.")
    if any(r["status"] == "number NOT in this clause" for r in refs):
        warnings.append("A number is attributed to a loaded clause that does not contain it: the answer is wrong there.")
    if unverified:
        warnings.append(f"Numbers not verified against any loaded text: {', '.join(unverified[:8])}. Confirm each in "
                        "the named source.")
    return {"ok": True, "label": LABEL, "text": text, "model": llm.label, "references": refs,
            "unverified_numbers": unverified, "certainty": certainty if certainty in ("high", "medium", "low") else "low",
            "warnings": warnings}
