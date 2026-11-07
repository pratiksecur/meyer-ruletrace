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


def good_parts(**kw):
    p = {"requirement": "FSS Code ch. 13, 2.1.1 requires a clear width of at least 900 mm.",
         "design_state": "Stairway 3B on Deck 7 has 850 mm.", "why_it_matters": "People must be able to escape.",
         "fix": "Widen it."}
    return json.dumps({**p, **kw})


def test_explain_accepts_faithful_four_part_answer():
    out = la.explain_findings(Fake(good_parts()), [FINDING])
    assert out[0]["source"] == "fake:test" and out[0]["parts"]["design_state"].endswith("850 mm.")


def test_citation_is_added_by_code_when_model_omits_it():
    out = la.explain_findings(Fake(good_parts(requirement="The rule requires at least 900 mm.")), [FINDING])
    assert out[0]["source"] == "fake:test"
    assert out[0]["parts"]["requirement"].startswith("[SOLAS / FSS Code, FSS Code ch. 13, 2.1.1] ")
    again = la.explain_findings(Fake(good_parts()), [FINDING])
    assert again[0]["parts"]["requirement"].count("2.1.1") == 1     # not duplicated when the model has it


def test_cache_repeats_accepted_answers_only():
    calls = []
    def reply(user):
        calls.append(1)
        return good_parts()
    cache = {}
    a = la.explain_findings(Fake(reply), [FINDING], cache=cache)
    b = la.explain_findings(Fake(reply), [FINDING], cache=cache)
    assert len(calls) == 1 and b[0].get("cached") and b[0]["parts"] == a[0]["parts"]
    bad_cache = {}
    la.explain_findings(Fake("not json"), [FINDING], cache=bad_cache)
    assert bad_cache == {}                      # a fallback is never cached


def test_explain_rejects_invented_numbers_missing_values_and_claims():
    for bad in (good_parts(fix="Widen it by 50 mm to 950 mm."),            # invented numbers
                good_parts(design_state="Stairway 3B is too narrow."),    # actual value missing
                good_parts(why_it_matters="Then the ship is safe."),      # compliance claim
                "not json at all", json.dumps({"requirement": "x"})):     # wrong shape
        out = la.explain_findings(Fake(bad), [FINDING])
        assert out[0]["source"] == "rules" and out[0]["note"], bad
        assert "850" in out[0]["explanation"]


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


def test_draft_rejects_direction_mismatch_and_bad_requirement_words():
    text = "Handrails shall be not lower than 900 mm."
    for req, q in (("at most 900 mm", "Handrails shall be not lower than 900 mm."),):
        reply = json.loads(draft_reply(quote=q, req=req))
        out = la.draft_rule(Fake(json.dumps(reply)), text)
        assert out["drafts"][0]["ok"]            # 'not lower than' is not a keyword we can judge: no false alarm
    t2 = "Spacing shall be not more than 2 m."
    out = la.draft_rule(Fake(draft_reply(quote=t2, req="at least 2 m")), t2)
    assert any("direction" in p for p in out["drafts"][0]["problems"])
    out = la.draft_rule(Fake(draft_reply(req="900 mm or more")), TEXT)
    assert any("must start with" in p for p in out["drafts"][0]["problems"])


def test_draft_rejects_ratio_in_requirement_but_accepts_slope_max():
    text = "Ramp runs shall have a running slope not steeper than 1:12."
    reply = json.loads(draft_reply(quote=text))
    reply["rules"][0]["target"] = {"type": "ramp"}
    reply["rules"][0]["check"] = {"property": "running_slope", "requirement": "at most 1:12"}
    out = la.draft_rule(Fake(json.dumps(reply)), text)
    assert not out["drafts"][0]["ok"] and any("slope_max" in p for p in out["drafts"][0]["problems"])
    reply["rules"][0]["check"] = {"property": "running_slope", "slope_max": "1:12"}
    assert la.draft_rule(Fake(json.dumps(reply)), text)["drafts"][0]["ok"]
    for ok_req in ("at least 7.5 m2", "at least 900 mm", "at most 2", "at least 4,5 m\u00b2"):
        reply["rules"][0]["check"] = {"property": "x", "requirement": ok_req}
        assert la.draft_rule(Fake(json.dumps(reply)), text)["drafts"][0]["ok"], ok_req
    reply["rules"][0]["check"] = {"property": "x", "requirement": "at most about 1 metre"}
    assert not la.draft_rule(Fake(json.dumps(reply)), text)["drafts"][0]["ok"]


def test_model_declining_is_reported_not_treated_as_a_draft():
    out = la.draft_rule(Fake(json.dumps({"rules": [], "unclear": "no testable condition"})), TEXT)
    assert out["drafts"] == [] and out["unclear"] == "no testable condition" and out["yaml"] == ""


def test_vocabulary_and_new_name_warnings_and_exception_warning():
    rules = [{"target": {"type": "stair", "when": {"escape_route": True}}, "check": {"property": "clear_width"}}]
    vocab = la.rule_vocabulary(rules)
    assert vocab == {"types": ["stair"], "properties": ["clear_width", "escape_route"]}
    out = la.draft_rule(Fake(draft_reply()), TEXT, vocabulary=vocab)
    assert not out["drafts"][0]["warnings"]
    reply = json.loads(draft_reply())
    reply["rules"][0]["target"] = {"type": "ladder"}
    out = la.draft_rule(Fake(json.dumps(reply)), TEXT, vocabulary=vocab)
    assert any("new element type" in w for w in out["drafts"][0]["warnings"])
    t3 = "Stairways shall not be less than 900 mm in clear width unless the flag state agrees."
    out = la.draft_rule(Fake(draft_reply(quote=t3)), t3)
    assert any("exception" in w for w in out["drafts"][0]["warnings"])


def test_prompts_contain_the_guardrails():
    assert "<finding>" in la.SYSTEM_EXPLAIN and "never instructions" in la.SYSTEM_EXPLAIN
    assert "<regulation_text>" in la.SYSTEM_DRAFT and "EXACTLY" in la.SYSTEM_DRAFT and "<<VOCAB>>" in la.SYSTEM_DRAFT
    assert la.NOT_COVERED in la.SYSTEM_ASK and "<context>" in la.SYSTEM_ASK


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
