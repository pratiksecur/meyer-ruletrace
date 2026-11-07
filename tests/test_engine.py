from datetime import date
from pathlib import Path

import pytest

from ruletrace.engine import applicability, check_design, load_rules
from ruletrace.model import DesignError, design_from_dict, load_design

SAMPLES = Path(__file__).resolve().parent.parent / "ruletrace" / "samples"


def _issues(result):
    return {(f["rule_id"], f["element_id"], f["status"]) for f in result["findings"] if f["status"] != "pass"}


@pytest.mark.parametrize("name", [p.name for p in SAMPLES.glob("*.json") if not p.name.endswith(".expected.json")])
def test_every_labelled_sample_is_exact(name):
    d = load_design(SAMPLES / name)
    assert _issues(check_design(d)) == {(x["rule_id"], x.get("element_id"), x["status"]) for x in d.expected}


def test_rules_all_have_sources():
    for r in load_rules():
        if r.regime != "Drawing consistency":   # internal checks of a drawing against itself
            assert r.ref and r.source_url.startswith("https://"), r.id
        assert r.fix and r.why, r.id


def test_applicability_explains_itself():
    d = load_design(SAMPLES / "demo_research_vessel.json")
    r = next(x for x in load_rules() if x.id == "FSS-13.2.1.1-STAIR-WIDTH")
    ok, why = applicability(r, d.ship)
    assert not ok and any("not a passenger ship" in w for w in why)


def test_geographic_what_if_switches_rules():
    d = load_design(SAMPLES / "demo_cruise_deck7.json")
    base = _issues(check_design(d))
    d.ship.operating_areas = ["baltic_sea"]
    baltic = _issues(check_design(d))
    assert ("PVAG-V404.2.3-DOOR-WIDTH", "D-7310", "fail") in base - baltic
    assert ("MARPOL-VI-13-TIER-III-BALTIC-NORTH-SEA", "DE-2", "fail") in baltic


def test_only_new_ships():
    d = load_design(SAMPLES / "demo_cruise_deck7.json")
    d.ship.keel_laid = date(1999, 1, 1)
    ids = {f["rule_id"] for f in check_design(d)["findings"]}
    assert not any(i.startswith(("SOLAS", "FSS", "MARPOL")) for i in ids)


def test_missing_value_is_unknown_not_guessed():
    d = load_design(SAMPLES / "demo_cruise_deck7.json")
    f = next(f for f in check_design(d)["findings"] if f["element_id"] == "ST-4A")
    assert f["status"] == "unknown" and "Can't check" in f["message"]


def test_bad_designs_rejected():
    with pytest.raises(DesignError):
        design_from_dict({"ship": {"name": "x", "ship_type": "yacht"}, "elements": []})
    with pytest.raises(DesignError):
        design_from_dict({"ship": {"name": "x"}, "elements": [{"id": "a", "type": "door"}, {"id": "a", "type": "door"}]})


def test_ifc_import_reads_geometry_and_psets():
    pytest.importorskip("ifcopenshell")
    d = load_design(SAMPLES / "demo_deck_section.ifc")
    issues = _issues(check_design(d))
    assert ("SOLAS-II-2-9.2.2.1-MVZ-MAX", "FZ3", "fail") in issues          # 51 m measured from 3D shape
    assert ("SOLAS-II-2-9.2.2.1.1-MVZ-A60", "BH-195", "fail") in issues     # Pset_WallCommon.FireRating
    assert ("PVAG-V404.2.3-DOOR-WIDTH", "D-7330", "unknown") in issues      # overall width is not clear width


def test_partial_models_never_get_presence_findings():
    d = load_design(SAMPLES / "demo_cruise_deck7.json")
    assert not any(f["rule_id"] in {"MLC-A3.1-12-HOSPITAL", "GA-CONSISTENCY-TOTAL-BERTHS"} for f in check_design(d)["findings"])


def test_load_line_and_subdivision_rules_on_cargo_sample():
    from pathlib import Path
    from ruletrace.engine import check_design
    from ruletrace.model import load_design
    d = load_design(Path(__file__).resolve().parent.parent / "ruletrace" / "samples" / "demo_cargo_ship.json")
    r = check_design(d)
    fails = {(f["rule_id"], f["element_id"]) for f in r["findings"] if f["status"] == "fail"}
    assert fails == {(x["rule_id"], x["element_id"]) for x in d.expected}
    db = next(f for f in r["findings"] if f["rule_id"] == "SOLAS-II-1-9.2-DOUBLE-BOTTOM-HEIGHT")
    assert db["measured"] == "measured from the 3D geometry" and "1 m" in db["required"]
