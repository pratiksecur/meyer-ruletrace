"""The Maritime Labour Convention, 2006 (as amended, incl. 2022 amendments) from the ILO, as Markdown for the MLC parser.

The text is copied verbatim from the ILO's consolidated PDF; only the layout changes (running headers and page
numbers removed, lines joined into paragraphs, headings marked). The ILO's text is not redistributed with the code
(the output file is git-ignored): every installation downloads it from the ILO (scripts/fetch_mlc.py, or the
Copilot's "Build the index").
"""

from __future__ import annotations

import re
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

URL = "https://www.ilo.org/sites/default/files/2024-10/NORMES_MLC%20Amendments-EN_2022_Web_1.pdf"

DASH = r"\s*[–—-]\s*"
HEADINGS = [  # (pattern, markdown level, normalised text)
    (re.compile(r"^Preamble$"), 2, lambda m: "Preamble"),
    (re.compile(r"^Article\s+([IVX]+)$"), 2, lambda m: f"Article {m.group(1)}"),
    (re.compile(r"^Explanatory\s+note\s+to\s+the\s+Regulations\s+and\s+Code", re.I), 2,
     lambda m: "Explanatory note to the Regulations and Code"),
    (re.compile(r"^title\s+(\d)\.\s*(.*)$", re.I), 1, lambda m: f"Title {m.group(1)}. {m.group(2).capitalize()}"),
    (re.compile(rf"^Regulation\s+(\d+(?:\.\d+){{1,2}}){DASH}(.+)$"), 2, lambda m: f"Regulation {m.group(1)} – {m.group(2)}"),
    (re.compile(rf"^Standard\s+(A\d+(?:\.\d+){{1,2}}){DASH}(.+)$"), 3, lambda m: f"Standard {m.group(1)} – {m.group(2)}"),
    (re.compile(rf"^Guideline\s+(B\d+(?:\.\d+){{1,3}}){DASH}(.+)$"), 3, lambda m: f"Guideline {m.group(1)} – {m.group(2)}"),
    (re.compile(r"^appendix\s+([AB]\d-[IVX]+)\b\s*(?:[–—-]\s*(.*))?$", re.I), 2,
     lambda m: f"Appendix {m.group(1).upper()}" + (f" – {m.group(2).capitalize()}" if m.group(2) else "")),
]
NEW_PARA = re.compile(r"^(\d{1,2}\.\s|\([a-z]\)\s|\([ivx]+\)\s|Purpose:|Note:)")


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "RuleMate rule-text fetcher"})
    with urllib.request.urlopen(req, timeout=60) as r:
        dest.write_bytes(r.read())


def heading(line: str) -> tuple[int, str] | None:
    for rx, level, fmt in HEADINGS:
        if m := rx.match(line):
            return level, fmt(m)
    return None


def convert(pdf_path: Path) -> tuple[str, int]:
    import pdfplumber

    out: list[str] = []
    para = ""
    started = False
    appendix = ""
    pending = ""  # page marker to write before the next paragraph (a paragraph may run on over a page break)

    def flush():
        nonlocal para
        if para:
            out.append(para.strip())
            out.append("")
            para = ""

    def mark():
        nonlocal pending
        if pending:
            out.append(f"<!-- page: {pending} -->")
            pending = ""

    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        for pg in pdf.pages:
            h = float(pg.height)
            body = pg.crop((0, 52, float(pg.width), h - 48))  # running header (top) and page number (bottom)
            foot = (pg.crop((0, h - 50, float(pg.width), h)).extract_text() or "").strip()
            top = (pg.crop((0, 0, float(pg.width), 52)).extract_text() or "").strip()
            printed = foot if foot.isdigit() else ""
            lines = [l.strip() for l in (body.extract_text() or "").splitlines() if l.strip()]
            if not started:
                if any(l == "Preamble" for l in lines) and not any("....." in l for l in lines):
                    started = True
                else:
                    continue
            if printed:
                pending = printed
            if (am := re.match(r"^appendix\s+([AB]\d-[IVX]+)", top, re.I)) and am.group(1).upper() != appendix:
                appendix = am.group(1).upper()  # an appendix's name is only in the running header
                flush()
                mark()
                out.append(f"## Appendix {appendix}")
                out.append("")
            for line in lines:
                if "....." in line:
                    continue
                hd = heading(line)
                if hd and hd[1].startswith("Appendix "):
                    aid = hd[1].split()[1]
                    if aid == appendix:
                        continue  # the same appendix named again (header, continuation page)
                    appendix = aid
                if hd:
                    flush()
                    mark()
                    out.append("#" * hd[0] + " " + hd[1])
                    out.append("")
                    continue
                if NEW_PARA.match(line) or not para:
                    flush()
                    mark()
                    para = line
                elif para.endswith("-") and line[:1].islower():
                    # a word broken over two lines; prefixes that keep their hyphen (re-registered) keep it
                    para = (para if re.search(r"\b(re|co|non|self|well|long|short|full|part)-$", para, re.I) else para[:-1]) + line
                else:
                    para += " " + line
        flush()
    return "\n".join(out), n


def fetch(out: Path, pdf: Path | None = None, url: str = URL) -> str:
    """Download (unless a PDF is given), convert and write; returns a one-line summary."""
    with tempfile.TemporaryDirectory() as td:
        src = pdf or Path(td) / "mlc.pdf"
        if pdf is None:
            download(url, src)
        text, n = convert(src)
    head = (f"<!-- SOURCE: Maritime Labour Convention, 2006, as amended (incl. 2022 amendments), consolidated text by "
            f"the International Labour Office ({url}), downloaded {date.today().isoformat()}. Text verbatim; layout "
            f"converted by rulemate/ingest/mlc_fetch.py. Not redistributed: this file is git-ignored. -->\n"
            "# Maritime Labour Convention, 2006, as amended (MLC 2006)\n\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(head + text + "\n", encoding="utf-8")
    return (f"{out} ({n} PDF pages, {text.count(chr(10) + '### Standard')} standards, "
            f"{text.count(chr(10) + '### Guideline')} guidelines)")
