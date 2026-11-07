"""Read a general-arrangement (GA) drawing in vector PDF into the design model.

How the drawing is interpreted (every step is reported in ``design.interpretation`` so an
engineer can see and correct it):

1. **Scale** from the rows of frame numbers (5, 10, 15 … printed along each deck plan's
   centreline): a straight-line fit of frame number against x gives points per frame, and
   the stated frame spacing (e.g. "FRAME SPACING: 0,60 m") gives metres. Cross-checked against
   the title-block scale (e.g. 1:150).
2. **Deck plans** from the large deck titles ("SHELTER DECK", "MAIN DECK" …): each plan is the
   band of the sheet above its title. The frame row inside the band is its centreline.
3. **Deck heights** from the profile legend ("11300" next to "SHELTER DECK" → 11.3 m ABL).
4. **Rooms** from text labels: "2 PERS. 15 m2" → a two-berth cabin of 15 m², "GALLEY 30 m2" → a
   space, "SICKBAY 20 m2" → hospital. Position = label position in metres; footprint = a square of
   the stated area (approximate; marked as such).
5. **Principal particulars and declarations** from the title block ("LENGTH OVER ALL: 69,80 m",
   "ACCOMMODATION FOR 38 PERSONS", "SHELTER DECK: 6 SINGLE AND 10 DOUBLE CABINS"), so the plan
   can be checked against what the drawing itself claims.

Needs ``pip install pypdfium2`` (extra ``drawings``); PDFium reads large vector drawings in under a second.

Drawings from other yards are laid out differently, so the reader also handles:
- **Dimension tables** whose values sit in a separate column ("LENGTH O.A ... 60,00 m"): labels and
  values are paired row by row.
- **"DEPTH TO TRAWL DECK 8.80 m"** lines as deck heights when there is no profile legend.
- **No frame numbers as text**: the title-block scale is used instead, and positions are not measured.
- **Outlined text / scans** (no text layer): the page is read with Tesseract OCR if it is installed
  (slower, about a minute; reported as OCR in the interpretation, with less certainty).
When the room list can't be read completely, rules about rooms that must exist (e.g. a hospital) are
not applied, so a missing label can never turn into a false finding.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .model import Design, DesignError, design_from_dict

DECK_ALIASES = {"FORECASTLE DECK": "FO'C'SLE DECK", "FOCSLE DECK": "FO'C'SLE DECK"}
TITLE_RE = re.compile(r"^(?:[A-Z'’]+\s)?(?:DECK|TANKTOP|BOTTOM)$")
GENERIC_TITLE_RE = re.compile(r"^(?:[A-Z0-9'’/.]+\s){0,2}(?:DECK|TANKTOP|BOTTOM)$")
AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*2?\b", re.I)
PERS_RE = re.compile(r"(\d+)\s*PERS\.?", re.I)
HOSPITAL_RE = re.compile(r"\b(SICKBAY|SICK BAY|HOSPITAL)\b", re.I)


def deck_title(name: str) -> str:
    """'FO'C'SLE DECK' -> "Fo'c'sle deck", 'TANKTOP' -> 'Tanktop'."""
    t = DECK_ALIASES.get(name.strip().upper(), name.strip().upper()).lower()
    return t[:1].upper() + t[1:]


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least squares y = a + b x."""
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    b = sum((p[0] - mx) * (p[1] - my) for p in points) / sxx
    return my - b * mx, b


