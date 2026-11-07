"""Hybrid retrieval: exact clause-ID lookup + BM25 + dense embeddings, fused with RRF,
then one hop of cross-reference following.

Embeddings are cached on disk (data/index/embeddings.npy + embeddings.json) and
rebuilt only when clauses.json changes. If the embedding model cannot be loaded
(offline, no cache) retrieval falls back to BM25 + ID lookup and says so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from pydantic import BaseModel
from rank_bm25 import BM25Okapi

from .config import CLAUSES_FILE, EMBED_MODEL, INDEX_DIR
from .ingest.parsers import NOT_A_VALUE
from .ingest.pipeline import load_index
from .lookup import find_clause
from .models import Clause

log = logging.getLogger(__name__)

RRF_K = 60
# Confidence bands on the best cosine similarity (bge-small), calibrated on the eval set:
#   >= HIGH  -> "high": answer normally
#   >= LOW   -> "low":  LLM may answer, but is told to say "not found" if the clauses do not cover it
#   <  LOW   -> "none": code answers "Not found in the provided rules" without calling the LLM
HIGH_SCORE = float(os.getenv("RULEMATE_HIGH_SCORE", "0.70"))
LOW_SCORE = float(os.getenv("RULEMATE_LOW_SCORE", "0.60"))
MAX_XREF = 8  # cap on clauses added by following cross-references
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

STOPWORDS = set(
    "a an the of to in on for and or is are be by with what which how does do shall must should "
    "as at it its this that from than not no any all may can i we you there their per".split()
)


# words that do not add meaning to "what does 13.3.1 say"
ID_FILLER = set("say says said mean means state states tell me about show text requirement requirements "
                "regulation reg clause paragraph section table rule rules content contain contains explain explains "
                "describe describes summarize summarise summary require requires required cover covers what's in".split())


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+(?:[./-][a-z0-9]+)*", text.lower())
    out = []
    for t in toks:
        if t in STOPWORDS:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss") and t.isalpha():
            t = t[:-1]
        out.append(t)
    return out


def clause_search_text(c: Clause) -> str:
    """What gets indexed: breadcrumb + title + text, so 'stairway' in a heading helps its children."""
    crumbs = " > ".join(p.split(" ", 1)[-1] for p in c.path)
    return f"{crumbs}. {c.title}. {c.text}"


# --------------------------------------------------------------------- ID detection

_ID_PATTERNS = [
    rf"\b(?:Pt\.?\s*\d+\s*Ch\.?\s*\d+\s*)?Sec\.?\s*\d+\s*\[\d+(?:\.\d+)*\]",
    r"\[\d+(?:\.\d+)*\]",
    rf"\b[IVX]+-\d+/\d+(?:\.\d+)*{NOT_A_VALUE}",
    rf"\b(?:reg(?:ulation)?\.?|clause|paragraph|section|§)\s*\d+(?:\.\d+)*{NOT_A_VALUE}",
    r"\bTable\s+[A-Z]?\d+(?:\.\d+)*",
    r"(?<![\w.])\d+\.\d+-\d+(?:\([a-z]\))?",  # CFR section / paragraph: 190.10-25(b)
    rf"(?<![\w.])\d+(?:\.\d+){{1,6}}{NOT_A_VALUE}",
]
_ID_RE = re.compile("|".join(f"(?:{p})" for p in _ID_PATTERNS), re.I)


# MLC 2006: "Standard A3.1, paragraph 6(a)", "A3.1/6(a)", "Guideline B3.1.5", "Regulation 3.1", "Article V"
_MLC_ID = re.compile(
    r"\b(?:(?:Standard|Guideline)\s+)?(?P<code>[AB]\d{1,2}(?:\.\d{1,2}){1,3})(?:\s*(?:,\s*)?(?:/|\bpara(?:graph)?\.?\s*)"
    r"(?P<p>\d{1,2})(?:\s*\((?P<l>[a-z])\))?)?(?![\w.])"
    r"|\bRegulation\s+(?P<reg>\d(?:\.\d{1,2}){1,2})(?:,?\s*para(?:graph)?\.?\s*(?P<rp>\d{1,2}))?(?![\w.])"
    r"|\bArticle\s+(?P<art>[IVX]{1,5})\b(?:,?\s*para(?:graph)?\.?\s*(?P<ap>\d{1,2}))?", re.I)


def extract_clause_ids(query: str) -> list[str]:
    out = []
    for m in _MLC_ID.finditer(query):
        if m.group("code"):
            cid = m.group("code").upper() + (f"/{m.group('p')}" if m.group("p") else "") + (
                f"({m.group('l').lower()})" if m.group("l") else "")
        elif m.group("reg"):
            cid = f"R{m.group('reg')}" + (f"/{m.group('rp')}" if m.group("rp") else "")
        else:
            cid = f"Art.{m.group('art').upper()}" + (f"/{m.group('ap')}" if m.group("ap") else "")
        out.append(cid)
    # the words of an MLC reference are used up: "Standard A3.1 paragraph 12" must not also look up "paragraph 12"
    rest = _MLC_ID.sub(" ", query) if out else query
    return out + [m.group(0).strip() for m in _ID_RE.finditer(rest)]


# --------------------------------------------------------------------- results


class Hit(BaseModel):
    clause: Clause
    score: float  # fused RRF score (1.0 for exact ID matches)
    source: Literal["id", "search", "xref", "child"]
    cosine: float | None = None
    bm25_rank: int | None = None
    dense_rank: int | None = None
    via: str | None = None  # for xref/child hits: the clause_id that led here


class RetrievalResult(BaseModel):
    query: str
    hits: list[Hit]
    found: bool  # False -> answer must be "Not found in the provided rules"
    confidence: Literal["high", "low", "none"]
    top_score: float  # best cosine similarity (or 1.0 for exact ID match)
    mode: str  # "hybrid" | "bm25-only"
    id_queries: list[str] = []

    def cited_uids(self) -> set[str]:
        return {h.clause.uid for h in self.hits}


# --------------------------------------------------------------------- retriever


@dataclass
class Retriever:
    clauses: list[Clause]
    use_dense: bool = True
    model_name: str = EMBED_MODEL
    embeddings: np.ndarray | None = None
    mode: str = "bm25-only"
    _model: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Headings are reachable via ID lookup and child expansion, but are too
        # short to rank well, so only clause/table/section bodies are searched.
        self.searchable = [i for i, c in enumerate(self.clauses) if c.kind != "heading"]
        self.by_uid = {c.uid: c for c in self.clauses}
        self.children: dict[str, list[Clause]] = {}
        for c in self.clauses:
            if c.parent_id:
                self.children.setdefault(f"{c.doc}::{c.parent_id}", []).append(c)
        self.bm25 = BM25Okapi([tokenize(clause_search_text(self.clauses[i])) for i in self.searchable])
        if self.use_dense:
            try:
                self._load_dense()
                self.mode = "hybrid"
            except Exception as e:  # offline, model missing, etc.
                log.warning("Dense retrieval disabled (%s); using BM25 only.", e)

    # -- embeddings ---------------------------------------------------------
    def _model_obj(self):
        if self._model is None:
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
            from sentence_transformers import SentenceTransformer

            try:  # use the local cache without a network round trip
                self._model = SentenceTransformer(self.model_name, local_files_only=True)
            except Exception:
                self._model = SentenceTransformer(self.model_name)
        return self._model

    def _load_dense(self) -> None:
        texts = [clause_search_text(self.clauses[i]) for i in self.searchable]
        digest = hashlib.sha1(("\n".join(texts) + self.model_name).encode()).hexdigest()
        emb_file, meta_file = INDEX_DIR / "embeddings.npy", INDEX_DIR / "embeddings.json"
        if emb_file.exists() and meta_file.exists():
            meta = json.loads(meta_file.read_text())
            if meta.get("digest") == digest:
                self.embeddings = np.load(emb_file)
                return
        self.embeddings = np.asarray(
            self._model_obj().encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False),
            dtype=np.float32,
        )
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.save(emb_file, self.embeddings)
        meta_file.write_text(json.dumps({"digest": digest, "model": self.model_name, "n": len(texts)}))

    def _embed_query(self, q: str) -> np.ndarray:
        prefix = BGE_QUERY_PREFIX if "bge" in self.model_name.lower() else ""
        return np.asarray(self._model_obj().encode([prefix + q], normalize_embeddings=True)[0], dtype=np.float32)

    # -- search -------------------------------------------------------------
    def search(self, query: str, k: int = 5, follow_refs: bool = True, pool: int = 30,
               docs: list[str] | None = None) -> RetrievalResult:
        """``docs``: only search these rule documents (the rule sets that apply to the vessel); None = all."""
        hits: list[Hit] = []
        allowed = set(docs) if docs else None
        clauses = [c for c in self.clauses if c.doc in allowed] if allowed else self.clauses
        seen: set[str] = set()

        def add(h: Hit) -> None:
            if h.clause.uid not in seen:
                seen.add(h.clause.uid)
                hits.append(h)

        # 1. exact clause IDs mentioned in the question
        id_queries = extract_clause_ids(query)
        for qid in id_queries:
            for c in find_clause(clauses, qid)[:5]:
                add(Hit(clause=c, score=1.0, source="id", cosine=1.0))
                for child in self._descendants(c)[:8]:
                    add(Hit(clause=child, score=0.9, source="child", via=c.clause_id))

        # 2. hybrid ranking (skipped when the question is only "what does <ID> say")
        residual = _ID_RE.sub(" ", query)
        if id_queries and not [t for t in tokenize(residual) if t not in ID_FILLER]:
            if follow_refs:
                self._follow_refs(hits, add)
            return RetrievalResult(query=query, hits=hits, found=bool(hits), confidence="high" if hits else "none",
                                   top_score=1.0 if hits else 0.0, mode=self.mode, id_queries=id_queries)
        bm = self.bm25.get_scores(tokenize(query))
        mask = None
        if allowed:
            mask = np.array([self.clauses[i].doc in allowed for i in self.searchable])
            bm = np.where(mask, bm, 0.0)
        bm_order = [j for j in np.argsort(-bm)[:pool] if bm[j] > 0]
        bm_rank = {j: r for r, j in enumerate(bm_order)}
        dense_rank: dict[int, int] = {}
        cos = None
        if self.embeddings is not None:
            cos = self.embeddings @ self._embed_query(query)
            if mask is not None:
                cos = np.where(mask, cos, -1.0)
            dense_rank = {j: r for r, j in enumerate(np.argsort(-cos)[:pool])}

        fused: dict[int, float] = {}
        for ranks in (bm_rank, dense_rank):
            for j, r in ranks.items():
                fused[j] = fused.get(j, 0.0) + 1.0 / (RRF_K + r + 1)
        ranked = sorted(fused, key=lambda j: -fused[j])
        norm = 2.0 / (RRF_K + 1) if dense_rank else 1.0 / (RRF_K + 1)  # max possible fused score

        top_cos = float(cos.max()) if cos is not None else None
        for j in ranked:
            if sum(h.source == "search" for h in hits) >= k:
                break
            add(
                Hit(
                    clause=self.clauses[self.searchable[j]],
                    score=round(fused[j] / norm, 4),
                    source="search",
                    cosine=round(float(cos[j]), 4) if cos is not None else None,
                    bm25_rank=bm_rank.get(j),
                    dense_rank=dense_rank.get(j),
                )
            )

        # 3. follow cross-references one hop from every primary hit
        if follow_refs:
            self._follow_refs(hits, add)

        if any(h.source == "id" for h in hits):
            conf, top = "high", 1.0
        elif top_cos is not None:
            top = top_cos
            conf = "high" if top >= HIGH_SCORE else "low" if top >= LOW_SCORE else "none"
        else:  # BM25-only fallback: lexical score bands
            top = float(bm.max()) if len(bm) else 0.0
            conf = "high" if top >= 8.0 else "low" if top >= 3.0 else "none"
        return RetrievalResult(query=query, hits=hits, found=conf != "none", confidence=conf,
                               top_score=round(top, 4), mode=self.mode, id_queries=id_queries)

    def _follow_refs(self, hits: list[Hit], add) -> None:
        """One hop: ID matches and the top 3 search hits pull in the clauses they reference."""
        sources = [h for h in hits if h.source in ("id", "child")]
        sources += [h for h in hits if h.source == "search"][:3]
        n = 0
        for h in sources:
            for ref in h.clause.refs:
                target = self.by_uid.get(f"{h.clause.doc}::{ref}")
                if target is None or n >= MAX_XREF:
                    continue
                before = len(hits)
                add(Hit(clause=target, score=0.0, source="xref", via=h.clause.clause_id))
                for child in self._descendants(target)[:4]:
                    add(Hit(clause=child, score=0.0, source="xref", via=target.clause_id))
                n += len(hits) - before

    def _descendants(self, c: Clause) -> list[Clause]:
        out, stack = [], list(self.children.get(c.uid, []))
        while stack:
            x = stack.pop(0)
            out.append(x)
            stack[0:0] = self.children.get(x.uid, [])
        return out

    def get(self, doc: str, clause_id: str) -> Clause | None:
        return self.by_uid.get(f"{doc}::{clause_id}")


_cached: Retriever | None = None


def get_retriever(use_dense: bool = True) -> Retriever:
    """Process-wide retriever, reloaded when clauses.json changes."""
    global _cached
    mtime = CLAUSES_FILE.stat().st_mtime
    if _cached is None or getattr(_cached, "_mtime", None) != mtime or _cached.use_dense != use_dense:
        _cached = Retriever(load_index().clauses, use_dense=use_dense)
        _cached._mtime = mtime  # type: ignore[attr-defined]
    return _cached
