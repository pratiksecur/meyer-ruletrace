"""Generate the synthetic sample designs in ruletrace/samples/.

All samples are invented for demonstration (Meyer Turku provides no real design files).
Each flawed sample lists the findings a correct checker must report (``expected``), so the
evaluation script can measure precision and recall.

    python scripts/make_samples.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "ruletrace" / "samples"
DECK_Z = 30.0  # deck 7 at 30 m above baseline


def el(id, type, name, box, zone="", deck="Deck 7", **props):
    return {"id": id, "type": type, "name": name, "deck": deck, "zone": zone, "box": box, "props": props}


def cruise() -> dict:
    z = DECK_Z
    e = [
        # fire zones (vertical extent: decks 5-7)
        el("FZ2", "fire_zone", "Main vertical zone 2", [100, -19, z - 6, 44, 38, 9], zone="MVZ2"),
        el("FZ3", "fire_zone", "Main vertical zone 3", [144, -19, z - 6, 51, 38, 9], zone="MVZ3"),
        el("FZ4", "fire_zone", "Main vertical zone 4", [195, -19, z - 6, 40, 38, 9], zone="MVZ4"),
        # zone boundary bulkheads
        el("BH-100", "bulkhead", "Frame 100 zone boundary", [99.9, -19, z - 6, 0.2, 38, 9], zone="MVZ2",
           boundary="main_vertical_zone", fire_class="A-60"),
        el("BH-144", "bulkhead", "Frame 144 zone boundary", [143.9, -19, z - 6, 0.2, 38, 9], zone="MVZ3",
           boundary="main_vertical_zone", fire_class="A-60"),
        el("BH-195", "bulkhead", "Frame 195 zone boundary", [194.9, -19, z - 6, 0.2, 38, 9], zone="MVZ4",
           boundary="main_vertical_zone", fire_class="A-30"),
        # stairways
        el("ST-3A", "stair", "Stairway 3A (midship)", [160, -6, z - 3, 6, 3, 6], zone="MVZ3",
           escape_route=True, clear_width="1200 mm"),
        el("ST-3B", "stair", "Stairway 3B (aft crew/pax)", [182, 8, z - 3, 5, 2, 6], zone="MVZ3",
           escape_route=True, clear_width="850 mm"),
        el("ST-4A", "stair", "Stairway 4A", [210, -6, z - 3, 6, 3, 6], zone="MVZ4", escape_route=True),
        # corridors
        el("COR-7-3", "corridor", "Deck 7 main corridor, zone 3", [146, -1, z, 46, 1.6, 2.4], zone="MVZ3",
           escape_route=True, accessible_route=True, clear_width="1400 mm"),
        el("COR-7-4", "corridor", "Deck 7 side corridor, zone 4", [197, 4, z, 30, 0.9, 2.4], zone="MVZ4",
           escape_route=True, accessible_route=True, clear_width="880 mm"),
        # doors
        el("D-7310", "door", "Accessible cabin 7310 door", [150, 0.6, z, 0.8, 0.1, 2.1], zone="MVZ3",
           accessible_route=True, clear_width="800 mm", threshold_height="25 mm"),
        el("D-7320", "door", "Accessible cabin 7320 door", [156, 0.6, z, 0.9, 0.1, 2.1], zone="MVZ3",
           accessible_route=True, clear_width="850 mm", threshold_height="10 mm"),
        el("D-7401", "door", "Zone 4 escape door", [200, -1, z, 1.0, 0.1, 2.1], zone="MVZ4",
           escape_route=True, clear_width="1000 mm"),
        # ramps
        el("R-7-1", "ramp", "Ramp to pool deck lift lobby", [170, 3, z, 3, 1.5, 0.3], zone="MVZ3",
           accessible_route=True, running_slope="1:10"),
        el("R-7-2", "ramp", "Ramp to theatre foyer", [220, -3, z, 4.2, 1.5, 0.3], zone="MVZ4",
           accessible_route=True, running_slope="1:14"),
        # low-location lighting
        el("LLL-7-3", "low_location_lighting", "Escape route strip, zone 3", [146, -0.3, z, 46, 0.05, 0.05],
           zone="MVZ3", height_above_deck="250 mm"),
        el("LLL-7-4", "low_location_lighting", "Escape route strip, zone 4", [197, 4.4, z + 0.45, 30, 0.05, 0.05],
           zone="MVZ4", height_above_deck="450 mm"),
        # navigation lights (height above the uppermost continuous deck at z = 60 m)
        el("NL-FWD", "nav_light", "Forward masthead light", [270, -0.2, 71, 0.4, 0.4, 0.4], deck="Mast",
           role="forward_masthead", height_above_hull="11 m"),
        el("NL-AFT", "nav_light", "After masthead light", [160, -0.2, 76, 0.4, 0.4, 0.4], deck="Mast",
           role="after_masthead", height_above_hull="16 m"),
        # main engines
        el("DE-1", "diesel_engine", "Main engine 1", [40, -10, 3, 12, 4, 5], deck="Engine room", nox_tier="III"),
        el("DE-2", "diesel_engine", "Main engine 2", [40, 6, 3, 12, 4, 5], deck="Engine room", nox_tier="II"),
    ]
    expected = [
        {"rule_id": "SOLAS-II-2-9.2.2.1-MVZ-MAX", "element_id": "FZ3", "status": "fail"},
        {"rule_id": "SOLAS-II-2-3.32-MVZ-GENERAL", "element_id": "FZ2", "status": "warn"},
        {"rule_id": "SOLAS-II-2-3.32-MVZ-GENERAL", "element_id": "FZ3", "status": "warn"},
        {"rule_id": "SOLAS-II-2-9.2.2.1.1-MVZ-A60", "element_id": "BH-195", "status": "fail"},
        {"rule_id": "FSS-13.2.1.1-STAIR-WIDTH", "element_id": "ST-3B", "status": "fail"},
        {"rule_id": "FSS-13.2.1.1-STAIR-WIDTH", "element_id": "ST-4A", "status": "unknown"},
        {"rule_id": "SOLAS-II-2-13-ESCAPE-DOOR-CORRIDOR", "element_id": "COR-7-4", "status": "fail"},
        {"rule_id": "PVAG-V403.5.1-ROUTE-WIDTH", "element_id": "COR-7-4", "status": "fail"},
        {"rule_id": "PVAG-V404.2.3-DOOR-WIDTH", "element_id": "D-7310", "status": "fail"},
        {"rule_id": "ADA-404.2.5-THRESHOLD", "element_id": "D-7310", "status": "fail"},
        {"rule_id": "ADA-405.2-RAMP-SLOPE", "element_id": "R-7-1", "status": "fail"},
        {"rule_id": "SOLAS-II-2-13.3.2.5.1-LLL-HEIGHT", "element_id": "LLL-7-4", "status": "fail"},
        {"rule_id": "COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT", "element_id": "NL-FWD", "status": "fail"},
        {"rule_id": "MARPOL-VI-13-TIER-III-NA", "element_id": "DE-2", "status": "fail"},
    ]
    return {
        "title": "Demo cruise ship: Deck 7, zones 2-4 (synthetic)",
        "ship": {"name": "MS Demo Aurora (synthetic)", "ship_type": "passenger", "passengers": 2800,
                 "persons_total": 4000, "length_m": 300, "breadth_m": 38, "gross_tonnage": 180000,
                 "keel_laid": "2026-03-01", "flag": "FI",
                 "operating_areas": ["us_ports", "north_american_eca", "us_caribbean_eca", "mediterranean"],
                 "notes": "Synthetic design for demonstration. Not a Meyer Turku design."},
        "elements": e, "expected": expected,
    }


def fixed(d: dict) -> dict:
    d = copy.deepcopy(d)
    d["title"] = "Demo cruise ship: Deck 7, after fixes (synthetic)"
    patch = {"FZ3": {"box": [144, -19, 24, 46, 38, 9]}, "FZ4": {"box": [190, -19, 24, 45, 38, 9]},
             "BH-195": {"props": {"fire_class": "A-60"}}, "ST-3B": {"props": {"clear_width": "950 mm"}},
             "ST-4A": {"props": {"clear_width": "1100 mm"}}, "COR-7-4": {"props": {"clear_width": "1000 mm"}},
             "D-7310": {"props": {"clear_width": "860 mm", "threshold_height": "12 mm"}},
             "R-7-1": {"props": {"running_slope": "1:12"}}, "LLL-7-4": {"props": {"height_above_deck": "200 mm"}},
             "NL-FWD": {"props": {"height_above_hull": "12 m"}}, "NL-AFT": {"props": {"height_above_hull": "17 m"}},
             "DE-2": {"props": {"nox_tier": "III"}}}
    for e in d["elements"]:
        p = patch.get(e["id"])
        if p:
            if "box" in p:
                e["box"] = p["box"]
            e["props"].update(p.get("props", {}))
    d["expected"] = [{"rule_id": "SOLAS-II-2-3.32-MVZ-GENERAL", "element_id": "FZ2", "status": "warn"},
                     {"rule_id": "SOLAS-II-2-3.32-MVZ-GENERAL", "element_id": "FZ3", "status": "warn"},
                     {"rule_id": "SOLAS-II-2-3.32-MVZ-GENERAL", "element_id": "FZ4", "status": "warn"}]
    return d


def research() -> dict:
    return {
        "title": "Demo research vessel (synthetic, not a passenger ship)",
        "ship": {"name": "RV Demo Tern (synthetic)", "ship_type": "research", "passengers": 0, "persons_total": 30,
                 "length_m": 60, "breadth_m": 15, "gross_tonnage": 1800, "keel_laid": "2026-05-01", "flag": "US",
                 "operating_areas": ["north_american_eca", "us_ports"],
                 "notes": "Synthetic. Shows that passenger-ship rules are not applied to a research vessel."},
        "elements": [
            el("RV-ST1", "stair", "Main stair", [20, -1, 5, 4, 1, 3], deck="Main deck", escape_route=True,
               clear_width="800 mm"),
            el("RV-NL-FWD", "nav_light", "Forward masthead light", [45, -0.2, 18, 0.3, 0.3, 0.3], deck="Mast",
               role="forward_masthead", height_above_hull="8 m"),
            el("RV-NL-AFT", "nav_light", "After masthead light", [20, -0.2, 23, 0.3, 0.3, 0.3], deck="Mast",
               role="after_masthead", height_above_hull="13.5 m"),
            el("RV-DE-1", "diesel_engine", "Main generator 1", [10, -4, 1, 5, 2, 3], deck="Engine room",
               nox_tier="III"),
        ],
        "expected": [{"rule_id": "COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT", "element_id": "RV-NL-FWD", "status": "fail"},
                     {"rule_id": "COLREG-ANNEX-I-3a-MASTHEAD-SPACING", "element_id": None, "status": "fail"}],
    }


def cascade(d: dict) -> dict:
    """Fixing one finding (forward masthead raised to 12 m) creates another downstream."""
    d = copy.deepcopy(d)
    d["title"] = "Demo cruise ship: forward masthead raised to 12 m (synthetic)"
    for e in d["elements"]:
        if e["id"] == "NL-FWD":
            e["props"]["height_above_hull"] = "12 m"
    d["expected"] = [x for x in d["expected"] if x["element_id"] != "NL-FWD"] + [
        {"rule_id": "COLREG-ANNEX-I-2a-ii-MASTHEAD-SEPARATION", "element_id": "NL-AFT", "status": "fail"}]
    return d


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    c = cruise()
    for name, d in {"demo_cruise_deck7.json": c, "demo_cruise_deck7_fixed.json": fixed(c),
                    "demo_cruise_masthead_cascade.json": cascade(c),
                    "demo_research_vessel.json": research()}.items():
        (OUT / name).write_text(json.dumps(d, indent=2), encoding="utf-8")
        print("wrote", OUT / name)