def _words_pdfium(path: Path, page_no: int) -> tuple[list[dict], str]:
    """Words with positions and font size, using PDFium (fast on large vector drawings).
    Coordinates: points, origin top-left (like pdfplumber)."""
    import ctypes

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    doc = pdfium.PdfDocument(str(path))
    page = doc[page_no]
    height = page.get_height()
    tp = page.get_textpage()
    n = tp.count_chars()
    full = tp.get_text_range()
    words: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur["text"].strip():
            words.append(cur)
        cur = None

    for i in range(n):
        ch = tp.get_text_range(i, 1)
        if not ch or ch.isspace():
            flush()
            continue
        left, bottom, right, top = tp.get_charbox(i)
        mat = raw.FS_MATRIX()
        raw.FPDFText_GetMatrix(tp.raw, i, ctypes.byref(mat))
        # effective size on the page = font size x vertical scale of the text matrix
        size = raw.FPDFText_GetFontSize(tp.raw, i) * (mat.c ** 2 + mat.d ** 2) ** 0.5
        y_top, y_bot = height - top, height - bottom
        if cur is not None:
            same_line = abs(y_bot - cur["bottom"]) < 0.5 * max(size, 1)
            gap = left - cur["x1"]
            if not (same_line and abs(size - cur["size"]) < 0.3 and -0.5 * size < gap < 0.3 * size):
                flush()
        if cur is None:
            cur = {"text": ch, "x0": left, "x1": right, "top": y_top, "bottom": y_bot, "size": size}
        else:
            cur["text"] += ch
            cur["x1"], cur["top"], cur["bottom"] = max(cur["x1"], right), min(cur["top"], y_top), max(cur["bottom"], y_bot)
    flush()
    tp.close()
    page.close()
    doc.close()
    return words, full


def _blocks(words: list[dict]) -> list[dict]:
    """Group words into multi-line labels (same font size, stacked or side by side)."""
    # order words line by line (baselines within a quarter of the font size), left to right
    raw_ws = sorted((w for w in words if 3.5 <= w["size"] <= 8.5), key=lambda w: w["bottom"])
    lines: list[list[dict]] = []
    for w in raw_ws:
        if lines and abs(w["bottom"] - sum(x["bottom"] for x in lines[-1]) / len(lines[-1])) < w["size"] * 0.25:
            lines[-1].append(w)
        else:
            lines.append([w])
    ws = [w for ln in lines for w in sorted(ln, key=lambda w: w["x0"])]
    blocks: list[dict] = []
    for w in ws:
        size = round(w["size"], 1)
        best = None
        for b in blocks:
            last = b["words"][-1]
            if abs(b["size"] - size) > 0.35:
                continue
            # same baseline as a word of this label, directly left or right of it
            same_line = any(abs(w["bottom"] - x["bottom"]) < size * 0.25 and
                            (-0.5 <= w["x0"] - x["x1"] < size * 1.2 or -0.5 <= x["x0"] - w["x1"] < size * 1.2)
                            for x in b["words"][-4:])
            line_top = min(x["top"] for x in b["words"] if abs(x["bottom"] - b["bottom"]) < size * 0.25)
            prev_bottom = max((x["bottom"] for x in b["words"] if x["bottom"] < line_top + 0.5), default=b["bottom"])
            below = (-1 <= w["top"] - b["bottom"] <= size * 0.9 or -1 <= w["top"] - prev_bottom <= size * 0.9) \
                and w["x0"] < b["x1"] + 3 and w["x1"] > b["x0"] - 3
            if not (same_line or below):
                continue
            # prefer continuing the same line; otherwise the label most directly above
            overlap = min(w["x1"], b["x1"]) - max(w["x0"], b["x0"])
            centre_gap = abs((w["x0"] + w["x1"]) / 2 - (b["x0"] + b["x1"]) / 2)
            score = (0 if same_line else 1, 0 if overlap > 0 else 1, centre_gap)
            if best is None or score < best[0]:
                best = (score, b)
        target = best[1] if best else None
        if target is None:
            blocks.append({"words": [w], "size": size, "x0": w["x0"], "x1": w["x1"], "top": w["top"],
                           "bottom": w["bottom"]})
        else:
            target["words"].append(w)
            target["x0"], target["x1"] = min(target["x0"], w["x0"]), max(target["x1"], w["x1"])
            target["bottom"] = max(target["bottom"], w["bottom"])
    for b in blocks:
        b["text"] = " ".join(x["text"] for x in b["words"])
        b["cx"], b["cy"] = (b["x0"] + b["x1"]) / 2, (b["top"] + b["bottom"]) / 2
    return blocks


