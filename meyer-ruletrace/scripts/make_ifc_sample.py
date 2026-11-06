"""Build a small synthetic IFC4 model (a passenger-ship deck section) for testing the importer.

    python scripts/make_ifc_sample.py            # -> ruletrace/samples/demo_deck_section.ifc
"""

from __future__ import annotations

import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.api as api
import ifcopenshell.guid

OUT = Path(__file__).resolve().parent.parent / "ruletrace" / "samples" / "demo_deck_section.ifc"


def box_rep(f, ctx, x, y, z, dx, dy, dz):
    """An axis-aligned box as an extruded rectangle (metres)."""
    pt = f.createIfcCartesianPoint
    d3 = f.createIfcDirection
    prof = f.createIfcRectangleProfileDef("AREA", None,
                                          f.createIfcAxis2Placement2D(f.createIfcCartesianPoint((dx / 2, dy / 2))),
                                          dx, dy)
    solid = f.createIfcExtrudedAreaSolid(prof, f.createIfcAxis2Placement3D(pt((x, y, z)), d3((0., 0., 1.)),
                                                                            d3((1., 0., 0.))),
                                         d3((0., 0., 1.)), dz)
    return f.createIfcProductDefinitionShape(None, None, [f.createIfcShapeRepresentation(ctx, "Body", "SweptSolid",
                                                                                          [solid])])


def main(out: Path = OUT) -> Path:
    f = ifcopenshell.file(schema="IFC4")
    project = api.run("root.create_entity", f, ifc_class="IfcProject", name="Demo deck section (synthetic)")
    api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METRE"})
    model = api.run("context.add_context", f, context_type="Model")
    body = api.run("context.add_context", f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW",
                   parent=model)
    site = api.run("root.create_entity", f, ifc_class="IfcSite", name="Yard")
    ship = api.run("root.create_entity", f, ifc_class="IfcBuilding", name="Hull (demo)")
    storey = api.run("root.create_entity", f, ifc_class="IfcBuildingStorey", name="Deck 7")
    api.run("aggregate.assign_object", f, products=[site], relating_object=project)
    api.run("aggregate.assign_object", f, products=[ship], relating_object=site)
    api.run("aggregate.assign_object", f, products=[storey], relating_object=ship)
    ps = api.run("pset.add_pset", f, product=project, name="RuleTrace_Ship")
    api.run("pset.edit_pset", f, pset=ps, properties={
        "Name": "MS Demo Aurora (synthetic IFC)", "ShipType": "passenger", "Passengers": 2800, "LengthM": 300.0,
        "BreadthM": 38.0, "GrossTonnage": 180000.0, "KeelLaid": "2026-03-01", "Flag": "FI",
        "OperatingAreas": "us_ports, north_american_eca"})

    def add(cls, name, tag, box, psets, **attrs):
        e = api.run("root.create_entity", f, ifc_class=cls, name=name)
        if "Tag" in e.get_info():
            e.Tag = tag
        psets = {**psets, "RuleTrace": {"Id": tag, **psets.get("RuleTrace", {})}}
        for k, v in attrs.items():
            setattr(e, k, v)
        e.Representation = box_rep(f, body, *box)
        e.ObjectPlacement = f.createIfcLocalPlacement(None, f.createIfcAxis2Placement3D(
            f.createIfcCartesianPoint((0., 0., 0.)), None, None))
        if e.is_a("IfcSpatialElement"):
            api.run("aggregate.assign_object", f, products=[e], relating_object=storey)
        else:
            api.run("spatial.assign_container", f, products=[e], relating_structure=storey)
        for pname, props in psets.items():
            p = api.run("pset.add_pset", f, product=e, name=pname)
            api.run("pset.edit_pset", f, pset=p, properties=props)
        return e

    z = 30.0
    add("IfcWall", "Frame 195 zone boundary", "BH-195", (194.9, -19, z, 0.2, 38, 3),
        {"Pset_WallCommon": {"FireRating": "A-30"}, "RuleTrace": {"Boundary": "main_vertical_zone"}})
    add("IfcWall", "Frame 144 zone boundary", "BH-144", (143.9, -19, z, 0.2, 38, 3),
        {"Pset_WallCommon": {"FireRating": "A-60"}, "RuleTrace": {"Boundary": "main_vertical_zone"}})
    add("IfcDoor", "Accessible cabin 7310 door", "D-7310", (150, 0.6, z, 0.9, 0.1, 2.1),
        {"RuleTrace": {"ClearWidth": 800.0, "ThresholdHeight": 25.0, "AccessibleRoute": True}},
        OverallWidth=0.9, OverallHeight=2.1)
    add("IfcDoor", "Store door (no clear width modelled)", "D-7330", (158, 0.6, z, 0.9, 0.1, 2.1),
        {"RuleTrace": {"AccessibleRoute": True, "ThresholdHeight": 10.0}}, OverallWidth=0.9, OverallHeight=2.1)
    add("IfcStair", "Stairway 3B", "ST-3B", (182, 8, z - 3, 5, 2, 6),
        {"RuleTrace": {"ClearWidth": 850.0, "EscapeRoute": True}})
    add("IfcSpace", "Zone 3", "FZ3", (144, -19, z, 51, 38, 3), {"RuleTrace": {"Type": "fire_zone"}})
    out.parent.mkdir(parents=True, exist_ok=True)
    f.write(str(out))
    return out


if __name__ == "__main__":
    print("wrote", main(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT))
