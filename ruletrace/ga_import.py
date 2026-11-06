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

Needs ``pip install pypdfium2`` (extra ``drawings``); PDFium reads large vector drawings in under a second. Scanned (image-only) PDFs have no text
layer and are rejected with a clear message.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .model import Design, DesignError, design_from_dict

DECK_ALIASES = {"FORECASTLE DECK": "FO'C'SLE DECK", "FOCSLE DECK": "FO'C'SLE DECK"}
TITLE_RE = re.compile(r"^(?:[A-Z'’]+\s)?(?:DECK|TANKTOP|BOTTOM)$")
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


def design_from_ga_pdf(path: Path, ship: dict | None = None, page_no: int = 0) -> Design:
    try:
        import pypdfium2  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise DesignError("reading drawings needs: pip install pypdfium2") from exc
    try:
        words, full_text = _words_pdfium(path, page_no)
    except Exception as exc:
        raise DesignError(f"could not read the PDF: {exc}") from exc
    if len(words) < 20:
        raise DesignError("this PDF has no readable text layer (scanned drawing?); it needs OCR first")
    notes: list[str] = []

    # -- principal particulars ---------------------------------------------------------------
    def particular(label: str) -> float | None:
        m = re.search(label + r"[^:\n]*:\s*([\d.,]+)\s*m", full_text, re.I)
        return _num(m.group(1)) if m else None

    loa, lbp = particular(r"LENGTH OVER ALL"), particular(r"LENGTH BETWEEN P\.?P\.?")
    breadth, depth = particular(r"BREADTH"), particular(r"DEPTH")
    spacing = particular(r"FRAME SPACING")
    m = re.search(r"ACCOMMODATION FOR\s+(\d+)\s+PERSONS", full_text, re.I)
    persons = int(m.group(1)) if m else 0
    m = re.search(r"\b1\s*:\s*(\d{2,4})", full_text)
    stated_scale = int(m.group(1)) if m else None
    cls = re.search(r"([A-Z]{2,4}) CLASS:\s*([^\n]+)", full_text)
    special_purpose = bool(re.search(r"Special (?:Service|Purpose)|Research Vessel|SPS", full_text, re.I))
    passenger = bool(re.search(r"\bpassenger ship\b", full_text, re.I)) and not special_purpose
    declared = {}
    for dm in re.finditer(r"([A-Z'’ ]+DECK):\s*((?:\d+\s+(?:SINGLE|DOUBLE)(?:\s+AND\s+)?)+)\s*CABINS", full_text):
        deck = DECK_ALIASES.get(dm.group(1).strip(), dm.group(1).strip())
        nums = dict((k.lower(), int(v)) for v, k in re.findall(r"(\d+)\s+(SINGLE|DOUBLE)", dm.group(2)))
        declared[deck] = nums
    title = re.search(r"VESSEL NAME:\s*([^\n]+)", full_text) or re.search(r"DTU AQUA\s+([^\n]+)", full_text)
    ship_name = (title.group(1).strip().title() if title else Path(path).stem)

    # -- scale from frame-number rows ----------------------------------------------------------
    rows: dict[int, list[tuple[int, float]]] = {}
    for w in words:
        if re.fullmatch(r"\d{1,3}", w["text"]) and int(w["text"]) % 5 == 0:
            rows.setdefault(round(w["top"]), []).append((int(w["text"]), (w["x0"] + w["x1"]) / 2))
    frame_rows = {y: sorted(set(v)) for y, v in rows.items() if len(v) >= 10}
    if not frame_rows:
        raise DesignError("no frame-number scale found on the drawing; can't measure positions")
    a, b = _fit([p for v in frame_rows.values() for p in v])  # x_pt = a + b * frame
    if not spacing:
        spacing = 0.6
        notes.append("frame spacing not stated; assumed 0.60 m (check)")
    pt_per_m = b / spacing
    implied_scale = 72 / 25.4 * 1000 / pt_per_m
    scale_note = f"measured scale 1:{implied_scale:.0f}"
    if stated_scale:
        ok = abs(implied_scale - stated_scale) / stated_scale < 0.02
        scale_note += f", title block says 1:{stated_scale} ({'agrees' if ok else 'DIFFERS: check'})"
    to_x = lambda x_pt: (x_pt - a) / b * spacing  # noqa: E731  metres from frame 0

    # -- deck plans and heights ----------------------------------------------------------------
    titles = []
    for w in words:
        if w["size"] < 10:
            continue
        left = [v for v in words if abs(v["top"] - w["top"]) < 2 and 0 < w["x0"] - v["x1"] < 15 and v["size"] >= 10]
        name = (left[0]["text"] + " " + w["text"]) if left else w["text"]
        if TITLE_RE.match(name) or name in {"TANKTOP", "PROFILE"}:
            titles.append((DECK_ALIASES.get(name, name), w["top"], w["x0"]))
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
        plans.append({"deck": name, "band": band, "title_x": x, "centreline_pt": cl[0] if cl else None,
                      "height_m": legend.get(name, default_h)})

    # -- rooms ------------------------------------------------------------------------------------
    elements: list[dict] = []
    counter: dict[str, int] = {}
    all_blocks = _blocks(words)
    for blk in all_blocks:
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
    for deck, nums in declared.items():
        elements.append({"id": f"DECL-{deck.split()[0][:3].upper()}", "type": "declaration",
                         "name": f"Stated on drawing: {deck_title(deck)}", "deck": deck_title(deck),
                         "props": {"declared_single": nums.get("single", 0), "declared_double": nums.get("double", 0)}})
    if not elements:
        raise DesignError("no rooms found on the drawing")

    ship_d = {"name": ship_name, "ship_type": "special_purpose" if special_purpose else ("passenger" if passenger else "cargo"),
              "passengers": 0, "persons_total": persons, "length_m": lbp or loa or 0, "breadth_m": breadth or 0,
              "notes": f"Read from drawing {Path(path).name}. Keel-laying date and gross tonnage are not on the drawing.",
              "complete_arrangement": bool(persons and declared) or "GENERAL ARRANGEMENT" in full_text.upper()}
    if ship:
        ship_d.update(ship)
    interp = {
        "source": Path(path).name, "page": page_no + 1, "scale": scale_note,
        "frame_spacing_m": spacing, "points_per_metre": round(pt_per_m, 3),
        "principal_particulars": {"loa_m": loa, "lbp_m": lbp, "breadth_m": breadth, "depth_m": depth,
                                  "accommodation_persons": persons,
                                  "class": f"{cls.group(1)}: {cls.group(2).strip()}" if cls else None},
        "decks": [{"deck": deck_title(p["deck"]), "height_m": p["height_m"],
                   "centreline": "estimated" if p.get("centreline_estimated") else "from frame row"} for p in plans],
        "found": {t: sum(1 for e in elements if e["type"] == t) for t in ("cabin", "hospital", "space", "declaration")},
        "declared_cabins": declared,
        "notes": notes + ["Positions are read from label positions; room footprints are approximated from stated "
                          "areas. An engineer should confirm the reading before relying on the result."],
    }
    d = design_from_dict({"title": f"Drawing: {ship_name} general arrangement", "ship": ship_d, "elements": elements,
                          "source_format": "pdf-drawing", "interpretation": interp}, source_file=Path(path).name)
    return d
