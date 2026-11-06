"""Read an IFC 3D model (open BIM standard, IFC2X3 / IFC4) into the design model.

What is read:
- element class → design type (IfcDoor → door, IfcStair → stair, IfcWall → bulkhead, …);
  a "RuleTrace" property set with ``Type`` overrides the mapping for anything else;
- properties from every property set, normalised to snake_case (ClearWidth → clear_width),
  plus the standard ``Pset_WallCommon.FireRating`` / ``Pset_DoorCommon.FireRating`` → fire_class;
- geometry: the world-space bounding box of each element's 3D shape;
- the ship profile from a ``RuleTrace_Ship`` property set on IfcProject (or passed in).

Deliberate limits, for accuracy: a door's ``OverallWidth`` is NOT treated as its clear width
(the frame takes space). It is kept as ``overall_width`` and the clear-width rule reports
"can't check" until a clear width is modelled.
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import Design, DesignError, Element, design_from_dict

CLASS_MAP = {
    "IfcDoor": "door", "IfcStair": "stair", "IfcStairFlight": "stair", "IfcWall": "bulkhead",
    "IfcWallStandardCase": "bulkhead", "IfcRamp": "ramp", "IfcRampFlight": "ramp", "IfcEngine": "diesel_engine",
}
SPACE_TYPES = {"corridor": "corridor", "fire_zone": "fire_zone", "main vertical zone": "fire_zone"}
UNIT_HINTS = {"clear_width": "mm", "threshold_height": "mm", "height_above_deck": "mm", "overall_width": "mm",
              "height_above_hull": "m"}


def _snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.replace(" ", "_"))
    return re.sub(r"_+", "_", s).lower()


def _value(key: str, v):
    """Numbers in IFC are in model length units (we convert mm/m via the unit hint)."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)) and key in UNIT_HINTS:
        return f"{v:g} {UNIT_HINTS[key]}"
    return v


def design_from_ifc(path: Path, ship: dict | None = None) -> Design:
    try:
        import ifcopenshell
        import ifcopenshell.geom
        import ifcopenshell.util.element as uel
    except ImportError as exc:  # pragma: no cover
        raise DesignError("IFC import needs: pip install ifcopenshell") from exc
    try:
        f = ifcopenshell.open(str(path))
    except Exception as exc:
        raise DesignError(f"could not open IFC file: {exc}") from exc

    project = (f.by_type("IfcProject") or [None])[0]
    ship_d = dict(ship or {})
    if project is not None and not ship_d:
        ps = uel.get_psets(project).get("RuleTrace_Ship", {})
        ship_d = {_snake(k): v for k, v in ps.items() if k != "id"}
        if isinstance(ship_d.get("operating_areas"), str):
            ship_d["operating_areas"] = [a.strip() for a in ship_d["operating_areas"].split(",") if a.strip()]
        ship_d.setdefault("name", project.Name or Path(path).stem)
    if not ship_d.get("name"):
        raise DesignError("IFC has no RuleTrace_Ship property set; provide the ship profile with the upload")

    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    elements: list[dict] = []
    for prod in f.by_type("IfcProduct"):
        if prod.is_a("IfcSpatialStructureElement") and not prod.is_a("IfcSpace"):
            continue
        psets = uel.get_psets(prod)
        rt = psets.get("RuleTrace", {})
        etype = rt.get("Type") or CLASS_MAP.get(prod.is_a())
        if etype is None and prod.is_a("IfcSpace"):
            label = f"{prod.ObjectType or ''} {prod.LongName or ''} {prod.Name or ''}".lower()
            etype = next((t for k, t in SPACE_TYPES.items() if k in label), None)
        if etype is None and prod.is_a("IfcLightFixture"):
            etype = "low_location_lighting" if rt.get("Role") == "low_location_lighting" else "nav_light"
        if etype is None:
            continue
        props: dict = {}
        for pname, values in psets.items():
            for k, v in values.items():
                key = _snake(k)
                if key in {"id", "type"}:
                    continue
                if key == "fire_rating":
                    key = "fire_class"
                props.setdefault(key, _value(key, v))
        if prod.is_a("IfcDoor") and getattr(prod, "OverallWidth", None):
            props.setdefault("overall_width", _value("overall_width", prod.OverallWidth * 1000
                                                     if prod.OverallWidth < 10 else prod.OverallWidth))
        box = None
        if prod.Representation is not None:
            try:
                shape = ifcopenshell.geom.create_shape(settings, prod)
                v = shape.geometry.verts
                xs, ys, zs = v[0::3], v[1::3], v[2::3]
                box = (min(xs), min(ys), min(zs), max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
                box = tuple(round(b, 4) for b in box)
            except Exception:
                box = None
        elements.append({"id": rt.get("Id") or prod.Tag or prod.GlobalId, "type": etype,
                         "name": prod.Name or "", "deck": rt.get("Deck", ""), "zone": rt.get("Zone", ""),
                         "props": props, "box": list(box) if box else None, "source": f"IFC {prod.GlobalId}"})
    d = design_from_dict({"title": f"IFC model: {Path(path).name}", "ship": ship_d, "elements": elements,
                          "source_format": "ifc"}, source_file=Path(path).name)
    return d


__all__ = ["design_from_ifc", "Element"]
