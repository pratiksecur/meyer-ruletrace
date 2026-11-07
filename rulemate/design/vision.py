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
    key = hashlib.sha1(png + llm.model.encode() + prompt.encode()).hexdigest()[:20]
    cache = CACHE / f"{key}.json"
    if cache.exists():
        raw = json.loads(cache.read_text())["raw"]
    else:
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                 {"type": "image_url", "image_url": {"url": url}}]}]
        raw = llm.complete(messages, max_tokens=max_tokens)
        CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"model": llm.model, "raw": raw}))
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
        printed = re.search(r"\((\d{1,2})\)", label)  # "SENIOR SCIENTIST (2)": the drawing prints the berths
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


def read_sheet(path: Path, page_no: int, llm, words: list[Word], dpi: int = 150) -> list[VisionRoom]:
    png, w, h = render_png(path, page_no, dpi)
    return verify(ask_gemini(llm, png), words, w, h)