def norm_deck(name: str) -> str:
    """'1st. DECK' / '1.DECK' / '1ST DECK' -> '1 DECK'; 'FORECASTLE DECK' -> "FO'C'SLE DECK"."""
    t = re.sub(r"[.]", " ", name.upper())
    t = re.sub(r"\b(\d+)\s*(?:ST|ND|RD|TH)\b", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    return DECK_ALIASES.get(t, t)


def _words_ocr(path: Path, page_no: int, scale: float = 4.0) -> tuple[list[dict], str]:
    """Words from a page without a text layer, read with the Tesseract command-line tool."""
    exe = shutil.which("tesseract")
    if not exe:
        raise DesignError("this PDF has no readable text layer (outlined text or a scan). Install Tesseract OCR "
                          "(https://github.com/tesseract-ocr/tesseract) to read it, or export the drawing with text")
    import csv

    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(path))
    img = doc[page_no].render(scale=scale).to_pil().convert("L")
    doc.close()
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "page.png"
        img.save(png)
        out = subprocess.run([exe, str(png), "stdout", "--psm", "11", "tsv"], capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise DesignError(f"OCR failed: {out.stderr.strip()[:200]}")
    words, lines = [], {}
    for r in csv.DictReader(out.stdout.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE):
        t = (r.get("text") or "").strip()
        if not t or float(r.get("conf") or -1) < 50:
            continue
        x0, top, w, h = (int(r[k]) / scale for k in ("left", "top", "width", "height"))
        words.append({"text": t, "x0": x0, "x1": x0 + w, "top": top, "bottom": top + h, "size": h / 0.72, "ocr": True})
        lines.setdefault((r["block_num"], r["par_num"], r["line_num"]), []).append(t)
    return words, "\n".join(" ".join(v) for v in lines.values())


def _ocr_dims_block(path: Path, page_no: int, words: list[dict]) -> str:
    """Re-read the 'MAIN DIMENSIONS' table at a higher resolution, as text lines (OCR misreads small digits
    at page resolution, e.g. 77,50 as 77,00). Returns '' when there is no such heading."""
    head = next((w for w in words if re.match(r"DIMENSIONS?|PARTICULARS", w["text"], re.I)), None)
    exe = shutil.which("tesseract")
    if not head or not exe:
        return ""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(path))
    pg = doc[page_no]
    W, H = pg.get_size()
    s = head["size"]
    x0, x1 = max(0, head["x0"] - 8 * s), min(W, head["x0"] + 30 * s)
    t0, t1 = max(0, head["top"] - s), min(H, head["top"] + 16 * s)
    img = pg.render(scale=5, crop=(x0, H - t1, W - x1, t0)).to_pil().convert("L")
    doc.close()
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "dims.png"
        img.save(png)
        out = subprocess.run([exe, str(png), "stdout", "--psm", "6"], capture_output=True, text=True, timeout=120)
    return out.stdout if out.returncode == 0 else ""


def _dims_from_text(text: str) -> tuple[dict, dict]:
    """'LENGTH O.A 77,50 m' / 'DEPTH TO 2.DECK 9,10 m' lines -> particulars and deck heights."""
    found, depth_to = {}, {}
    for line in text.upper().splitlines():
        line = re.sub(r"^\W*(?:RAME|FRAMF)\s", "FRAME ", line.strip())
        for key, rx in PARTICULARS.items():
            m = re.match("(?:" + rx + r")[^\d]{0,40}?" + NUM, line)
            if m:
                v = _num(m.group(m.lastindex))
                if key == "depth_to":
                    depth_to[norm_deck(re.match(PARTICULARS[key], line).group(1))] = v
                else:
                    found.setdefault(key, v)
                break
    return found, depth_to


def _lines(words: list[dict]) -> list[dict]:
    """Words of similar size on one baseline, left to right, as text lines (gaps up to 3 font sizes)."""
    out: list[dict] = []
    for w in sorted(words, key=lambda w: (round(w["bottom"]), w["x0"])):
        for ln in out:
            if abs(ln["bottom"] - w["bottom"]) < 0.4 * w["size"] and abs(ln["size"] - w["size"]) < 0.25 * w["size"] \
                    and 0 <= w["x0"] - ln["x1"] < 3 * w["size"]:
                ln["words"].append(w)
                ln["x1"] = w["x1"]
                break
        else:
            out.append({"words": [w], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "bottom": w["bottom"], "size": w["size"]})
    for ln in out:
        ln["text"] = " ".join(x["text"] for x in ln["words"])
    return out


