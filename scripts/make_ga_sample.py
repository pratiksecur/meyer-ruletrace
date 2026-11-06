"""Draw a small synthetic general-arrangement PDF (two deck plans) with planted problems.

    python scripts/make_ga_sample.py      # -> ruletrace/samples/demo_ga_drawing.pdf

Planted problems (what a correct reader + checker must report):
- a 4 m² single cabin (below MLC minimum even for ships < 3,000 GT)
- a 7 m² two-person cabin on a special purpose ship (MLC: at least 7.5 m²)
- no hospital although 16 persons are on board
- title block says 3 double cabins on the shelter deck; the plan shows 2
- title block says accommodation for 16 persons; the plans have 14 berths
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "ruletrace" / "samples" / "demo_ga_drawing.pdf"
PT_PER_M = 72 / 25.4 * 1000 / 150      # 1:150
SPACING = 0.6
X0 = 90                                  # frame 0
W, H = landscape(A3)


def fx(frame: float) -> float:
    return X0 + frame * SPACING * PT_PER_M


def main(out: Path = OUT) -> Path:
    c = canvas.Canvas(str(out), pagesize=(W, H))
    top = lambda y: H - y  # noqa: E731  (work in top-down coordinates like the reader)

    def text(x, y, s, size):
        c.setFont("Helvetica", size)
        c.drawString(x, top(y), s)

    def plan(title, title_y, centre_y, cabins):
        c.setLineWidth(0.6)
        c.rect(fx(0), top(centre_y + 5 * PT_PER_M), fx(80) - fx(0), 10 * PT_PER_M)
        c.setDash(6, 3)
        c.line(fx(0), top(centre_y), fx(80), top(centre_y))
        c.setDash()
        for f in range(5, 81, 5):
            c.setFont("Helvetica", 6.6)
            c.drawCentredString(fx(f), top(centre_y - 2), str(f))
        for frame, offset_m, lines in cabins:
            for i, ln in enumerate(lines):
                text(fx(frame), centre_y + offset_m * PT_PER_M + i * 7.5, ln, 6.7)
        text(fx(62), title_y, title, 13.2)

    plan("SHELTER DECK", 300, 230, [
        (10, -3.5, ["1 PERS.", "12 m2"]), (20, -3.5, ["1 PERS.", "12 m2"]), (30, -3.5, ["1 PERS.", "12 m2"]),
        (40, -3.5, ["1 PERS.", "4 m2"]), (10, 2.5, ["2 PERS.", "15 m2"]), (25, 2.5, ["2 PERS.", "7 m2"]),
        (50, 2.0, ["MESS", "40 m2"]), (62, 2.0, ["GALLEY", "20 m2"])])
    plan("FO'C'SLE DECK", 520, 450, [
        (10, -3.5, ["1 PERS.", "(CAPTAIN)", "16 m2"]), (22, -3.5, ["1 PERS.", "10 m2"]),
        (34, -3.5, ["1 PERS.", "10 m2"]), (10, 2.5, ["1 PERS.", "10 m2"]), (22, 2.5, ["1 PERS.", "10 m2"]),
        (34, 2.5, ["1 PERS.", "10 m2"]), (50, 2.0, ["DAY ROOM", "30 m2"])])
    # profile legend (deck heights above baseline)
    for i, (name, h) in enumerate([("FO'C'SLE", "8400"), ("SHELTER", "5600")]):
        text(W - 150, 60 + i * 30, f"{name} DECK", 5.3)
        text(W - 160, 68 + i * 30, h, 5.3)
    for i, ln in enumerate(["VESSEL NAME: RV DEMO PETREL (SYNTHETIC)", "PRINCIPAL PARTICULARS",
                            "LENGTH OVER ALL :    52,00  m", "LENGTH BETWEEN P.P.  :    48,00  m",
                            "BREADTH MLD :    11,00  m", "FRAME SPACING :    0,60  m",
                            "BV CLASS: Special Service/Research Vessel", "ACCOMMODATION FOR 16 PERSONS",
                            "SHELTER DECK: 4 SINGLE AND 3 DOUBLE CABINS", "FO'C'SLE DECK: 6 SINGLE CABINS",
                            "Scale 1:150   Synthetic drawing for RuleTrace tests"]):
        text(W - 360, 640 + i * 14, ln, 9)
    c.save()
    return out


if __name__ == "__main__":
    print("wrote", main())
