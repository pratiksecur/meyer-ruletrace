from tests.conftest import CEO, ENG

AINO = {"X-User": "aino", "X-Roles": "ciso"}


def _design(client):
    did = client.post("/ruletrace/designs/sample/demo_research_vessel", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={}).json()
    return did, [f for f in r["findings"] if f["status"] in {"fail", "warn", "unknown"}]


def test_every_open_finding_needs_a_human_decision_before_sign_off(client):
    did, open_ = _design(client)
    assert len(open_) == 2
    assert client.post(f"/ruletrace/designs/{did}/signoff", headers=ENG, json={"stage": "prepared"}).status_code == 409
    f1, f2 = open_
    client.post(f"/ruletrace/designs/{did}/findings/review", headers=ENG,
                json={"rule_id": f1["rule_id"], "element_id": f1["element_id"], "decision": "confirm"})
    # a dismissal needs a reason
    bad = client.post(f"/ruletrace/designs/{did}/findings/review", headers=ENG,
                      json={"rule_id": f2["rule_id"], "element_id": f2["element_id"], "decision": "dismiss"})
    assert bad.status_code == 400
    client.post(f"/ruletrace/designs/{did}/findings/review", headers=ENG, json={
        "rule_id": f2["rule_id"], "element_id": f2["element_id"], "decision": "dismiss",
        "reason_code": "accepted_deviation", "reason": "Agreed with class in meeting 12, see minutes."})
    assert client.post(f"/ruletrace/designs/{did}/signoff", headers=ENG, json={"stage": "prepared"}).json()["signoff"]["stage"] == "prepared"
    # four-eyes: the engineer who prepared it can't approve, and an engineer role can't approve at all
    assert client.post(f"/ruletrace/designs/{did}/signoff", headers=ENG, json={"stage": "approved"}).status_code == 403
    ok = client.post(f"/ruletrace/designs/{did}/signoff", headers=AINO, json={"stage": "approved", "note": "ok"}).json()
    assert ok["signoff"]["approved_by"] == "aino"
    # signed-off checks are locked until reopened
    assert client.post(f"/ruletrace/designs/{did}/findings/review", headers=ENG,
                       json={"rule_id": f1["rule_id"], "element_id": f1["element_id"], "decision": "clear"}).status_code == 409
    s = client.get("/ruletrace/summary", headers=CEO).json()
    assert s["totals"]["approved"] == 1 and s["designs"][0]["review"]["stage"] == "approved"
    assert client.get("/audit/verify", headers=ENG).json()["ok"]


def test_same_person_cannot_prepare_and_approve(client):
    ella = {"X-User": "ella", "X-Roles": "admin"}       # an admin may both review and approve, but not the same check
    did, open_ = _design(client)
    for f in open_:
        client.post(f"/ruletrace/designs/{did}/findings/review", headers=ella,
                    json={"rule_id": f["rule_id"], "element_id": f["element_id"], "decision": "confirm"})
    client.post(f"/ruletrace/designs/{did}/signoff", headers=ella, json={"stage": "prepared"})
    r = client.post(f"/ruletrace/designs/{did}/signoff", headers=ella, json={"stage": "approved"})
    assert r.status_code == 403 and "four-eyes" in r.json()["detail"]