PARTICULARS = {
    "loa": r"LENGTH\s*(?:[O0]\.?\s?A\.?|OVER\s?ALL)", "lbp": r"LENGTH\s*(?:P\.?\s?P\.?|BETWEEN\s*P\.?P\.?)",
    "breadth": r"BREADTH|BEAM", "spacing": r"FRAME\s*SPACING", "depth_to": r"DEPTH\s+TO\s+(.+?DECK)",
}
NUM = r"(\d+(?:[.,]\d+)?)"


def _particulars_table(words: list[dict]) -> tuple[dict, dict]:
    """Main-dimension tables, including ones whose values are a separate column, paired row by row."""
    lines = _lines(words)
    found, depth_to = {}, {}
    labels = []
    for ln in lines:
        t = ln["text"].upper()
        key = next((k for k, rx in PARTICULARS.items() if re.match("(?:" + rx + ")", t)), None)
        if not key:
            continue
        inline = re.search("(?:" + PARTICULARS[key] + r")[^\d]{0,40}?" + NUM, t)
        if inline:
            labels.append((ln, key, t, _num(inline.group(inline.lastindex))))
        else:
            labels.append((ln, key, t, None))
    todo = [x for x in labels if x[3] is None]
    if todo:  # values in a column to the right: pair by order, choosing the vertical offset that fits best
        size = todo[0][0]["size"]
        x_lab = max(x[0]["x1"] for x in todo)
        nums = sorted((w for w in words if re.fullmatch(NUM + r"(?:\s*/\s*[\d.,]+)?", w["text"]) and abs(w["size"] - size) < 0.3 * size
                       and 0 < w["x0"] - x_lab < 40 * size and min(x[0]["top"] for x in todo) - 2 * size < w["top"]
                       < max(x[0]["bottom"] for x in todo) + 2 * size), key=lambda w: w["top"])
        todo.sort(key=lambda x: x[0]["top"])
        # 1) OCR: each label takes the value on its own row (OCR often drops a label or a value, so
        #    pairing by order would shift every value below the gap). Text layers keep exact order.
        used, paired = set(), 0
        for ln, key, t, _v in (list(todo) if any(w.get("ocr") for w in words) else []):
            row = [w for w in nums if id(w) not in used and abs(w["top"] - ln["top"]) < 0.6 * ln["size"]]
            if row:
                w = min(row, key=lambda w: abs(w["top"] - ln["top"]))
                used.add(id(w))
                labels[labels.index((ln, key, t, None))] = (ln, key, t, _num(w["text"].split("/")[0]))
                paired += 1
        if paired:
            todo = []
        # 2) otherwise pair by order, choosing the vertical offset that fits best
        if todo and len(nums) >= len(todo):
            best = min(range(len(nums) - len(todo) + 1),
                       key=lambda k: sum(abs(nums[k + i]["top"] - x[0]["top"]) for i, x in enumerate(todo)))
            for i, (ln, key, t, _v) in enumerate(todo):
                labels[labels.index((ln, key, t, None))] = (ln, key, t, _num(nums[best + i]["text"].split("/")[0]))
    for ln, key, t, v in labels:
        if v is None:
            continue
        if key == "depth_to":
            depth_to[norm_deck(re.match(PARTICULARS[key], t).group(1))] = v
        else:
            found.setdefault(key, v)
    return found, depth_to


