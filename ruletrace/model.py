"""Ship design model: a ship profile plus 3D design elements with properties.

Every importer (native JSON, IFC, drawings) produces this model, and the rule engine only
reads this model, so a new input format never changes a rule.

Coordinates are metres in the ship frame: x aft→forward from the aft perpendicular,
y port(−)→starboard(+) from centreline, z up from baseline. ``box`` is
(x, y, z, length_x, width_y, height_z) of an axis-aligned bounding box.
Property values are strings with units ("850 mm", "1:10", "A-60") or plain numbers/booleans.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

SHIP_TYPES = {"passenger", "cargo", "special_purpose", "research", "ro_ro_passenger"}


@dataclass
class Ship:
    name: str
    ship_type: str
    passengers: int = 0
    persons_total: int = 0
    length_m: float = 0.0
    breadth_m: float = 0.0
    gross_tonnage: float = 0.0
    keel_laid: date | None = None
    flag: str = ""
    operating_areas: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def is_passenger_ship(self) -> bool:
        """SOLAS I/2: a passenger ship carries more than twelve passengers."""
        return self.passengers > 12 or self.ship_type in {"passenger", "ro_ro_passenger"}


@dataclass
class Element:
    id: str
    type: str
    name: str = ""
    deck: str = ""
    zone: str = ""
    props: dict = field(default_factory=dict)
    box: tuple[float, float, float, float, float, float] | None = None
    source: str = ""   # where this element came from: file, IFC GlobalId, drawing page

    def geom(self, key: str) -> float | None:
        if not self.box:
            return None
        x, y, z, lx, wy, hz = self.box
        return {"length": lx, "width": wy, "height": hz, "x_min": x, "x_max": x + lx,
                "y_min": y, "y_max": y + wy, "z_min": z, "z_max": z + hz}.get(key)


@dataclass
class Design:
    ship: Ship
    elements: list[Element]
    title: str = ""
    source_format: str = "json"
    source_file: str = ""
    expected: list[dict] = field(default_factory=list)   # labelled answers, for evaluation only

    def by_id(self, element_id: str) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ship"]["keel_laid"] = self.ship.keel_laid.isoformat() if self.ship.keel_laid else None
        return d


class DesignError(ValueError):
    pass


def _ship(d: dict) -> Ship:
    try:
        kl = d.get("keel_laid")
        ship = Ship(
            name=str(d["name"]), ship_type=str(d.get("ship_type", "passenger")),
            passengers=int(d.get("passengers", 0)), persons_total=int(d.get("persons_total", 0)),
            length_m=float(d.get("length_m", 0)), breadth_m=float(d.get("breadth_m", 0)),
            gross_tonnage=float(d.get("gross_tonnage", 0)),
            keel_laid=date.fromisoformat(kl) if kl else None, flag=str(d.get("flag", "")),
            operating_areas=[str(a) for a in d.get("operating_areas", [])], notes=str(d.get("notes", "")))
    except (KeyError, ValueError, TypeError) as exc:
        raise DesignError(f"invalid ship profile: {exc}") from exc
    if ship.ship_type not in SHIP_TYPES:
        raise DesignError(f"ship_type must be one of {sorted(SHIP_TYPES)}")
    return ship


def design_from_dict(d: dict, source_file: str = "") -> Design:
    if "ship" not in d or "elements" not in d:
        raise DesignError("a design needs 'ship' and 'elements'")
    elements, seen = [], set()
    for raw in d["elements"]:
        if "id" not in raw or "type" not in raw:
            raise DesignError(f"every element needs 'id' and 'type': {raw}")
        if raw["id"] in seen:
            raise DesignError(f"duplicate element id '{raw['id']}'")
        seen.add(raw["id"])
        box = raw.get("box")
        if box is not None and (len(box) != 6 or any(not isinstance(v, (int, float)) for v in box)):
            raise DesignError(f"element {raw['id']}: box must be 6 numbers [x, y, z, length, width, height]")
        elements.append(Element(id=str(raw["id"]), type=str(raw["type"]), name=str(raw.get("name", "")),
                                deck=str(raw.get("deck", "")), zone=str(raw.get("zone", "")),
                                props=dict(raw.get("props", {})), box=tuple(box) if box else None,
                                source=str(raw.get("source", source_file))))
    return Design(ship=_ship(d["ship"]), elements=elements, title=str(d.get("title", "")),
                  source_format=str(d.get("source_format", "json")), source_file=source_file,
                  expected=list(d.get("expected", [])))


def load_design(path: Path) -> Design:
    path = Path(path)
    if path.suffix.lower() == ".ifc":
        from .ifc_import import design_from_ifc
        return design_from_ifc(path)
    return design_from_dict(json.loads(path.read_text(encoding="utf-8")), source_file=path.name)
