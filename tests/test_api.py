import json
from pathlib import Path

from tests.conftest import CEO, ENG

SAMPLES = Path(__file__).resolve().parent.parent / "ruletrace" / "samples"


def test_viewer_and_rules(client):
    assert "RuleTrace" in client.get("/ruletrace").text
    assert client.get("/ruletrace/static/three.module.min.js").status_code == 200
    assert client.get("/ruletrace/rules", headers=ENG).json()["count"] >= 15


def test_sample_check_and_audit(client):
    did = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={}).json()
    assert r["counts"]["fail"] == 11 and r["audit_seq"] >= 1
    w = client.post(f"/ruletrace/designs/{did}/check", headers=ENG,
                    json={"ship_overrides": {"operating_areas": ["baltic_sea"]}}).json()
    assert w["what_if"] == {"operating_areas": ["baltic_sea"]}
    assert client.get("/audit/verify", headers=ENG).json()["ok"]


def test_upload_json_and_ifc(client):
    body = (SAMPLES / "demo_research_vessel.json").read_bytes()
    d = client.post("/ruletrace/designs", headers=ENG, files={"file": ("rv.json", body)}).json()
    assert d["elements"] == 4
    import pytest
    pytest.importorskip("ifcopenshell")
    ifc = (SAMPLES / "demo_deck_section.ifc").read_bytes()
    d2 = client.post("/ruletrace/designs", headers=ENG, files={"file": ("deck.ifc", ifc)}).json()
    assert d2["format"] == "ifc"
    assert client.post(f"/ruletrace/designs/{d2['id']}/check", headers=ENG, json={}).json()["counts"]["fail"] >= 4


def test_rejections(client):
    assert client.post("/ruletrace/designs", headers=ENG, files={"file": ("x.txt", b"hi")}).status_code == 400
    assert client.post("/ruletrace/designs", headers=ENG, files={"file": ("x.json", b"{")}).status_code == 400
    assert client.get("/ruletrace/designs/../../etc", headers=ENG).status_code in {400, 404}
    assert client.get("/ruletrace/designs", headers=CEO).status_code == 403
    did = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    assert client.post(f"/ruletrace/designs/{did}/check", headers=ENG,
                       json={"ship_overrides": {"elements": []}}).status_code == 400


def test_check_result_carries_part_details(client):
    import pytest
    did = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={}).json()
    el = next(e for e in r["elements"] if e["type"] == "stair")
    assert el["props"]["clear_width"] and el["box"] and "source" in el
    assert all("property" in f for f in r["findings"])          # the viewer draws the checked dimension
    pytest.importorskip("ifcopenshell")
    did = client.post("/ruletrace/designs/sample/demo_deck_section", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={}).json()
    assert any(e.get("mesh") and e["mesh"]["f"] for e in r["elements"])  # the IFC model's own triangles
    assert client.get("/ruletrace/static/parts.js").status_code == 200


def test_compare_revisions(client):
    a = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    b = client.post("/ruletrace/designs/sample/demo_cruise_masthead_cascade", headers=ENG).json()["id"]
    c = client.post(f"/ruletrace/designs/{a}/compare/{b}", headers=ENG).json()
    assert [x["rule_id"] for x in c["fixed"]] == ["COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT"]
    assert [x["rule_id"] for x in c["broken"]] == ["COLREG-ANNEX-I-2a-ii-MASTHEAD-SEPARATION"]   # the knock-on effect
    assert c["parts_changed"][0]["changes"][0]["what"] == "height_above_hull"
    assert client.post(f"/ruletrace/designs/{a}/compare/{a}", headers=ENG).status_code == 400
    assert client.post(f"/ruletrace/designs/{a}/compare/{b}", headers=CEO).status_code == 403


def test_gross_tonnage_from_volume(client):
    did = client.post("/ruletrace/designs/sample/demo_cargo_ship", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={}).json()
    gt = next(f for f in r["findings"] if f["rule_id"] == "ITC69-REG3-GROSS-TONNAGE-LOW")
    assert gt["status"] == "fail" and gt["required"] == "at least 9783"
    ok = client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={"ship_overrides": {"gross_tonnage": 9881}}).json()
    assert all(f["status"] == "pass" for f in ok["findings"] if f["rule_id"].startswith("ITC69"))


def test_change_measurements_and_recheck(client):
    did = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/revise", headers=ENG, json={"changes": [
        {"element_id": "ST-3B", "prop": "clear_width", "value": "950 mm"},
        {"element_id": "COR-7-4", "prop": "clear_width", "value": "860 mm"}]}).json()
    assert r["revision_of"] == did and len(r["changes"]) == 2
    c = client.post(f"/ruletrace/designs/{did}/compare/{r['id']}", headers=ENG).json()
    assert ("FSS-13.2.1.1-STAIR-WIDTH", "ST-3B") in {(x["rule_id"], x["element_id"]) for x in c["fixed"]}
    assert any(p["id"] == "COR-7-4" for p in c["parts_changed"])
    assert client.post(f"/ruletrace/designs/{did}/revise", headers=ENG, json={"changes": [
        {"element_id": "NOPE", "prop": "x", "value": 1}]}).status_code == 400
    assert client.post(f"/ruletrace/designs/{did}/revise", headers=ENG, json={"changes": [
        {"element_id": "ST-3B", "prop": "height", "value": "-3"}]}).status_code == 400
    assert client.get("/audit/verify", headers=ENG).json()["ok"]


def test_change_ship_particulars(client):
    did = client.post("/ruletrace/designs/sample/demo_cargo_ship", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/revise", headers=ENG, json={"ship": {"gross_tonnage": 9881}}).json()
    c = client.post(f"/ruletrace/designs/{did}/compare/{r['id']}", headers=ENG).json()
    assert "ITC69-REG3-GROSS-TONNAGE-LOW" in [x["rule_id"] for x in c["fixed"]]
    assert client.post(f"/ruletrace/designs/{did}/revise", headers=ENG, json={"ship": {"name": "x"}}).status_code == 400
