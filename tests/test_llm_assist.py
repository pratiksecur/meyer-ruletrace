"""Tests for the local-LLM helpers. They use a fake model, so Ollama is not needed."""
import json

from ruletrace import llm_assist as la


class Fake:
    name = "fake:test"

    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user, *, json_mode=False):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply(user) if callable(self.reply) else self.reply


FINDING = {"rule_id": "FSS-13.2.1.1-STAIR-WIDTH", "title": "Stairway clear width", "regime": "SOLAS / FSS Code",
           "ref": "FSS Code ch. 13, 2.1.1", "quote": "Stairways shall not be less than 900 mm in clear width.",
           "element_name": "Stairway 3B", "deck": "Deck 7", "required": "at least 900 mm", "actual": "850 mm",
           "message": "850 mm is below 900 mm", "why_rule": "People must be able to escape.", "fix": "Widen it.",
           "status": "fail", "element_id": "ST-3B"}

TEXT = "2.1.1 Stairways shall not be less than 900 mm in clear width. Handrails shall be fitted on both sides."


def test_explain_accepts_grounded_answer():
    out = la.explain_findings(Fake("The stairway has 850 mm but FSS Code ch. 13, 2.1.1 needs 900 mm. Widen it."), [FINDING])
    assert out[0]["source"] == "fake:test" and "850" in out[0]["explanation"]


def test_explain_rejects_invented_numbers_and_falls_back():
    out = la.explain_findings(Fake("It is 50 mm too narrow, needs 950 mm."), [FINDING])
    assert out[0]["source"] == "rules" and "950" not in out[0]["explanation"] and "ungrounded" not in out[0]["explanation"]
    assert "numbers" in out[0]["note"]


def test_explain_falls_back_when_model_down_and_skips_passes():
    ok = {**FINDING, "status": "pass"}
    out = la.explain_findings(Fake(la.LLMError("down")), [FINDING, ok])
    assert len(out) == 1 and out[0]["source"] == "rules" and "900 mm" in out[0]["explanation"]


def draft_reply(quote="Stairways shall not be less than 900 mm in clear width.", req="at least 900 mm"):
    return json.dumps({"rules": [{"id": "fss stair width", "title": "Stair width", "ref": "FSS 13.2.1.1", "quote": quote,
                                  "applies_if": {"passenger_ship": True}, "target": {"type": "stair", "when": {"escape_route": True}},
                                  "check": {"property": "clear_width", "requirement": req}, "severity": "major",
                                  "why": "Escape.", "fix": "Widen."}], "unclear": ""})


def test_draft_ok_is_unverified_and_yaml():
    out = la.draft_rule(Fake(draft_reply()), TEXT, regime="SOLAS / FSS Code", source_url="https://x")
    d = out["drafts"][0]
    assert d["ok"] and d["rule"]["verified"] is False and d["rule"]["id"] == "FSS-STAIR-WIDTH"
    assert "verified: false" in out["yaml"]


def test_draft_rejects_non_verbatim_quote():
    out = la.draft_rule(Fake(draft_reply(quote="Stairs must be at least 900 mm wide.")), TEXT)
    assert not out["drafts"][0]["ok"] and any("verbatim" in p for p in out["drafts"][0]["problems"])
    assert out["yaml"] == ""


def test_draft_warns_on_number_not_in_quote_and_rejects_duplicates():
    out = la.draft_rule(Fake(draft_reply(req="at least 1200 mm")), TEXT)
    assert out["drafts"][0]["ok"] and out["drafts"][0]["warnings"]
    dup = la.draft_rule(Fake(draft_reply()), TEXT, existing_ids={"FSS-STAIR-WIDTH"})
    assert not dup["drafts"][0]["ok"]


def test_draft_rejects_unknown_check_keys():
    reply = json.loads(draft_reply())
    reply["rules"][0]["check"] = {"property": "clear_width", "python": "__import__('os')"}
    out = la.draft_rule(Fake(json.dumps(reply)), TEXT)
    assert not out["drafts"][0]["ok"]


RULES = [{"id": "COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT", "title": "Forward masthead light height above the hull",
          "regime": "COLREG", "ref": "COLREG Annex I, section 2(a)(i)", "quote": "height above the hull of not less than 6 metres",
          "why": "Seen over the hull.", "source_url": "https://x", "verified": True},
         {"id": "FSS-13.2.1.1-STAIR-WIDTH", "title": "Stairway clear width", "regime": "SOLAS / FSS Code",
          "ref": "FSS 13", "quote": "900 mm", "why": "Escape.", "source_url": "", "verified": True}]


def test_ask_retrieves_and_checks_citations():
    out = la.answer_question(Fake("Masthead light must be 6 metres up [COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT] and [MADE-UP]."),
                             "How high must the masthead light be?", RULES)
    assert [c["rule_id"] for c in out["citations"]] == ["COLREG-ANNEX-I-2a-i-MASTHEAD-HEIGHT"]
    assert out["unsupported_citations"] == ["MADE-UP"]


def test_ask_without_matching_rules_does_not_call_model():
    out = la.answer_question(Fake(la.LLMError("must not be called")), "Tell me about quantum chromodynamics", RULES)
    assert out["answer"] == "The loaded rules do not cover this."


def test_ask_flags_uncited_answer_and_invented_number():
    out = la.answer_question(Fake("It must be 99 metres."), "masthead light height", RULES)
    assert len(out["warnings"]) == 2
