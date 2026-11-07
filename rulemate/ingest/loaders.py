"""Format loaders: turn PDF / DOCX / HTML / TXT / MD into a flat stream of Blocks.

A Block is one line (or paragraph) of text, or one whole table rendered as
Markdown. Parsers work on this stream only, so they do not care about the
source format.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Block:
    text: str
    page: int | None = None
    kind: str = "text"  # "text" | "table"
    heading_level: int | None = None


def rows_to_markdown(rows: list[list[str | None]]) -> str:
    rows = [[_cell(c) for c in r] for r in rows if r and any(c not in (None, "") for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _cell(c: str | None) -> str:
    return re.sub(r"\s+", " ", c or "").replace("|", "/").strip()


# ---------------------------------------------------------------- TXT / MD

_PAGE_MARK = re.compile(r"<!--\s*page:?\s*(\d+)\s*-->", re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def load_text(path: Path) -> list[Block]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    is_md = path.suffix.lower() == ".md"
    blocks: list[Block] = []
    page = 1
    table: list[str] = []
    table_page = page

    def flush_table() -> None:
        nonlocal table
        if table:
            blocks.append(Block("\n".join(table), table_page, "table"))
            table = []

    for chunk_no, chunk in enumerate(raw.split("\f")):  # form feed = page break in TXT
        if chunk_no:
            page += 1
        for line in chunk.splitlines():
            m = _PAGE_MARK.search(line)
            if m:
                flush_table()
                page = int(m.group(1))
                continue
            line = _COMMENT.sub("", line).rstrip()
            if line.lstrip().startswith("|"):
                if not table:
                    table_page = page
                table.append(line.strip())
                continue
            flush_table()
            if not line.strip():
                continue
            level = None
            if is_md:
                hm = re.match(r"^(#{1,6})\s+(.*)$", line)
                if hm:
                    level, line = len(hm.group(1)), hm.group(2)
                line = re.sub(r"^\s*>\s?", "", line)  # blockquote
                line = line.replace("**", "").replace("__", "")
            if line.strip():
                blocks.append(Block(line.strip(), page, "text", level))
    flush_table()
    return blocks


# ---------------------------------------------------------------- PDF

_PAGE_NUMBER = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$", re.I)


def load_pdf(path: Path) -> list[Block]:
    import pdfplumber

    pages: list[list[tuple[float, Block]]] = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            items: list[tuple[float, Block]] = []
            boxes = []
            for t in page.find_tables():
                rows = t.extract()
                if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                    md = rows_to_markdown(rows)
                    if md:
                        boxes.append(t.bbox)
                        items.append((t.bbox[1], Block(md, pno, "table")))

            def outside(obj, boxes=boxes):
                if obj.get("object_type") != "char":
                    return True
                x = (obj["x0"] + obj["x1"]) / 2
                y = (obj["top"] + obj["bottom"]) / 2
                return not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)

            text_page = page.filter(outside) if boxes else page
            for ln in text_page.extract_text_lines():
                txt = ln["text"].strip()
                if txt:
                    items.append((ln["top"], Block(txt, pno, "text")))
            items.sort(key=lambda it: it[0])
            pages.append(items)

    # Drop running headers/footers: text lines repeated near the top/bottom of many pages.
    repeated: set[str] = set()
    if len(pages) >= 3:
        counts: Counter[str] = Counter()
        for items in pages:
            texts = [b.text for _, b in items if b.kind == "text"]
            edge = set(texts[:3] + texts[-3:])
            counts.update(_norm_edge(t) for t in edge)
        repeated = {t for t, n in counts.items() if n >= max(3, len(pages) // 2)}

    blocks: list[Block] = []
    for items in pages:
        for _, b in items:
            if b.kind == "text" and (_norm_edge(b.text) in repeated or _PAGE_NUMBER.match(b.text)):
                continue
            blocks.append(b)
    return blocks


def _norm_edge(t: str) -> str:
    return re.sub(r"\d+", "#", t.strip().lower())


# ---------------------------------------------------------------- DOCX


def load_docx(path: Path) -> list[Block]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    blocks: list[Block] = []
    page = 1
    for el in d.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, d)
            # Word stores where pages broke at last render; good enough for citations.
            xml = el.xml
            page += xml.count("lastRenderedPageBreak") + xml.count('w:type="page"')
            text = p.text.strip()
            if not text:
                continue
            level = None
            style = (p.style.name if p.style is not None else "") or ""
            hm = re.match(r"heading\s*(\d)", style, re.I)
            if hm:
                level = int(hm.group(1))
            elif style.lower() == "title":
                level = 1
            for line in text.splitlines():
                if line.strip():
                    blocks.append(Block(line.strip(), page, "text", level))
        elif tag == "tbl":
            t = Table(el, d)
            rows = [[c.text for c in r.cells] for r in t.rows]
            md = rows_to_markdown(rows)
            if md:
                blocks.append(Block(md, page, "table"))
    return blocks


# ---------------------------------------------------------------- HTML

_TEXT_TAGS = {"p", "li", "pre", "dt", "dd", "caption", "figcaption", "blockquote", "td", "th"}


def load_html(path: Path) -> list[Block]:
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        t.decompose()
    blocks: list[Block] = []

    def emit(text: str, level: int | None = None) -> None:
        for line in text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                blocks.append(Block(line, None, "text", level))

    def walk(node) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if child.strip():
                    emit(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name == "table":
                rows = [
                    [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                    for tr in child.find_all("tr")
                ]
                cap = child.find("caption")
                if cap:
                    emit(cap.get_text(" ", strip=True))
                md = rows_to_markdown(rows)
                if md:
                    blocks.append(Block(md, None, "table"))
            elif re.fullmatch(r"h[1-6]", name):
                emit(child.get_text(" ", strip=True), int(name[1]))
            elif name in _TEXT_TAGS:
                emit(child.get_text(" ", strip=True))
            elif name == "br":
                continue
            else:
                walk(child)

    walk(soup.body or soup)
    return blocks


LOADERS = {
    ".pdf": load_pdf,
    ".docx": load_docx,
    ".html": load_html,
    ".htm": load_html,
    ".txt": load_text,
    ".md": load_text,
}


def load_blocks(path: Path) -> list[Block]:
    try:
        loader = LOADERS[path.suffix.lower()]
    except KeyError:
        raise ValueError(f"Unsupported file type: {path}") from None
    return loader(path)
