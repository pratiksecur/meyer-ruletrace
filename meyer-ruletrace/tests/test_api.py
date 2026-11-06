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
