"""Ingest pipeline: files in data/rules/ -> clauses.json."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..config import CLAUSES_FILE, RULES_DIR, SUPPORTED_EXTENSIONS
from ..models import Clause, DocumentInfo, IngestResult
from .loaders import Block, load_blocks
from .parsers import Draft, pick_parser


def is_synthetic(path: Path, blocks: list[Block]) -> bool:
    head = " ".join(b.text for b in blocks[:15])
    return "SYNTHETIC" in path.name.upper() or "SYNTHETIC" in head.upper()


def doc_title(path: Path, blocks: list[Block]) -> str:
    for b in blocks[:30]:
        if b.kind == "text" and b.heading_level == 1:
            return b.text[:150]
    for b in blocks[:5]:
        if b.kind == "text":
            return b.text[:150]
    return path.stem


def ancestors(cid: str) -> list[str]:
    """Candidate fall-backs for an unknown reference, most specific first.

    "II-2/13.4.2.1" -> ["II-2/13.4.2", "II-2/13.4", "II-2/13"]
    "Pt.3 Ch.2 Sec.4 [2.1.3]" -> ["Pt.3 Ch.2 Sec.4 [2.1]", "Pt.3 Ch.2 Sec.4 [2]", "Pt.3 Ch.2 Sec.4"]
    """
    out = []
    cur = cid
    for _ in range(8):
        nxt = re.sub(r"(?<=\d)\([a-z0-9]+\)$", "", cur)  # CFR paragraph: 190.10-25(b) -> 190.10-25
        if nxt == cur:
            nxt = re.sub(r"(?<=\d\.\d)(\d*)-\d+$", r"\1", cur)  # CFR section: 190.10-25 -> 190.10
        if nxt == cur:
            nxt = re.sub(r"(?<=\d)\.\d+(\]?)$", r"\1", cur)
        if nxt == cur:
            nxt = re.sub(r"\s*\[\d+\]$", "", cur)
        if nxt == cur or not nxt:
            break
        out.append(nxt)
        cur = nxt
    return out


def build_clauses(doc: str, drafts: list[Draft], parser, synthetic: bool) -> list[Clause]:
    # unique IDs (a re-used number keeps its first ID, later ones get "#2", "#3")
    seen: dict[str, int] = {}
    for d in drafts:
        if d.clause_id in seen:
            seen[d.clause_id] += 1
            d.clause_id = f"{d.clause_id}#{seen[d.clause_id]}"
        else:
            seen[d.clause_id] = 1

    by_id = {d.clause_id: d for d in drafts}

    def parent_of(d: Draft) -> Draft | None:
        pid = d.parent_id
        while pid:
            if pid in by_id and by_id[pid] is not d:
                return by_id[pid]
            anc = ancestors(pid)
            pid = anc[0] if anc else None
        return None

    clauses = []
    for d in drafts:
        text = d.text
        if not text and d.kind != "heading":
            continue
        chain, p = [], parent_of(d)
        while p is not None and len(chain) < 10:
            chain.append(p)
            p = parent_of(p)
        title = d.title or next((a.title for a in chain if a.title), "")
        path = [f"{a.clause_id} {a.title}".strip() for a in reversed(chain)]

        refs, unresolved = [], []
        for r in parser.find_refs(text, d.ctx):
            target = r if r in by_id else next((a for a in ancestors(r) if a in by_id), None)
            if target is None:
                unresolved.append(r)
            elif target != d.clause_id and target not in refs:
                refs.append(target)

        clauses.append(
            Clause(
                uid=f"{doc}::{d.clause_id}",
                doc=doc,
                clause_id=d.clause_id,
                title=title,
                page=d.page,
                text=text or title,
                kind=d.kind,  # type: ignore[arg-type]
                parent_id=chain[0].clause_id if chain else None,
                path=path,
                refs=refs,
                unresolved_refs=sorted(set(unresolved)),
                synthetic=synthetic,
            )
        )
    return clauses


def ingest_file(path: Path, style: str | None = None) -> tuple[DocumentInfo, list[Clause]]:
    blocks = load_blocks(path)
    parser = pick_parser(blocks, style)
    drafts = parser.parse(blocks)
    synthetic = is_synthetic(path, blocks)
    clauses = build_clauses(path.stem, drafts, parser, synthetic)
    info = DocumentInfo(
        doc=path.stem,
        path=str(path),
        title=doc_title(path, blocks),
        style=parser.name,
        n_clauses=len(clauses),
        n_tables=sum(c.kind == "table" for c in clauses),
        synthetic=synthetic,
        sha1=hashlib.sha1(path.read_bytes()).hexdigest(),
    )
    return info, clauses


def ingest_dir(rules_dir: Path = RULES_DIR, out_file: Path = CLAUSES_FILE, styles: dict[str, str] | None = None) -> IngestResult:
    """Ingest every supported file under ``rules_dir``.

    ``styles`` optionally forces a parser per file stem, e.g. {"DNV-RU-SHIP-Pt3Ch2": "class"};
    it is also read from ``rules_dir/styles.json`` if present.
    """
    styles = dict(styles or {})
    sfile = rules_dir / "styles.json"
    if sfile.exists():
        styles.update(json.loads(sfile.read_text()))

    docs, clauses = [], []
    for path in sorted(rules_dir.rglob("*")):
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS or path.name.startswith((".", "~$")):
            continue
        if path.name.lower() in {"readme.md", "readme.txt"}:
            continue
        info, cl = ingest_file(path, styles.get(path.stem))
        docs.append(info)
        clauses.extend(cl)

    result = IngestResult(documents=docs, clauses=clauses)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(result.model_dump_json(indent=1), encoding="utf-8")
    return result


def load_index(path: Path = CLAUSES_FILE) -> IngestResult:
    return IngestResult.model_validate_json(path.read_text(encoding="utf-8"))
