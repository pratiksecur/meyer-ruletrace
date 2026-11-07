"""Check a picture: a screenshot, drawing excerpt, sketch or photo of a layout with dimensions written on it.

For interior designers: drop in what is on the screen, get PASS / FAIL / NEEDS REVIEW against the rules.

Gemini reads every value written on the image and says what it measures ("door clear width", "turning space
diameter") and which space it belongs to. Code then:
  * confirms the value is really written there: local OCR of the same image must find the value's digits inside
    the box Gemini reports (a value Gemini cannot point to on the image is not used);
  * parses the value and unit as written (30", 2'-6", 1500 mm) and checks it against the rules like any other
    design value. Gemini never decides PASS or FAIL.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from ..compliance.check import DesignRow
from ..compliance.units import parse_value
from .vision import ask_gemini

PROMPT = """This image shows part of a ship interior design: a cabin or space layout, a drawing excerpt, a screenshot
from a design tool, a sketch or a photo, with dimensions or values written on it.
List every dimension or value that is WRITTEN on the image. For each return:
- "space": the room or space it belongs to, as written or as evident from the image (e.g. "Accessible cabin bathroom")
- "parameter": what it measures, in the words an engineer would use, e.g. "door clear width", "turning space diameter",
  "walking surface clear width", "water closet seat height", "grab bar height above finish floor",
  "work surface height", "berth length", "corridor clear width", "stair angle of inclination"
- "value_text": the value exactly as written, e.g. "30\\"", "1500", "2'-6\\"", "760 mm"
- "unit": the unit as written or as stated on the image (mm, cm, m, in, ft, degree), or null if none is given
- "box_2d": [ymin, xmin, ymax, xmax] of the written value, normalized to 0-1000
Never invent a value that is not written on the image. Return only JSON: {"values": [...]}"""

MAX_SIDE = 2400


@dataclass
class PictureValue:
    space: str
    parameter: str
    value_text: str
    unit: str
    value: float | None
    bbox_px: tuple[int, int, int, int]
    confirmed: bool = False
    evidence: str = ""
    notes: list[str] = field(default_factory=list)


def normalize_image(data: bytes, max_side: int = MAX_SIDE) -> tuple[bytes, "object"]:
    """Any uploaded image -> PNG bytes (longest side at most max_side) and the PIL image."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 200_000_000  # large drawing sheets are legitimate input
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), img


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def pdf_page_png(path, page_no: int, max_side: int = 3600) -> bytes:
    """A drawing page rendered as a picture (longest side at most max_side)."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        pg = pdf.pages[page_no - 1]
        dpi = max(50, min(200, int(max_side * 72 / max(float(pg.width), float(pg.height)))))
        img = pg.to_image(resolution=dpi).original.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def read_picture(data: bytes, llm, words: list | None = None, max_side: int = MAX_SIDE) -> tuple[list[PictureValue], bytes]:
    """Gemini reads the values; local OCR confirms each one at the position Gemini gives."""
    import numpy as np

    from .extract import _engine

    png, img = normalize_image(data, max_side)
    W, H = img.size
    if words is None:
        res = _engine()(np.array(img))
        words = [(t.strip(), min(q[0] for q in b), min(q[1] for q in b), max(q[0] for q in b), max(q[1] for q in b))
                 for b, t in zip(res.boxes if res.boxes is not None else [], res.txts or [])]
    out = []
    for p in ask_gemini(llm, png, prompt=PROMPT, list_key="values"):
        try:
            ymin, xmin, ymax, xmax = (float(v) for v in p.get("box_2d") or [])
        except (TypeError, ValueError):
            continue
        box = (int(xmin / 1000 * W), int(ymin / 1000 * H), int(xmax / 1000 * W), int(ymax / 1000 * H))
        text = str(p.get("value_text") or "").strip()
        unit = str(p.get("unit") or "").strip()
        if unit.lower() in ("null", "none"):
            unit = ""
        value, unit = parse_value(text, unit)
        pad = max(12, int(0.01 * max(W, H)))
        inside = [w for w in words if w[3] >= box[0] - pad and w[1] <= box[2] + pad and w[4] >= box[1] - pad
                  and w[2] <= box[3] + pad]
        d = _digits(text)
        hit = next((w for w in inside if d and d in _digits(w[0])), None)
        v = PictureValue(space=str(p.get("space") or "").strip(), parameter=str(p.get("parameter") or "").strip(),
                         value_text=text, unit=unit, value=value, bbox_px=box, confirmed=hit is not None)
        v.evidence = (f'OCR reads "{hit[0]}" at that spot' if hit else
                      f'OCR at that spot: "{" ".join(w[0] for w in inside)[:40]}"' if inside else "no text found at that spot")
        if value is None:
            v.notes.append("value not understood as a number")
        if not unit:
            v.notes.append("no unit on the image: give the unit before checking")
        out.append(v)
    return out, png


def to_rows(values: list[PictureValue], name: str) -> list[DesignRow]:
    """Confirmed values become design rows; the evidence box is kept for the UI."""
    rows = []
    for i, v in enumerate([v for v in values if v.confirmed], start=1):
        rows.append(DesignRow(
            item_id=f"P{i:02d}", space=v.space, parameter=v.parameter, value=v.value, unit=v.unit,
            raw_value=v.value_text,
            source=f'{name}: "{v.value_text}" read by Gemini, confirmed in code ({v.evidence})',
            notes="; ".join(v.notes), source_file=f"upload:{name}", source_page=1,
            source_bbox=",".join(str(x) for x in v.bbox_px)))
    return rows
