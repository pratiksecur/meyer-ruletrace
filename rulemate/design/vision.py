"""Gemini reads a GA drawing sheet; code confirms every reading against the drawing itself.

Why: OCR reads characters, not drawings. On CAD sheets its stroke-font output is noisy ("SARWAY", "HOSPNAL"),
it cannot tell a room label from a leader-line callout, and it cannot count berths that are drawn, not written.
A multimodal model reads the sheet the way an engineer does: room names, room numbers, the berths drawn in a cabin.

Trust: Gemini only proposes. A reading is used for a check only when code confirms it:
  * the label text Gemini reports is really on the sheet where Gemini says it is (OCR words inside the reported box,
    fuzzy match because of the CAD font), or its room number is;
  * a berth count must agree with what the label itself says (CREW DOUBLE = 2, SINGLE = 1) when it says anything.
Unconfirmed readings are shown as "Gemini only" and never decide a verdict.
"""

from __future__ import annotations

from ..config import CACHE_DIR

import base64
import io
import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .extract import Word

PROMPT = """This image is one sheet of a ship General Arrangement (GA) drawing.
List every labelled room or space whose label you can read on the sheet (deck plans and elevations).
For each, return:
- "label": the room name exactly as printed (e.g. "CREW DOUBLE", "SCIENCE", "HOSPITAL", "STAIRWAY")
- "room_number": the room number printed under the name exactly as printed (e.g. "01-36-2"), or null
- "kind": one of "stateroom", "hospital", "stairway", "toilet_shower", "lab", "mess", "corridor", "other"
- "berths": for staterooms only, the number of berths/bunks DRAWN inside the room, or null if you cannot see them
- "box_2d": [ymin, xmin, ymax, xmax] of the label text, normalized to 0-1000
Rules: copy text exactly; never invent a label or number you cannot read; skip title block, notes and revision tables.
Return only JSON: {"rooms": [...]}"""

KIND_WORDS = {"single": 1, "double": 2, "triple": 3, "quad": 4}


@dataclass
class VisionRoom:
    label: str
    room_number: str | None
    kind: str
    berths: int | None
    bbox: tuple[float, float, float, float]  # PDF points
    confirmed: bool = False
    berths_confirmed: bool = False
    evidence: str = ""  # what code found on the sheet
    notes: list[str] = field(default_factory=list)


def render_png(path: Path, page_no: int, dpi: int = 150) -> tuple[bytes, float, float]:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        pg = pdf.pages[page_no - 1]
        dpi = max(50, min(dpi, int(5000 * 72 / max(float(pg.width), float(pg.height)))))  # large sheets: cap size
        img = pg.to_image(resolution=dpi).original.convert("RGB")
        w, h = float(pg.width), float(pg.height)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), w, h


CACHE = CACHE_DIR / "gemini"


def ask_gemini(llm, png: bytes, max_tokens: int = 32000, prompt: str | None = None, list_key: str = "rooms") -> list[dict]:
    """One request per sheet. Responses are cached by image, model and prompt (the free tier allows 20 requests
    per day per model), and the raw answer is kept next to the parsed one for review."""
    import hashlib

    prompt = prompt or PROMPT
    # a reading by any of the configured Gemini models counts (the model actually used is stored with it)
    names = list(dict.fromkeys([llm.model] + list(getattr(llm, "models", []))))
    hit = next((CACHE / f"{hashlib.sha1(png + m.encode() + prompt.encode()).hexdigest()[:20]}.json" for m in names
                if (CACHE / f"{hashlib.sha1(png + m.encode() + prompt.encode()).hexdigest()[:20]}.json").exists()), None)
    if hit is not None:
        raw = json.loads(hit.read_text())["raw"]
    else:
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                 {"type": "image_url", "image_url": {"url": url}}]}]
        raw = llm.complete(messages, max_tokens=max_tokens)
        used = llm.model  # after a rotation, the model that answered
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"{hashlib.sha1(png + used.encode() + prompt.encode()).hexdigest()[:20]}.json").write_text(
            json.dumps({"model": used, "raw": raw}))
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return []
    try:
        return list(json.loads(m.group(0)).get(list_key) or [])
    except json.JSONDecodeError:
        return []


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def verify(proposals: list[dict], words: list[Word], page_w: float, page_h: float, pad: float = 14.0) -> list[VisionRoom]:
    """Confirm each Gemini reading with the OCR words found inside the box Gemini reported."""
    out = []
    for p in proposals:
        try:
            ymin, xmin, ymax, xmax = (float(v) for v in p.get("box_2d") or [])
        except (TypeError, ValueError):
            continue
        box = (xmin / 1000 * page_w, ymin / 1000 * page_h, xmax / 1000 * page_w, ymax / 1000 * page_h)
        label = str(p.get("label") or "").strip()
        num = (str(p.get("room_number")).strip() if p.get("room_number") not in (None, "", "null") else None)
        berths = p.get("berths")
        berths = int(berths) if isinstance(berths, (int, float)) and 0 < berths < 20 else None
        r = VisionRoom(label=label, room_number=num, kind=str(p.get("kind") or "other"), berths=berths,
                       bbox=tuple(round(v, 1) for v in box))
        inside = [w for w in words if w.x1 >= box[0] - pad and w.x0 <= box[2] + pad
                  and w.y1 >= box[1] - pad and w.y0 <= box[3] + pad]
        text = " ".join(w.text for w in inside)
        label_ok = bool(label) and (_sim(label, text) >= 0.6 or
                                    any(_sim(part, w.text) >= 0.7 for part in label.split() if len(part) > 3 for w in inside))
        num_ok = bool(num) and any(_sim(num, w.text) >= 0.8 for w in inside)
        r.confirmed = label_ok or num_ok
        r.evidence = f'OCR at that spot: "{text[:60]}"' if inside else "no OCR text at that spot"
        # berths printed on the drawing: "SENIOR SCIENTIST (2)", "2M" / "2 MAN" (2-man cabin), DOUBLE / SINGLE
        printed = re.search(r"\((\d{1,2})\)", label) or re.search(r"(?<![\d.])(\d)\s*-?\s*M(?:AN)?\b", label.upper())
        stated = int(printed.group(1)) if printed else next((v for k, v in KIND_WORDS.items() if k in label.lower()), None)
        if r.berths is not None and stated is not None:
            r.berths_confirmed = r.berths == stated
            if not r.berths_confirmed:
                r.notes.append(f"Gemini counted {r.berths} berths but the label says {stated}")
        elif stated is not None and r.berths is None:
            r.berths, r.berths_confirmed = stated, True  # the label states it
            r.notes.append("berths from the label")
        out.append(r)
    return out


