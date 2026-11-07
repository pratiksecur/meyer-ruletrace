"""AI actions (verified fixes, what-if) with a fake model: Ollama is never needed."""
import json
from pathlib import Path

import pytest

from ruletrace import llm_api
from ruletrace.llm_actions import WhatIfError, suggest_fixes, validate_whatif
from ruletrace.model import load_design
from tests.conftest import CEO, ENG

SAMPLES = Path(__file__).resolve().parent.parent / "ruletrace" / "samples"


class Fake:
    name = "fake:test"

    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user, *, json_mode=False):
        return self.reply(user) if callable(self.reply) else self.reply

    def status(self):
        return {"model": "fake", "reachable": True, "model_pulled": True, "message": "ok"}


class Down(Fake):
    def complete(self, system, user, *, json_mode=False):
        from ruletrace.llm_assist import LLMError
        raise LLMError("connection refused")


def rec(name="demo_cruise_deck7"):
    p = SAMPLES / f"{name}.json"
    return {"filename": p.name, "sha256": "test", "design": load_design(p).to_dict()}


def facts(user):
    return json.loads(user.removeprefix("<finding>").removesuffix("</finding>"))


# -- fixes -------------------------------------------------------------------------------------------

def test_good_model_proposal_is_verified_and_kept():
    def reply(user):
        f = facts(user)
        if f["rule_id"] == "FSS-13.2.1.1-STAIR-WIDTH":
            return json.dumps({"proposals": [{"element_id": f["element_id"], "property": f["property_tested"],
                                              "new_value": "950 mm", "reason": "Widen the stair."}]})
        return '{"proposals": []}'
    out = suggest_fixes(Fake(reply), rec(), limit=20)
    p = next(x for x in out["proposals"] if x["rule_id"] == "FSS-13.2.1.1-STAIR-WIDTH")
    assert p["source"] == "fake:test" and p["new_value"] == "950 mm"
    assert p["counts_after"]["fail"] == p["counts_before"]["fail"] - 1


@pytest.mark.parametrize("bad, why", [
    ({"element_id": "NOPE", "new_value": "950 mm"}, "not in the design"),
    ({"property": "threshold_height", "new_value": "950 mm"}, "the rule tests 'clear_width'"),
    ({"new_value": "wide enough"}, "no number"),
    ({"new_value": "860 mm"}, "still fail"),
])
def test_bad_model_proposals_are_rejected(bad, why):
    def reply(user):
        f = facts(user)
        p = {"element_id": f["element_id"], "property": f["property_tested"], "reason": "x", **bad}
        return json.dumps({"proposals": [p]})
    out = suggest_fixes(Fake(reply), rec(), limit=20)
    rej = [d for d in out["discarded"] if d["rule_id"] == "FSS-13.2.1.1-STAIR-WIDTH" and d["source"] == "fake:test"]
    assert rej and why in rej[0]["why"]
    # the rule-based fallback still finds the verified fix
    assert next(x for x in out["proposals"] if x["rule_id"] == "FSS-13.2.1.1-STAIR-WIDTH")["source"] == "rules"


def test_fix_that_breaks_another_rule_is_discarded():
    out = suggest_fixes(None, rec(), limit=20)  # no model: fallback only
    mast = [d for d in out["discarded"] if d["rule_id"] == "COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT"]
    assert mast and "breaks COLREG-ANNEX-I-2a-ii-MASTHEAD-SEPARATION" in mast[0]["why"]
    assert not any(p["rule_id"] == "COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT" for p in out["proposals"])


def test_model_down_falls_back_and_computed_checks_are_skipped():
    out = suggest_fixes(Down(None), rec("demo_cargo_ship"), limit=20)
    assert {p["source"] for p in out["proposals"]} == {"rules"} and len(out["proposals"]) == 5
    assert any("model failed" in d["why"] for d in out["discarded"])
    assert any(d["rule_id"] == "SOLAS-II-1-9.2-DOUBLE-BOTTOM-HEIGHT" and "computed" in d["why"] for d in out["discarded"])


