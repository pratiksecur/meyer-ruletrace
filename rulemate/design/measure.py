"""Measure design values from the vector geometry of a CAD drawing (PDF).

Stair inclination from an elevation / profile view: a stair flight is drawn as parallel sloped stringer lines.
Its angle comes straight from the line coordinates, so it does not depend on the drawing scale. Each flight is
named by an OCR label containing STAIRWAY (with its room number), and flights next to machinery-space labels
are marked, because 46 CFR 190.10-25(b) exempts stairways within machinery spaces.

Precision: CAD lines exported to PDF carry coordinate rounding (a 50 degree stringer reads 49.97-50.02 degrees), so a
flight's angle is the mean of its stringers rounded to 0.1 degree.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .extract import Word, similar

MIN_LEN = 25.0  # pt: shorter sloped lines are hatching, handrail ends, symbols
STAIR_RANGE = (30.0, 65.0)  # degrees from horizontal; ladders are steeper, ramps flatter
LABEL_DIST = 70.0  # pt from the flight to its STAIRWAY label
_MACHINERY = re.compile(r"ENGINE|MACH|MOTOR|GENERATOR|PUMP ROOM|ECR|SWITCHBOARD", re.I)


@dataclass
class Flight:
    angle: float  # degrees, rounded to 0.1
    bbox: tuple[float, float, float, float]  # PDF points
    stringers: int
    label: str = ""
    room_no: str = ""
    machinery: bool = False
    raw_angles: tuple[float, ...] = ()


def _sloped_lines(page) -> list[tuple[float, float, tuple]]:
    lines = []
    for l in page.lines:
        (ax, ay), (bx, by) = l["pts"][0], l["pts"][-1]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < MIN_LEN:
            continue
        ang = abs(math.degrees(math.atan2(dy, dx))) % 180
        ang = min(ang, 180 - ang)
        if STAIR_RANGE[0] <= ang <= STAIR_RANGE[1]:
            lines.append((ang, length, (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))))
    return lines


def _center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def stair_flights(path: Path, page_no: int, words: list[Word]) -> list[Flight]:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return flights_from_lines(_sloped_lines(pdf.pages[page_no - 1]), words)


def flights_from_lines(lines: list[tuple[float, float, tuple]], words: list[Word]) -> list[Flight]:
    """``lines``: (angle, length, bbox) of sloped lines in an elevation view."""
    # parallel lines lying on top of each other / side by side form one flight
    groups: list[list] = []
    for ang, length, b in sorted(lines, key=lambda x: (x[2][1], x[2][0])):
        for g in groups:
            ga, _, gb = g[0]
            if abs(ga - ang) < 1.0 and abs(_center(gb)[0] - _center(b)[0]) < 12 and abs(_center(gb)[1] - _center(b)[1]) < 12:
                g.append((ang, length, b))
                break
        else:
            groups.append([(ang, length, b)])

    labels = [w for w in words if similar(w.text, "STAIRWAY", 0.7) or "STAIR" in w.text.upper()]
    numbers = [w for w in words if re.fullmatch(r"\d{1,2}-\d{1,2}-[\dCOo]", w.text.strip())]
    machinery = [w for w in words if _MACHINERY.search(w.text)]

    flights = []
    for g in groups:
        b = (min(x[2][0] for x in g), min(x[2][1] for x in g), max(x[2][2] for x in g), max(x[2][3] for x in g))
        flights.append((g, b, _center(b)))

    def dist(w, c):
        return math.hypot(w.cx - c[0], (w.y0 + w.y1) / 2 - c[1])

    # labels go one-to-one to flights, closest pairs first; a flight near stair labels but without its own
    # (OCR missed it) is kept as "label not read" instead of borrowing a neighbour's label
    pairs = sorted((dist(w, c), i, j) for i, (_, _, c) in enumerate(flights) for j, w in enumerate(labels)
                   if dist(w, c) <= LABEL_DIST)
    own: dict[int, Word] = {}
    used: set[int] = set()
    for _, i, j in pairs:
        if i not in own and j not in used:
            own[i] = labels[j]
            used.add(j)
    near_any = {i for _, i, _ in pairs}

    result = []
    for i, (g, b, (cx, cy)) in enumerate(flights):
        if i not in near_any:
            continue  # sloped lines with no stair label nearby: crane, mast, hull lines
        lab = own.get(i)
        room_no = ""
        if lab is not None:
            num = min(numbers, key=lambda w: math.hypot(w.cx - lab.cx, w.y0 - lab.y1), default=None)
            if num and math.hypot(num.cx - lab.cx, num.y0 - lab.y1) < 20:
                room_no = re.sub(r"[COo]$", "0", num.text.strip())
        mach = any(math.hypot(w.cx - cx, (w.y0 + w.y1) / 2 - cy) < 90 for w in machinery)
        raw = tuple(round(x[0], 2) for x in g)
        result.append(Flight(angle=round(sum(raw) / len(raw), 1), bbox=tuple(round(v, 1) for v in b),
                             stringers=len(g), label="STAIRWAY" if lab is not None else "STAIRWAY (label not read)",
                             room_no=room_no, machinery=mach, raw_angles=raw))
    return result
