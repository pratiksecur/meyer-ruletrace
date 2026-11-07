"""API tests for the local-LLM endpoints, with a fake model (Ollama not needed)."""
import json

from ruletrace import llm_api
from tests.conftest import CEO, ENG


class Fake:
    name = "fake:test"

    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user, *, json_mode=False):
        return self.reply(user) if callable(self.reply) else self.reply

    def status(self):
        return {"model": "fake", "reachable": True, "model_pulled": True, "message": "ok"}


def _design(client):
    return client.post("/ruletrace/designs/sample/demo_cruise_deck7", headers=ENG).json()["id"]


def test_explain_uses_model_or_falls_back_and_is_audited(client, monkeypatch):
    did = _design(client)
    def faithful(user):  # build a faithful four-part answer from the finding the endpoint sent
        f = json.loads(user.removeprefix("<finding>").removesuffix("</finding>"))
        return json.dumps({"requirement": f"{f['regime']} {f['ref']} requires {f['required']}.",
                           "design_state": f"{f['element_name']} has {f['actual']}.",
                           "why_it_matters": f["why_rule"] or "", "fix": f["fix"] or ""})
    monkeypatch.setattr(llm_api, "get_client", lambda: Fake(faithful))
    r = client.post(f"/ruletrace/designs/{did}/explain", headers=ENG, json={"limit": 3}).json()
    assert r["counts"]["fail"] >= 1 and 1 <= len(r["explanations"]) <= 3
    assert all(e["source"] == "fake:test" and e["parts"]["fix"] is not None for e in r["explanations"])
    monkeypatch.setattr(llm_api, "get_client", lambda: Fake('{"requirement": "123456 mm", "design_state": "x"}'))
    r2 = client.post(f"/ruletrace/designs/{did}/explain", headers=ENG, json={"limit": 2}).json()
    assert all(e["source"] == "rules" for e in r2["explanations"])
    assert client.get("/audit/verify", headers=ENG).json()["ok"]


def test_draft_endpoint_never_saves_and_marks_unverified(client, monkeypatch):
    text = "2.1.1 Stairways shall not be less than 900 mm in clear width."
    reply = json.dumps({"rules": [{"id": "x-stair", "title": "Stair width", "ref": "2.1.1",
                                   "quote": "Stairways shall not be less than 900 mm in clear width.",
                                   "target": {"type": "stair"}, "check": {"property": "clear_width", "requirement": "at least 900 mm"}}]})
    monkeypatch.setattr(llm_api, "get_client", lambda: Fake(reply))
    r = client.post("/ruletrace/rules/draft", headers=ENG, json={"text": text, "regime": "SOLAS / FSS Code"}).json()
    assert r["drafts"][0]["ok"] and "verified: false" in r["yaml"]
    assert client.get("/ruletrace/rules", headers=ENG).json()["count"] >= 15  # unchanged rule pack


def test_ask_cites_rules_and_respects_roles(client, monkeypatch):
    monkeypatch.setattr(llm_api, "get_client",
                        lambda: Fake("It is in [COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT]."))
    r = client.post("/ruletrace/ask", headers=ENG, json={"question": "masthead light height above the hull"}).json()
    assert r["citations"] and r["citations"][0]["rule_id"] == "COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT"
    assert client.post("/ruletrace/ask", headers=CEO, json={"question": "masthead light height"}).status_code == 403
    assert client.get("/ruletrace/llm/status", headers=ENG).json()["reachable"] is True


def test_explain_one_finding_by_rule(client, monkeypatch):
    did = _design(client)
    monkeypatch.setattr(llm_api, "get_client", lambda: Fake("not json"))  # forces the deterministic fallback
    r = client.post(f"/ruletrace/designs/{did}/explain", headers=ENG,
                    json={"limit": 1, "rule_id": "ADA-405.2-RAMP-SLOPE", "element_id": "R-7-1"}).json()
    assert [(e["rule_id"], e["element_id"]) for e in r["explanations"]] == [("ADA-405.2-RAMP-SLOPE", "R-7-1")]
    assert r["explanations"][0]["source"] == "rules" and "1:12" in r["explanations"][0]["explanation"]