def test_fixes_endpoint_audited_and_engineer_only(client, monkeypatch):
    monkeypatch.setattr(llm_api, "get_client", lambda: Down(None))
    did = client.post("/ruletrace/designs/sample/demo_cargo_ship", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/fixes", headers=ENG, json={"limit": 3}).json()
    assert r["failing_considered"] == 3 and all("re-checked" in p["verification"] for p in r["proposals"])
    assert client.post(f"/ruletrace/designs/{did}/fixes", headers=CEO, json={}).status_code == 403
    from sqlalchemy import select
    from vartija_core.audit import AuditEntry
    with client.app.state.session_factory() as s:
        entry = s.scalars(select(AuditEntry).where(AuditEntry.action == "design.fixes_suggested")).first()
    assert entry is not None and entry.payload["verified"] == len(r["proposals"])
    assert client.get("/audit/verify", headers=ENG).json()["ok"]


# -- what-if ---------------------------------------------------------------------------------------------

AREAS = {"us_ports", "north_american_eca", "baltic_sea", "north_sea", "mediterranean"}
SHIP = {"operating_areas": ["us_ports"], "keel_laid": "2026-03-01"}


def test_whatif_valid_add_area_and_keel():
    raw = {"ship_overrides": {"keel_laid": "2027-01-01"}, "areas": {"add": ["baltic_sea"], "remove": [], "replace": None},
           "assumptions": ["only the year was given"], "unclear": ""}
    ov, ass = validate_whatif(raw, "keel laid in 2027 and we also operate in the Baltic", SHIP, AREAS)
    assert ov == {"keel_laid": "2027-01-01", "operating_areas": ["us_ports", "baltic_sea"]} and ass


@pytest.mark.parametrize("raw, text, why", [
    ({"areas": {"add": ["atlantis"]}}, "we operate near Atlantis", "not a known operating area"),
    ({"areas": {"add": ["north_sea"]}}, "we operate in the Baltic", "doesn't mention north sea"),
    ({"ship_overrides": {"keel_laid": "2031-01-01"}}, "keel laid in 2027", "not in the question"),
    ({"ship_overrides": {"keel_laid": "next spring"}}, "keel laid next spring", "ISO date"),
    ({"ship_overrides": {"passengers": 90000}}, "what with 90000 passengers", "outside"),
    ({"ship_overrides": {"draught": 8}}, "draught 8 m", "can't be changed"),
    ({"unclear": "which sea is meant?"}, "what if we go north", "unclear"),
    ({}, "is the ship nice?", "doesn't change anything"),
])
def test_whatif_bad_or_ungrounded_output_is_refused(raw, text, why):
    with pytest.raises(WhatIfError, match=why):
        validate_whatif(raw, text, SHIP, AREAS)


def test_whatif_endpoint_returns_diff(client, monkeypatch):
    reply = json.dumps({"ship_overrides": {}, "areas": {"add": [], "remove": ["us_ports"], "replace": None},
                        "assumptions": [], "unclear": ""})
    monkeypatch.setattr(llm_api, "get_client", lambda: Fake(reply))
    did = client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]
    r = client.post(f"/ruletrace/designs/{did}/whatif", headers=ENG,
                    json={"text": "what if we no longer call at US ports?"}).json()
    assert "us_ports" not in r["ship_overrides"]["operating_areas"]
    assert "PVAG-V404.2.3-DOOR-WIDTH" in r["rules_switched_off"] and r["counts_after"]["fail"] < r["counts_before"]["fail"]
    monkeypatch.setattr(llm_api, "get_client", lambda: Fake('{"unclear": "which area?"}'))
    bad = client.post(f"/ruletrace/designs/{did}/whatif", headers=ENG, json={"text": "what if we sail somewhere else"})
    assert bad.status_code == 422 and "unclear" in bad.json()["detail"]
    # the ordinary check endpoint is unchanged
    assert client.post(f"/ruletrace/designs/{did}/check", headers=ENG, json={"ship_overrides": {"draught": 1}}).status_code == 400
