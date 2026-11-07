"""Organisation posture for the CISO / compliance lead, and who may see it."""
from types import SimpleNamespace as NS

from ruletrace.posture import build_posture, rating
from tests.conftest import CEO, ENG

CISO = {"X-User": "aino", "X-Roles": "ciso"}
OUTSIDER = {"X-User": "olli", "X-Roles": "visitor"}


def rule(i, regime, verified=True):
    return NS(id=i, title=f"Rule {i}", regime=regime, ref=f"ref {i}", severity="major", verified=verified)


def fail(rule_id, element_id, severity="major", regime="SOLAS"):
    return {"rule_id": rule_id, "element_id": element_id, "regime": regime, "title": f"Rule {rule_id}",
            "ref": "ref", "severity": severity}


def design(i, counts, fails=()):
    return {"design_id": f"d{i}", "ship": f"Ship {i}", "title": f"Design {i}", "counts": counts, "fails": list(fails),
            "readiness": "fix" if counts.get("fail") else "ready", "readiness_label": "x",
            "checked_by": "mikko", "checked_at": "2026-11-07T10:00:00Z"}


def summary(designs, major, minor, regimes=()):
    return {"designs": designs, "regimes": list(regimes), "totals": {"checks_run": len(designs),
            "fail_by_severity": {"major": major, "minor": minor}}, "chain": {"ok": True, "entries": 5}}


def test_rating_rules():
    assert rating(0, 0, 0, 0, 0)["level"] == "none"
    assert rating(2, 1, 0, 0, 0)["level"] == "at_risk"
    assert rating(2, 0, 3, 0, 0)["level"] == "watch"
    assert rating(2, 0, 0, 0, 4)["level"] == "watch" and "could not be decided" in rating(2, 0, 0, 0, 4)["reason"]
    assert rating(2, 0, 0, 0, 0)["level"] == "clear"
    assert "AT RISK when any major issue is open" in rating(1, 1, 0, 0, 0)["rule"]


def test_posture_across_several_ships():
    d1 = design(1, {"pass": 8, "fail": 2, "warn": 0, "unknown": 1},
                [fail("R1", "e1"), fail("R2", "e2", "minor", "MARPOL")])
    d2 = design(2, {"pass": 10, "fail": 1, "warn": 1, "unknown": 0}, [fail("R1", "e9")])
    d3 = design(3, {"pass": 12, "fail": 0, "warn": 0, "unknown": 0})
    rules = [rule("R1", "SOLAS"), rule("R2", "MARPOL", verified=False), rule("R3", "MARPOL")]
    p = build_posture(summary([d1, d2, d3], 2, 1, [{"regime": "SOLAS", "fail": 2}, {"regime": "MARPOL", "fail": 1}]), rules, [])
    k = p["kpis"]
    assert k["designs"] == 3 and k["major_open"] == 2 and k["minor_open"] == 1 and k["unknown_open"] == 1
    assert k["pass_rate"] == round(100 * 30 / (30 + 3 + 1)) and k["coverage"] == round(100 * 34 / 35)
    assert p["rating"]["level"] == "at_risk"
    assert [r["ship"] for r in p["ships"]] == ["Ship 1", "Ship 2", "Ship 3"]
    assert [f["ship"] for f in p["focus"]] == ["Ship 1", "Ship 2"]                      # clear ship is not in focus
    r1 = p["recurring"][0]
    assert r1["rule_id"] == "R1" and r1["designs"] == 2 and r1["systemic"] is True      # fails on two ships
    marpol = next(g for g in p["regimes"] if g["regime"] == "MARPOL")
    assert marpol["rules"] == 2 and marpol["verified"] == 1 and marpol["open_fail"] == 1 and marpol["designs_affected"] == 1
    assert p["rule_pack"] == {"rules": 3, "verified": 2, "unverified": 1,
                              "unverified_rules": [{"id": "R2", "regime": "MARPOL", "ref": "ref R2"}]}
    texts = [s["text"] for s in p["signals"]]
    assert any("2 major issues open on 2 designs" in t for t in texts) and any("1 of 3 rules" in t for t in texts)
    assert p["signals"][-1]["level"] == "good"


