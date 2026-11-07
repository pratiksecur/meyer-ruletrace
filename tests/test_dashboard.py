from tests.conftest import CEO, ENG

OUTSIDER = {"X-User": "olli", "X-Roles": "visitor"}


def test_dashboard_reflects_design_checks(client):
    assert "RuleTrace" in client.get("/ruletrace/dashboard").text
    assert client.get("/ruletrace/summary", headers=ENG).json()["designs"] == []
    did = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={})
    # the same file loaded again is counted once
    did2 = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    client.post(f"/ruletrace/designs/{did2}/check", headers=ENG, json={})

    s = client.get("/ruletrace/summary", headers=ENG).json()
    assert s["view"] == "engineer" and s["totals"]["designs"] == 1 and s["totals"]["checks_run"] == 2
    d = s["designs"][0]
    assert d["counts"]["fail"] == 11 and d["readiness"] == "fix" and len(d["fails"]) == 11
    assert sum(g["fail"] for g in s["regimes"]) == 11 and s["chain"]["ok"]

    e = client.get("/ruletrace/summary", headers=CEO).json()
    assert e["view"] == "executive" and "fails" not in e["designs"][0]
    assert e["totals"]["open_fail"] == 11 and e["value"]["currency"] == "EUR"
    assert "entry_hash" not in e["activity"][0]


def test_audit_record_and_access(client):
    did = client.post("/ruletrace/designs/sample/demo_research_vessel", headers=ENG).json()["id"]
    client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={})
    a = client.get("/ruletrace/audit", headers=ENG).json()
    assert a["chain"]["ok"] and a["entries"][0]["action"] == "design.checked"
    assert a["entries"][0]["payload"]["design_id"] == did
    assert client.get("/ruletrace/audit", headers=CEO).status_code == 403
    assert client.get("/ruletrace/summary", headers=OUTSIDER).status_code == 403