TILE_PT = 1400  # a sheet larger than this (about A3) is read in tiles so small labels stay legible


def tile_grid(w: float, h: float, max_tiles: int | None = None) -> list[tuple[float, float, float, float]]:
    """Overlapping tiles covering a sheet, at most max_tiles (GEMINI_MAX_TILES, default 4: one request each)."""
    import math
    import os

    max_tiles = max_tiles or int(os.getenv("GEMINI_MAX_TILES", "4"))
    cols, rows = max(1, math.ceil(w / TILE_PT)), max(1, math.ceil(h / TILE_PT))
    while cols * rows > max_tiles:
        if cols >= rows and cols > 1:
            cols -= 1
        elif rows > 1:
            rows -= 1
    ox, oy = 0.06 * w / cols if cols > 1 else 0, 0.06 * h / rows if rows > 1 else 0
    return [(max(0, c * w / cols - ox), max(0, r * h / rows - oy), min(w, (c + 1) * w / cols + ox),
             min(h, (r + 1) * h / rows + oy)) for r in range(rows) for c in range(cols)]


def read_sheet(path: Path, page_no: int, llm, words: list[Word], dpi: int = 150) -> list[VisionRoom]:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        w, h = float(pdf.pages[page_no - 1].width), float(pdf.pages[page_no - 1].height)
    tiles = tile_grid(w, h)
    if len(tiles) == 1:
        png, w, h = render_png(path, page_no, dpi)
        return verify(ask_gemini(llm, png), words, w, h)
    proposals: list[dict] = []
    for (x0, y0, x1, y1) in tiles:  # each tile at full resolution; boxes mapped back to the whole sheet
        with pdfplumber.open(path) as pdf:
            res = max(72, min(200, int(3000 * 72 / max(x1 - x0, y1 - y0))))
            img = pdf.pages[page_no - 1].crop((x0, y0, x1, y1)).to_image(resolution=res).original.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        for p in ask_gemini(llm, buf.getvalue()):
            try:
                ty0, tx0, ty1, tx1 = (float(v) for v in p.get("box_2d") or [])
            except (TypeError, ValueError):
                continue
            p["box_2d"] = [(y0 + ty0 / 1000 * (y1 - y0)) / h * 1000, (x0 + tx0 / 1000 * (x1 - x0)) / w * 1000,
                           (y0 + ty1 / 1000 * (y1 - y0)) / h * 1000, (x0 + tx1 / 1000 * (x1 - x0)) / w * 1000]
            proposals.append(p)
    # the same label seen in two overlapping tiles is one reading
    kept: list[dict] = []
    for p in proposals:
        cy, cx = (p["box_2d"][0] + p["box_2d"][2]) / 2, (p["box_2d"][1] + p["box_2d"][3]) / 2
        if not any(_norm(p.get("label", "")) == _norm(k.get("label", "")) and
                   abs(cy - (k["box_2d"][0] + k["box_2d"][2]) / 2) < 8 and abs(cx - (k["box_2d"][1] + k["box_2d"][3]) / 2) < 8
                   for k in kept):
            kept.append(p)
    return verify(kept, words, w, h)