def test_posture_with_no_checks_and_with_broken_history():
    p = build_posture(summary([], 0, 0), [rule("R1", "SOLAS")], [])
    assert p["rating"]["level"] == "none" and p["kpis"]["pass_rate"] is None and p["ships"] == [] and p["focus"] == []
    broken = summary([design(1, {"pass": 1, "fail": 0, "warn": 0, "unknown": 0})], 0, 0)
    broken["chain"] = {"ok": False, "entries": 3, "first_broken_seq": 2}
    sig = build_posture(broken, [], [])["signals"]
    assert sig[-1]["level"] == "bad" and "has been changed" in sig[-1]["text"]


def test_trend_and_ai_use_come_from_the_audit_entries():
    e = lambda seq, action, **payload: {"seq": seq, "ts": f"t{seq}", "actor": "mikko", "action": action, "payload": payload}
    entries = [e(1, "design.checked", file="a.json", counts={"fail": 4, "pass": 6}),
               e(2, "design.explained", explained=5, fell_back_to_rules=1),
               e(3, "rule.drafted", usable=2), e(4, "assistant.asked"),
               e(5, "design.checked", file="a.json", counts={"fail": 1, "pass": 9})]
    p = build_posture(summary([], 0, 0), [], entries)
    assert [t["fail"] for t in p["trend"]] == [4, 1]
    ai = p["ai"]
    assert ai["total"] == 3 and ai["explanations"] == 5 and ai["explanations_fell_back"] == 1 and ai["fallback_pct"] == 20
    assert ai["rule_drafts_usable"] == 2 and "never decides compliance" in ai["policy"]


# ---- who may see it (needs the whole app) ----

def test_ciso_sees_the_whole_organisation_but_cannot_change_anything(client):
    did = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={})
    did2 = client.post("/ruletrace/designs/sample/demo_research_vessel", headers=ENG).json()["id"]
    client.post(f"/ruletrace/designs/{did2}/check", headers=ENG, json={})

    p = client.get("/ruletrace/posture", headers=CISO).json()
    k = p["kpis"]
    assert k["designs"] == 2 and len(p["ships"]) == 2 and p["chain"]["ok"]
    assert p["rating"]["level"] == ("at_risk" if k["major_open"] else "watch")
    assert k["major_open"] + k["minor_open"] >= 11                  # the cruise deck alone has 11 open failures
    assert p["rule_pack"]["rules"] >= 30 and p["regimes"]

    s = client.get("/ruletrace/summary", headers=CISO).json()
    assert s["view"] == "compliance" and all("fails" not in d for d in s["designs"])   # no element-level findings
    assert client.get("/ruletrace/audit", headers=CISO).status_code == 200            # oversight may read the record

    # read-only: the compliance lead may look at a design's findings (to approve a sign-off), but can't
    # upload, load, change, delete or review designs
    assert client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=CISO).status_code == 403
    assert client.post(f"/ruletrace/designs/{did}/check", headers=CISO, json={}).status_code == 200
    assert client.get("/ruletrace/designs", headers=CISO).status_code == 200
    assert client.post(f"/ruletrace/designs/{did}/revise", headers=CISO,
                       json={"changes": [{"element_id": "ST-3B", "prop": "clear_width", "value": "950 mm"}]}).status_code == 403
    assert client.post(f"/ruletrace/designs/{did}/findings/review", headers=CISO,
                       json={"rule_id": "x", "element_id": None, "decision": "confirm"}).status_code == 403
    assert client.delete(f"/ruletrace/designs/{did}", headers=CISO).status_code == 403


def test_posture_is_closed_to_executives_and_outsiders_but_open_to_engineers(client):
    assert client.get("/ruletrace/posture", headers=ENG).status_code == 200
    assert client.get("/ruletrace/posture", headers=CEO).status_code == 403
    assert client.get("/ruletrace/posture", headers=OUTSIDER).status_code == 403
    assert client.get("/ruletrace/summary", headers=CEO).json()["view"] == "executive"
    assert client.get("/ruletrace/summary", headers=ENG).json()["view"] == "engineer"
    assert client.get("/ruletrace/audit", headers=CEO).status_code == 403
