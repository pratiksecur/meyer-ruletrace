"""Shared pydantic models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ClauseKind = Literal["clause", "heading", "table", "section"]


class Clause(BaseModel):
    """One citable unit of a rule document."""

    uid: str  # "<doc>::<clause_id>", unique across the index
    doc: str  # short document name (file stem)
    clause_id: str  # e.g. "II-2/13.3.1" or "Pt.3 Ch.2 Sec.4 [2.1.3]"
    title: str = ""
    page: int | None = None
    text: str = ""
    kind: ClauseKind = "clause"
    parent_id: str | None = None
    path: list[str] = Field(default_factory=list)  # breadcrumb of ancestor headings
    refs: list[str] = Field(default_factory=list)  # resolved clause_ids this clause points to
    unresolved_refs: list[str] = Field(default_factory=list)
    synthetic: bool = False

    @property
    def citation(self) -> str:
        return f"[{self.doc} § {self.clause_id}]"

    def display(self) -> str:
        head = f"{self.citation} {self.title}".strip()
        page = f" (p. {self.page})" if self.page else ""
        return f"{head}{page}\n{self.text}"


class DocumentInfo(BaseModel):
    doc: str
    path: str
    title: str
    style: str
    n_clauses: int
    n_tables: int
    synthetic: bool
    sha1: str


class IngestResult(BaseModel):
    documents: list[DocumentInfo]
    clauses: list[Clause]
