"""Question -> retrieved clauses -> LLM answer -> guardrails -> verified answer, excerpts, or 'not found'.

Code decides what is shown:
* retrieval confidence "none"           -> "Not found in the provided rules" + closest clauses (no LLM call)
* LLM replies NOT_FOUND on a weak match -> same (on a confident match or a named clause: excerpts instead)
* answer passes every guardrail         -> status "verified"
* fails twice / LLM down or too slow    -> status "excerpts": the matching clause text, labelled as excerpts
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Literal

from pydantic import BaseModel

from .config import CLAUSES_FILE, ROOT, CACHE_DIR
from .guardrails import check_answer
from .ingest.parsers import NOT_A_VALUE
from .llm import LLMError, get_llm
from .models import Clause
from .retrieval import Hit, RetrievalResult, Retriever, get_retriever

CACHE_FILE = CACHE_DIR / "answers.json"
MAX_CONTEXT = 12
NOT_FOUND_TEXT = "Not found in the provided rules."

SYSTEM_PROMPT = """You are RuleMate, an assistant for ship designers. You answer questions about design rules \
using ONLY the rule clauses given to you. You never use outside knowledge.

Rules for your answer:
1. Every sentence must contain at least one citation in the exact form [doc § clause], copied exactly from the \
header line of the clause that supports it. Put the citation at the end of the sentence, before the full stop.
2. Cite only clauses from the list you are given.
3. Copy every number and unit exactly as written in the clause you cite. Do not convert units, round, or calculate new numbers.
4. If you quote, quote word for word and put the quote in double quotes.
5. Do not guess. Never use words like "probably", "typically", "usually", "likely", "generally".
6. State conditions and exceptions that the clauses give (for example "provided that ...").
7. If a clause refers to another clause that is in the list (for example "calculated in accordance with 4.2"), also state what the referenced clause requires, and cite it.
8. If the clauses do not answer the question, reply with exactly: NOT_FOUND
9. Be concise: 1 to 5 sentences, or a short bullet list. No preamble, no closing remarks."""


class Citation(BaseModel):
    doc: str
    clause_id: str
    uid: str
    title: str = ""
    page: int | None = None
    synthetic: bool = False


class Answer(BaseModel):
    question: str
    status: Literal["verified", "excerpts", "not_found"]
    text: str
    citations: list[Citation] = []
    sources: list[Hit] = []
    confidence: str = ""
    top_score: float = 0.0
    attempts: int = 0
    violations: list[str] = []  # from the last failed attempt (empty when verified)
    rejected_attempts: int = 0  # how many LLM drafts the guardrails rejected
    note: str = ""
    model: str = ""
    cached: bool = False
    latency_ms: float = 0.0
    synthetic: bool = False

    @property
    def badge(self) -> str:
        return {"verified": "verified", "excerpts": "excerpts only", "not_found": "not found"}[self.status]


# ------------------------------------------------------------------ prompt


def context_clauses(res: RetrievalResult) -> list[Hit]:
    order = {"id": 0, "child": 1, "search": 2, "xref": 3}
    hits = [h for h in res.hits if h.clause.kind != "heading" or h.source == "id"]
    hits.sort(key=lambda h: order[h.source])  # stable: keeps rank order within each group
    return hits[:MAX_CONTEXT]


def format_context(hits: list[Hit]) -> str:
    blocks = []
    shown = {h.clause.clause_id for h in hits}
    for h in hits:
        c = h.clause
        meta = [c.title] if c.title else []
        if c.page:
            meta.append(f"page {c.page}")
        if h.source == "xref" and h.via:
            meta.append(f"referenced by {h.via}")
        visible = [r for r in c.refs if r in shown]  # only point at clauses the model can see and cite
        blocks.append(f"{c.citation} {' | '.join(meta)}\n{expand_refs(c.text, visible)}")
    return "\n\n".join(blocks)


def expand_refs(text: str, refs: list[str]) -> str:
    """'... given in 3.1.1.' -> '... given in 3.1.1 (= II-2/13.3.1.1).' so the model can cite the right clause."""
    for ref in refs:
        local = ref.split("/")[-1].split(" [")[-1].rstrip("]")
        parts = local.split(".")
        forms = sorted({local} | {".".join(parts[k:]) for k in range(1, len(parts) - 1)}, key=len, reverse=True)
        for f in forms:
            if f == ref or "." not in f:
                continue
            m = re.search(rf"(?<![\w.\[]){re.escape(f)}(?![\w]|\.\d){NOT_A_VALUE}", text)
            if m:
                text = f"{text[:m.end()]} (= {ref}){text[m.end():]}"
                break
        else:
            t = re.search(rf"\b{re.escape(ref.split(' ', 1)[-1])}\b", text) if " Table " in ref else None
            if t and ref not in text:
                text = f"{text[:t.end()]} (= {ref}){text[t.end():]}"
    return text


def build_messages(question: str, hits: list[Hit], confidence: str) -> list[dict]:
    caution = (
        "\nThe search found only a weak match. Check carefully whether these clauses really answer the question; "
        "if they do not, reply NOT_FOUND."
        if confidence == "low"
        else ""
    )
    user = f"Rule clauses:\n\n{format_context(hits)}\n\nQuestion: {question}{caution}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def is_not_found(text: str) -> bool:
    return bool(re.match(r"^\W*NOT[_ ]FOUND\b", text.strip(), re.I))


def strip_reasoning(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


# ------------------------------------------------------------------ fallbacks


def _citations(clauses: list[Clause]) -> list[Citation]:
    return [
        Citation(doc=c.doc, clause_id=c.clause_id, uid=c.uid, title=c.title, page=c.page, synthetic=c.synthetic)
        for c in clauses
    ]


def not_found_answer(question: str, res: RetrievalResult, note: str = "") -> Answer:
    closest = [h for h in res.hits if h.source == "search"][:3]
    lines = [NOT_FOUND_TEXT]
    if closest:
        lines += ["", "Closest clauses (they do not answer the question):"]
        lines += [f"- {h.clause.citation} {h.clause.title}".rstrip() for h in closest]
    return Answer(
        question=question,
        status="not_found",
        text="\n".join(lines),
        sources=res.hits,
        confidence=res.confidence,
        top_score=res.top_score,
        note=note,
        synthetic=any(h.clause.synthetic for h in closest),
    )


def excerpts_answer(question: str, res: RetrievalResult, hits: list[Hit], note: str, **kw) -> Answer:
    primary = [h for h in hits if h.source != "xref" and h.clause.kind != "heading"][:4]
    primary += [h for h in hits if h.source == "xref" and h.clause.kind != "heading"][:2]
    parts = [f"**Excerpts from the matching clauses** - not a generated answer ({note})."]
    for h in primary:
        c = h.clause
        page = f", p. {c.page}" if c.page else ""
        quoted = "\n".join(f"> {line}" for line in c.text.splitlines())
        parts.append(f"{c.citation} {c.title}{page}\n{quoted}")
    clauses = [h.clause for h in primary]
    return Answer(
        question=question,
        status="excerpts",
        text="\n\n".join(parts),
        citations=_citations(clauses),
        sources=res.hits,
        confidence=res.confidence,
        top_score=res.top_score,
        note=note,
        synthetic=any(c.synthetic for c in clauses),
        **kw,
    )


# ------------------------------------------------------------------ cache


def _index_digest() -> str:
    return hashlib.sha1(CLAUSES_FILE.read_bytes()).hexdigest()[:12] if CLAUSES_FILE.exists() else "none"


def _cache_key(question: str, model: str) -> str:
    q = re.sub(r"\s+", " ", question.strip().lower()).rstrip("?. ")
    return hashlib.sha1(f"{q}|{model}|{_index_digest()}".encode()).hexdigest()


def _cache_load() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _cache_save(key: str, ans: Answer) -> None:
    data = _cache_load()
    data[key] = ans.model_dump(mode="json")
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, indent=1))


# ------------------------------------------------------------------ main entry


def answer_question(
    question: str,
    retriever: Retriever | None = None,
    llm=None,
    use_cache: bool = True,
    max_attempts: int = 2,
    docs: list[str] | None = None,
) -> Answer:
    t0 = time.perf_counter()
    retriever = retriever or get_retriever()
    llm = llm or get_llm()

    def done(ans: Answer) -> Answer:
        ans.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return ans

    key = _cache_key(question, llm.label + (f"|{','.join(sorted(docs))}" if docs else ""))
    if use_cache:
        hit = _cache_load().get(key)
        if hit:
            ans = Answer.model_validate(hit)
            ans.cached = True
            return done(ans)

    res = retriever.search(question, docs=docs)
    if not res.found:
        return done(not_found_answer(question, res))

    hits = context_clauses(res)
    retrieved = [h.clause for h in hits]
    messages = build_messages(question, hits, res.confidence)

    last_violations: list[str] = []
    rejected = 0
    for attempt in range(1, max_attempts + 1):
        try:
            raw = strip_reasoning(llm.complete(messages, max_tokens=1500))
        except LLMError as e:
            return done(excerpts_answer(question, res, hits, f"LLM unavailable: {str(e)[:120]}",
                                        attempts=attempt - 1, rejected_attempts=rejected,
                                        violations=last_violations))
        model = llm.label
        if is_not_found(raw) and (res.confidence == "high" or any(h.source == "id" for h in res.hits)):
            # retrieval is confident (or the question names an existing clause): a refusal from the model is not
            # enough to tell the user "not found in the rules"; show the relevant clauses instead
            note = ("the model gave no answer; showing the requested clause" if any(h.source == "id" for h in res.hits)
                    else "the model gave no answer; showing the most relevant clauses")
            return done(excerpts_answer(question, res, hits, note, attempts=attempt, rejected_attempts=rejected,
                                        model=model))
        if is_not_found(raw):
            ans = not_found_answer(question, res, note="The model found no answer in the retrieved clauses.")
            ans.attempts, ans.model, ans.rejected_attempts = attempt, model, rejected
            if use_cache:
                _cache_save(key, ans)
            return done(ans)

        check = check_answer(raw, retrieved)
        if check.ok:
            ans = Answer(
                question=question,
                status="verified",
                text=check.text,
                citations=_citations(check.cited),
                sources=res.hits,
                confidence=res.confidence,
                top_score=res.top_score,
                attempts=attempt,
                rejected_attempts=rejected,
                model=model,
                synthetic=any(c.synthetic for c in check.cited),
            )
            if use_cache:
                _cache_save(key, ans)
            return done(ans)

        rejected += 1
        last_violations = [str(v) for v in check.violations]
        messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "Your answer failed these automatic checks:\n- "
                + "\n- ".join(last_violations)
                + "\n\nRewrite the whole answer and fix every problem. Follow all the rules. Valid citations are only: "
                + ", ".join(c.citation for c in retrieved)
                + "\nIf the clauses do not answer the question, reply with exactly: NOT_FOUND",
            },
        ]

    return done(
        excerpts_answer(question, res, hits, "the generated answer did not pass verification",
                        attempts=max_attempts, rejected_attempts=rejected, violations=last_violations,
                        model=llm.label)
    )
