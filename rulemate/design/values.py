"""Design values from any file: CSV, picture, or a drawing page (PDF).

    CSV      -> the rows as written
    picture  -> every dimension written on it, read by Gemini, confirmed by local OCR at the reported spot
    PDF page -> the same on the rendered page, plus stair angles measured from the drawing's vector lines
                (no Gemini needed for those; see measure.py)

Every value keeps where it came from. Unconfirmed readings are returned separately and never checked.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..compliance.check import DesignRow, read_design_csv


@dataclass
class FileValues:
    kind: str  # csv | picture | pdf
    rows: list[DesignRow] = field(default_factory=list)
    readings: list = field(default_factory=list)  # PictureValue, confirmed or not
    png: bytes | None = None  # what was read, for previews and evidence crops
    notes: list[str] = field(default_factory=list)


def values_from_file(data: bytes, name: str, llm=None, page: int = 1) -> FileValues:
    low = name.lower()
    if low.endswith(".csv"):
        return FileValues("csv", rows=read_design_csv(data))
    if low.endswith((".png", ".jpg", ".jpeg", ".webp")):
        if llm is None:
            raise ValueError("reading values from a picture needs Gemini (GEMINI_API_KEY)")
        from .picture import read_picture, to_rows

        vals, png = read_picture(data, llm)
        return FileValues("picture", rows=to_rows(vals, name), readings=vals, png=png)
    if low.endswith(".pdf"):
        return _pdf(data, name, llm, page)
    raise ValueError("upload a .csv, a picture (.png, .jpg) or a drawing (.pdf)")


def _pdf(data: bytes, name: str, llm, page: int) -> FileValues:
    import pdfplumber

    from .extract import ocr_page
    from .measure import stair_flights
    from .picture import pdf_page_png, read_picture, to_rows

    out = FileValues("pdf")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "drawing.pdf"
        path.write_bytes(data)
        with pdfplumber.open(path) as doc:
            n = len(doc.pages)
        if not 1 <= page <= n:
            raise ValueError(f"page must be between 1 and {n}")
        png = pdf_page_png(path, page)
        out.png = png
        if llm is not None:
            out.readings = _read_tiled(path, page, png, llm)
            out.rows += to_rows(out.readings, f"{name} p.{page}")
        else:
            out.notes.append("no Gemini key: written dimensions not read")
        # stair angles straight from the vector lines (elevation views)
        flights = [f for f in stair_flights(path, page, ocr_page(path, page, 200)) if not f.machinery]
        for i, f in enumerate(flights, start=1):
            out.rows.append(DesignRow(
                item_id=f"S{i:02d}", space=f"{f.label.title()} {f.room_no}".strip(),
                parameter="stairway angle of inclination", value=f.angle, unit="degree", raw_value=f"{f.angle:g}",
                source=f"{name} p.{page}: measured from {f.stringers} vector stringer line(s) "
                       f"{', '.join(f'{a:g}' for a in f.raw_angles)} deg"))
        if not flights:
            out.notes.append("no labelled stair flights in the vector lines of this page")
    return out


def _read_tiled(path, page: int, png: bytes, llm) -> list:
    """Written values on a drawing page; large sheets in tiles (GEMINI_MAX_TILES requests), boxes in page pixels."""
    import io

    import pdfplumber
    from PIL import Image

    from .picture import read_picture
    from .vision import tile_grid

    with pdfplumber.open(path) as doc:
        w, h = float(doc.pages[page - 1].width), float(doc.pages[page - 1].height)
    full = Image.open(io.BytesIO(png))
    sx, sy = full.width / w, full.height / h
    tiles = tile_grid(w, h)
    if len(tiles) == 1:
        return read_picture(png, llm, max_side=3600)[0]
    out = []
    for (x0, y0, x1, y1) in tiles:
        with pdfplumber.open(path) as doc:
            res = max(72, min(200, int(3000 * 72 / max(x1 - x0, y1 - y0))))
            img = doc.pages[page - 1].crop((x0, y0, x1, y1)).to_image(resolution=res).original.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        vals, _ = read_picture(buf.getvalue(), llm, max_side=3000)
        k = res / 72  # tile pixels per point
        for v in vals:  # tile pixels -> page points -> pixels of the whole-page preview
            bx = v.bbox_px
            v.bbox_px = (int((x0 + bx[0] / k) * sx), int((y0 + bx[1] / k) * sy), int((x0 + bx[2] / k) * sx),
                         int((y0 + bx[3] / k) * sy))
            if not any(o.value_text == v.value_text and o.parameter == v.parameter and abs(o.bbox_px[0] - v.bbox_px[0]) < 40
                       for o in out):
                out.append(v)
    return out
