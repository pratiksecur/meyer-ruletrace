"""FastAPI backend.

    uvicorn rulemate.api:app --port 8000

GET  /health               index + model status
POST /ask                  question -> verified answer / excerpts / not found
GET  /search?q=...         raw retrieval result (for debugging and the Sources panel)
GET  /clause/{clause_id}   clause text by ID (IDs may contain "/", spaces and brackets)
POST /check                design rows (JSON) -> PASS / FAIL / NEEDS REVIEW per row
POST /check/csv            same, CSV upload
GET  /limits               the extracted rule table
PUT  /limits               save an engineer-edited rule table
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from .answer import Answer, answer_question
from .compliance.check import DesignRow, RowResult, read_design_csv
from .compliance.limits import RuleTable
from .compliance.service import check_rows, get_rule_table, save_edited_table
from .config import CLAUSES_FILE
from .ingest.pipeline import load_index
from .llm import get_llm
from .lookup import find_clause
from .models import Clause
from .retrieval import RetrievalResult, get_retriever


@asynccontextmanager
async def lifespan(app: FastAPI):
    if CLAUSES_FILE.exists():
        get_retriever().search("warm up")  # load the embedding model before the first real request
    yield


app = FastAPI(title="RuleMate", version="0.1.0", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str
    use_cache: bool = True
    provider: str | None = None  # e.g. "groq", "ollama", "groq,ollama"
    docs: list[str] | None = None  # rule documents that apply; None = all


class ClauseView(BaseModel):
    clause: Clause
    children: list[str]  # clause_ids directly under this one
    referenced_by: list[str]  # clause_ids that refer to this one


class Health(BaseModel):
    ok: bool
    documents: list[dict]
    n_clauses: int
    retrieval_mode: str
    llm: str
    synthetic_data: bool


def _require_index():
    if not CLAUSES_FILE.exists():
        raise HTTPException(503, "No index. Run: python -m rulemate ingest")
    return get_retriever()


@app.get("/health", response_model=Health)
def health() -> Health:
    r = _require_index()
    docs = load_index().documents
    return Health(
        ok=True,
        documents=[d.model_dump(include={"doc", "title", "style", "n_clauses", "n_tables", "synthetic"}) for d in docs],
        n_clauses=len(r.clauses),
        retrieval_mode=r.mode,
        llm=get_llm().label,
        synthetic_data=any(d.synthetic for d in docs),
    )


@app.post("/ask", response_model=Answer)
def ask(req: AskRequest) -> Answer:
    if not req.question.strip():
        raise HTTPException(422, "Empty question")
    return answer_question(req.question, retriever=_require_index(), llm=get_llm(req.provider), use_cache=req.use_cache,
                           docs=req.docs)


@app.get("/search", response_model=RetrievalResult)
def search(q: str, k: int = 5, follow_refs: bool = True, docs: str | None = None) -> RetrievalResult:
    return _require_index().search(q, k=k, follow_refs=follow_refs, docs=docs.split(",") if docs else None)


@app.get("/clause/{clause_id:path}", response_model=list[ClauseView])
def clause(clause_id: str, doc: str | None = Query(None)) -> list[ClauseView]:
    r = _require_index()
    hits = find_clause(r.clauses, clause_id, doc)
    if not hits:
        raise HTTPException(404, f"No clause '{clause_id}'")
    out = []
    for c in hits[:10]:
        out.append(
            ClauseView(
                clause=c,
                children=[x.clause_id for x in r.children.get(c.uid, [])],
                referenced_by=[x.clause_id for x in r.clauses if x.doc == c.doc and c.clause_id in x.refs],
            )
        )
    return out


class CheckRequest(BaseModel):
    rows: list[DesignRow]
    explain: bool = False
    provider: str | None = None
    docs: list[str] | None = None  # rule documents that apply to this vessel; None = all


@app.post("/check", response_model=list[RowResult])
def check(req: CheckRequest) -> list[RowResult]:
    _require_index()
    try:
        return check_rows(req.rows, explain=req.explain, provider=req.provider, docs=req.docs)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/check/csv", response_model=list[RowResult])
async def check_csv(file: UploadFile = File(...), explain: bool = False, provider: str | None = None,
                    docs: str | None = None) -> list[RowResult]:
    _require_index()
    try:
        rows = read_design_csv(await file.read())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    try:
        return check_rows(rows, explain=explain, provider=provider, docs=docs.split(",") if docs else None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/limits", response_model=RuleTable)
def limits(rebuild: bool = False, method: str = "auto", provider: str | None = None) -> RuleTable:
    _require_index()
    return get_rule_table(method=method, provider=provider, force=rebuild)  # type: ignore[arg-type]


@app.put("/limits", response_model=RuleTable)
def put_limits(table: RuleTable) -> RuleTable:
    save_edited_table(table)
    return table