def design_from_ga_pdf(path: Path, ship: dict | None = None, page_no: int = 0) -> Design:
    try:
        import pypdfium2  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise DesignError("reading drawings needs: pip install pypdfium2") from exc
    try:
        words, full_text = _words_pdfium(path, page_no)
    except Exception as exc:
        raise DesignError(f"could not read the PDF: {exc}") from exc
    notes: list[str] = []
    ocr = len(words) < 20
    if ocr:
        words, full_text = _words_ocr(path, page_no)
        notes.append("This drawing has no text layer (outlined text); it was read with OCR. OCR can miss or misread "
                     "labels, so rules about rooms that must exist are not applied.")
        if len(words) < 20:
            raise DesignError("OCR found almost no text on this drawing")

    # -- principal particulars ---------------------------------------------------------------
    def particular(label: str) -> float | None:
        m = re.search(label + r"[^:\n]*:\s*([\d.,]+)\s*m", full_text, re.I)
        return _num(m.group(1)) if m else None

    loa, lbp = particular(r"LENGTH OVER ALL"), particular(r"LENGTH BETWEEN P\.?P\.?")
    breadth, depth = particular(r"BREADTH"), particular(r"DEPTH")
    spacing = particular(r"FRAME SPACING")
    table, depth_to = _particulars_table(words)
    if ocr:  # second, sharper OCR pass over the dimensions table; its values win where it read them
        t2, d2 = _dims_from_text(_ocr_dims_block(path, page_no, words))
        if t2 or d2:
            table, depth_to = {**table, **t2}, {**depth_to, **d2}
            notes.append("Main dimensions re-read by OCR at higher resolution.")
    loa, lbp, breadth = loa or table.get("loa"), lbp or table.get("lbp"), breadth or table.get("breadth")
    if not spacing and table.get("spacing"):
        spacing = table["spacing"] / 1000 if table["spacing"] > 20 else table["spacing"]  # "600 mm" or "0,60 m"
    m = re.search(r"ACCOMMODATION FOR\s+(\d+)\s+PERSONS", full_text, re.I)
    persons = int(m.group(1)) if m else 0
    m = re.search(r"\b1\s*:\s*(\d{2,4})", full_text)
    stated_scale = int(m.group(1)) if m else None
    cls = re.search(r"([A-Z]{2,4}) CLASS:\s*([^\n]+)", full_text)
    special_purpose = bool(re.search(r"Special (?:Service|Purpose)|Research Vessel|SPS|\bRESEARCHERS\b|\bSCIENTISTS\b", full_text, re.I))
    passenger = bool(re.search(r"\bpassenger ship\b", full_text, re.I)) and not special_purpose
    declared = {}
    for dm in re.finditer(r"([A-Z'’ ]+DECK):\s*((?:\d+\s+(?:SINGLE|DOUBLE)(?:\s+AND\s+)?)+)\s*CABINS", full_text):
        deck = DECK_ALIASES.get(dm.group(1).strip(), dm.group(1).strip())
        nums = dict((k.lower(), int(v)) for v, k in re.findall(r"(\d+)\s+(SINGLE|DOUBLE)", dm.group(2)))
        declared[deck] = nums
    m = re.search(r"\b(\d+)\s*CREW\b.{0,20}?\b(\d+)\s*(?:RESEARCHERS|SCIENTISTS|PASSENGERS)", full_text, re.I | re.S)
    if m:
        declared["_persons"] = {"crew": int(m.group(1)), "other": int(m.group(2))}
    title = (re.search(r"VESSEL NAME:\s*([^\n]+)", full_text) or re.search(r"DTU AQUA\s+([^\n]+)", full_text)
             or re.search(r"(\d+\s?m\s+[A-Z ]*VESSEL[A-Z ]*)", full_text) or re.search(r"(YARD No\.?\s*\d+)", full_text, re.I))
    ship_name = (title.group(1).strip().title() if title else Path(path).stem)

    # -- scale from frame-number rows ----------------------------------------------------------
    rows: dict[int, list[tuple[int, float]]] = {}
    for w in words:
        if re.fullmatch(r"\d{1,3}", w["text"]) and int(w["text"]) % 5 == 0:
            rows.setdefault(round(w["top"]), []).append((int(w["text"]), (w["x0"] + w["x1"]) / 2))
    if ocr:  # OCR boxes don't share an exact top: group frame numbers in 6 pt bands
        rows = {}
        for w in words:
            if re.fullmatch(r"\d{1,3}", w["text"]) and int(w["text"]) % 5 == 0:
                rows.setdefault(round(w["top"] / 6) * 6, []).append((int(w["text"]), (w["x0"] + w["x1"]) / 2))
    frame_rows = {y: sorted(set(v)) for y, v in rows.items() if len(v) >= 10}
    if frame_rows and ocr:  # keep the cleanest row only (OCR picks up stray numbers)
        y0 = max(frame_rows, key=lambda y: len(frame_rows[y]))
        a0, b0 = _fit(frame_rows[y0])
        frame_rows = {y0: [p for p in frame_rows[y0] if abs(a0 + b0 * p[0] - p[1]) < 3 * abs(b0)]}
    if not spacing:
        spacing = 0.6
        notes.append("frame spacing not stated; assumed 0.60 m (check)")
    positions_measured = bool(frame_rows)
    if frame_rows:
        a, b = _fit([p for v in frame_rows.values() for p in v])  # x_pt = a + b * frame
        pt_per_m = b / spacing
        implied_scale = 72 / 25.4 * 1000 / pt_per_m
        scale_note = f"measured scale 1:{implied_scale:.0f}"
        if stated_scale:
            ratio = implied_scale / stated_scale
            ok = abs(ratio - 1) < 0.02
            scale_note += f", title block says 1:{stated_scale} ({'agrees' if ok else 'DIFFERS: check'})"
            if not ok and abs(ratio - 2 ** 0.5) < 0.04:
                scale_note += ": the sheet looks plotted one paper size smaller (e.g. A0 drawing on A1)"
    elif stated_scale:
        pt_per_m = 72 / 25.4 * 1000 / stated_scale
        a, b = 0.0, pt_per_m * spacing
        scale_note = f"title block says 1:{stated_scale}; no frame numbers in the text, so it could not be checked and positions are not measured"
    else:
        raise DesignError("no frame numbers and no stated scale on the drawing; can't measure it")
    to_x = lambda x_pt: (x_pt - a) / b * spacing  # noqa: E731  metres from frame 0

    # -- deck plans and heights ----------------------------------------------------------------
    titles = []
    for w in words:
        if w["size"] < 10:
            continue
        left = [v for v in words if abs(v["top"] - w["top"]) < 2 and 0 < w["x0"] - v["x1"] < 15 and v["size"] >= 10]
        name = (left[0]["text"] + " " + w["text"]) if left else w["text"]
        before = [v["text"].upper() for v in words if abs(v["top"] - w["top"]) < 2 and 0 < w["x0"] - v["x1"] < 6 * w["size"]]
        if "TO" in before:  # "DEPTH TO 1st. DECK" in a dimensions table, not a plan title
            continue
        if "BELOW" in before and TITLE_RE.match(name):
            name = "BELOW " + name
        if TITLE_RE.match(name) or name in {"TANKTOP", "PROFILE"} or (name.startswith("BELOW ") and name.endswith("DECK")):
            titles.append((DECK_ALIASES.get(name, name), w["top"], w["x0"]))
        elif w["text"].upper().endswith("DECK") and not ocr:  # other yards: "1st. DECK", "BELOW MAIN DECK"
            row = sorted((v for v in words if abs(v["top"] - w["top"]) < 2 and abs(v["size"] - w["size"]) < 1
                          and 0 <= w["x0"] - v["x1"] < 3 * w["size"]), key=lambda v: v["x0"])[-2:]
            full = " ".join([v["text"] for v in row] + [w["text"]]).upper()
            cand = full if GENERIC_TITLE_RE.match(full) else " ".join([v["text"] for v in row[-1:]] + [w["text"]]).upper()
            if GENERIC_TITLE_RE.match(cand) and not any(t[1] == w["top"] for t in titles):
                titles.append((norm_deck(cand), w["top"], w["x0"]))
    titles.sort(key=lambda t: t[1])
    legend = {}
    for w in words:
        if 4.5 <= w["size"] <= 6 and w["text"] in {"DECK"}:
            left = [v for v in words if abs(v["top"] - w["top"]) < 2 and 0 < w["x0"] - v["x1"] < 12]
            name = DECK_ALIASES.get((left[0]["text"] + " DECK") if left else "DECK", (left[0]["text"] + " DECK") if left else "DECK")
            nums = [v for v in words if re.fullmatch(r"\d{4,5}", v["text"]) and 0 <= v["top"] - w["top"] <= 15
                    and abs(v["x0"] - w["x0"]) < 40]
            if nums:
                legend.setdefault(name, int(nums[0]["text"]) / 1000)
    tank = re.search(r"TANK\s?TOP\s+(\d{3,5})\s*(?:/\s*\d+\s*)?ABL", full_text)
    plans = []
    for i, (name, y, x) in enumerate(titles):
        # the plan is the band above its title, up to the previous title in the same sheet column
        if name == "PROFILE":
            continue
        above = [t for t in titles[:i] if abs(t[2] - x) < 300] or titles[:i]
        band = (above[-1][1] if above else 0.0, y)
        cl = [ry for ry in frame_rows if band[0] < ry < band[1]]
        default_h = (int(tank.group(1)) / 1000 if tank else 0.0) if name == "TANKTOP" else (0.0 if name == "DOUBLE BOTTOM" else None)
        h = legend.get(name, default_h)
        if h is None:
            h = depth_to.get(norm_deck(name))
        plans.append({"deck": name, "band": band, "title_x": x, "centreline_pt": cl[0] if cl else None,
                      "height_m": h})
    if ocr:  # OCR: deck names come from the "DEPTH TO" table only
        plans = [{"deck": k, "band": (0, 0), "title_x": 0, "centreline_pt": None, "height_m": v}
                 for k, v in sorted(depth_to.items(), key=lambda kv: -kv[1])]

    # -- rooms ------------------------------------------------------------------------------------
    elements: list[dict] = []
    counter: dict[str, int] = {}
    all_blocks = _blocks(words)
    for blk in (all_blocks if positions_measured and not ocr else []):
        cands = [p for p in plans if p["band"][0] < blk["cy"] < p["band"][1]]
        if not cands:
            continue
        plan = min(cands, key=lambda p: abs(p["title_x"] - blk["cx"]))
        txt = blk["text"]
        pers, area, hosp = PERS_RE.search(txt), AREA_RE.search(txt), HOSPITAL_RE.search(txt)
        if not (pers or hosp or (area and re.search(r"[A-Z]{3,}", txt))):
            continue
        if plan["centreline_pt"] is None:
            ys = [x["cy"] for x in all_blocks if plan["band"][0] < x["cy"] < plan["band"][1]]
            plan["centreline_pt"] = sorted(ys)[len(ys) // 2] if ys else (plan["band"][0] + plan["band"][1]) / 2
            plan["centreline_estimated"] = True
        x_m = round(to_x(blk["cx"]), 2)
        y_m = round((blk["cy"] - plan["centreline_pt"]) / pt_per_m, 2)
        z_m = plan["height_m"] if plan["height_m"] is not None else 0.0
        area_m2 = _num(area.group(1)) if area else None
        side = math.sqrt(area_m2) if area_m2 else 2.0
        name = re.sub(r"\s+", " ", AREA_RE.sub("", txt)).strip(" ,.")
        if pers:
            etype, props = "cabin", {"berths": int(pers.group(1))}
            role = re.search(r"\(([^)]+)\)", txt)
            if role:
                props["occupant"] = role.group(1).title()
        elif hosp:
            etype, props = "hospital", {}
        else:
            etype, props = "space", {}
        if area_m2:
            props["floor_area"] = f"{area_m2:g} m2"
        props["label"] = txt
        props["position"] = "approximate (from label position on the drawing)"
        deck_short = deck_title(plan["deck"])
        key = f"{etype}-{plan['deck'][:3]}"
        counter[key] = counter.get(key, 0) + 1
        elements.append({"id": f"{etype[:3].upper()}-{plan['deck'].split()[0][:3].upper()}-{counter[key]:02d}",
                         "type": etype, "name": name.title() or etype, "deck": deck_short,
                         "box": [round(x_m - side / 2, 2), round(y_m - side / 2, 2), z_m, round(side, 2), round(side, 2),
                                 2.4], "props": props, "source": f"{Path(path).name} p{page_no + 1}"})
    if ocr:
        for w in words:
            if HOSPITAL_RE.search(w["text"]):
                counter["hos"] = counter.get("hos", 0) + 1
                elements.append({"id": f"HOS-OCR-{counter['hos']:02d}", "type": "hospital", "name": w["text"].title(),
                                 "deck": "", "props": {"label": w["text"], "position": "not measured (read by OCR)"}})
    tank_h = next((p["height_m"] for p in plans if p["deck"] == "TANKTOP" and p["height_m"]), None)
    if tank_h and (lbp or loa) and breadth:
        elements.append({"id": "DB-01", "type": "double_bottom", "name": "Double bottom (to tank top)", "deck": "Tanktop",
                         "box": [0, round(-breadth / 2, 2), 0, lbp or loa, breadth, tank_h],
                         "props": {"role": "double_bottom", "source": "tank-top height read from the drawing"}})
    persons_decl = declared.pop("_persons", None)
    for deck, nums in declared.items():
        elements.append({"id": f"DECL-{deck.split()[0][:3].upper()}", "type": "declaration",
                         "name": f"Stated on drawing: {deck_title(deck)}", "deck": deck_title(deck),
                         "props": {"declared_single": nums.get("single", 0), "declared_double": nums.get("double", 0)}})
    if persons_decl and persons:
        elements.append({"id": "DECL-PERSONS", "type": "persons_statement", "name": "Stated on drawing: persons on board",
                         "deck": "", "props": {"declared_crew": persons_decl["crew"], "declared_other": persons_decl["other"],
                                               "declared_total": persons}})
    rooms_read = any(e["type"] in ("cabin", "space") for e in elements)
    if not rooms_read:
        notes.append("No room labels could be read as text on this drawing, so room rules (cabin sizes, hospital) "
                     "can't be checked from it.")

    ship_d = {"name": ship_name, "ship_type": "special_purpose" if special_purpose else ("passenger" if passenger else "cargo"),
              "passengers": 0, "persons_total": persons, "length_m": lbp or loa or 0, "breadth_m": breadth or 0,
              "notes": f"Read from drawing {Path(path).name}. Keel-laying date and gross tonnage are not on the drawing.",
              "complete_arrangement": rooms_read and not ocr and (bool(persons and declared) or "GENERAL ARRANGEMENT" in full_text.upper())}
    if ship:
        ship_d.update(ship)
    interp = {
        "source": Path(path).name, "page": page_no + 1, "scale": scale_note,
        "read_with": "OCR (Tesseract)" if ocr else "PDF text layer", "positions_measured": positions_measured and not ocr,
        "frame_spacing_m": spacing, "points_per_metre": round(pt_per_m, 3),
        "principal_particulars": {"loa_m": loa, "lbp_m": lbp, "breadth_m": breadth, "depth_m": depth,
                                  "accommodation_persons": persons,
                                  "class": f"{cls.group(1)}: {cls.group(2).strip()}" if cls else None},
        "decks": [{"deck": deck_title(p["deck"]), "height_m": p["height_m"],
                   "centreline": "estimated" if p.get("centreline_estimated") else "from frame row"} for p in plans],
        "found": {t: sum(1 for e in elements if e["type"] == t) for t in ("cabin", "hospital", "space", "declaration", "double_bottom")},
        "declared_cabins": declared,
        "notes": notes + ["Positions are read from label positions; room footprints are approximated from stated "
                          "areas. An engineer should confirm the reading before relying on the result."],
    }
    d = design_from_dict({"title": f"Drawing: {ship_name} general arrangement", "ship": ship_d, "elements": elements,
                          "source_format": "pdf-drawing", "interpretation": interp}, source_file=Path(path).name)
    return d
