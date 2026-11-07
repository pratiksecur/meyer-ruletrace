"""RuleMate Copilot endpoints, with fake models (no Gemini or Ollama needed)."""
import pytest

pytest.importorskip("rank_bm25", reason='Copilot extra not installed: pip install -e ".[copilot]"')

from tests.conftest import CEO, ENG  # noqa: E402


class FakeLLM:
    label = "fake:copilot"
    model = "fake"
    timeout = 10

    def __init__(self, reply):
        self.reply = reply

    def complete(self, messages, **kw):
        return self.reply


@pytest.fixture
def indexed(client):
    r = client.post("/ruletrace/copilot/index", headers=ENG)
    assert r.status_code == 200, r.text
    assert {"36CFR1191AppD", "46CFR190"} <= set(r.json()["documents"])  # + MLC2006 where downloaded
    return client


def test_status_and_roles(client):
    s = client.get("/ruletrace/copilot/status", headers=ENG).json()
    assert s["installed"] is True
    assert client.get("/ruletrace/copilot/status", headers=CEO).status_code == 403


def test_ask_cites_the_full_text_and_is_audited(indexed, monkeypatch):
    import rulemate.llm

    good = "Door openings shall provide a clear width of 32 inches minimum [36CFR1191AppD § 404.2.3]."
    monkeypatch.setattr(rulemate.llm, "get_llm", lambda spec=None: FakeLLM(good))
    a = indexed.post("/ruletrace/copilot/ask", headers=ENG,
                     json={"question": "What clear width must an accessible door opening have?"}).json()
    assert a["status"] == "verified" and a["citations"][0]["clause_id"] == "404.2.3"
    c = indexed.get(f"/ruletrace/copilot/clause/{a['citations'][0]['uid']}", headers=ENG).json()
    assert "32 inches" in c["text"]
    assert indexed.get("/audit/verify", headers=ENG).json()["ok"]


def test_ask_rejects_an_invented_citation(indexed, monkeypatch):
    import rulemate.llm

    bad = "Doors must be 40 inches wide [36CFR1191AppD § 999.9]."
    monkeypatch.setattr(rulemate.llm, "get_llm", lambda spec=None: FakeLLM(bad))
    a = indexed.post("/ruletrace/copilot/ask", headers=ENG,
                     json={"question": "What clear width must an accessible door opening have?", "docs": ["36CFR1191AppD"]}).json()
    assert a["status"] in ("excerpts", "not_found") and "40 inches" not in a["text"]


def test_read_picture_checks_only_confirmed_values(indexed, monkeypatch):
    import io

    from PIL import Image

    import rulemate.design.picture as picture

    monkeypatch.setenv("GEMINI_API_KEY", "test")
    proposals = [{"space": "Bathroom", "parameter": "door clear width", "value_text": '30"', "unit": '"',
                  "box_2d": [100, 100, 140, 300]},
                 {"space": "Cabin", "parameter": "turning space diameter", "value_text": "1500", "unit": "mm",
                  "box_2d": [600, 600, 650, 800]}]  # nothing written there
    monkeypatch.setattr(picture, "ask_gemini", lambda llm, png, **k: proposals)
    real = picture.read_picture
    monkeypatch.setattr(picture, "read_picture",
                        lambda data, llm, words=None: real(data, llm, words=[('BATH DOOR CLEAR 30"', 90, 45, 320, 75)]))
    buf = io.BytesIO()
    Image.new("RGB", (1000, 500), "white").save(buf, format="PNG")
    r = indexed.post("/ruletrace/copilot/read", headers=ENG,
                     files={"file": ("cabin.png", buf.getvalue(), "image/png")}).json()
    assert r["kind"] == "picture", r.get("in_short")
    assert r["n_confirmed"] == 1
    door, turn = r["items"]
    assert door["verdict"] == "FAIL" and any("404.2.3" in c for c in door["clauses"])
    assert turn["confirmed"] is False and turn["verdict"] is None


def test_drawing_reading_becomes_a_ruletrace_design(indexed, monkeypatch):
    from pathlib import Path

    import rulemate.design.extract as extract
    import rulemate.design.vision as vision
    from rulemate.design.vision import VisionRoom

    monkeypatch.setenv("GEMINI_API_KEY", "test")
    rooms = [VisionRoom("CREW DOUBLE", "01-34-0", "stateroom", 2, (600, 400, 640, 428), True, True),
             VisionRoom("SCIENCE", "01-36-2", "stateroom", 1, (500, 400, 540, 428), True, False),
             VisionRoom("HOSPITAL", "01-38-1", "hospital", None, (400, 400, 440, 428), True),
             VisionRoom("CREW DOUBLE", "09-99-9", "stateroom", 2, (100, 100, 140, 128), False, True)]  # not confirmed
    monkeypatch.setattr(vision, "read_sheet", lambda path, page, llm, words, dpi=150: rooms)
    monkeypatch.setattr(extract, "ocr_page", lambda path, page, dpi=200: [])
    pdf = Path(__file__).parents[1] / "ruletrace" / "samples" / "demo_ga_drawing.pdf"
    r = indexed.post("/ruletrace/copilot/read", headers=ENG, data={"page": "1"},
                     files={"file": ("scan.pdf", pdf.read_bytes(), "application/pdf")}).json()
    assert r["kind"] == "drawing" and r["summary"] == {"cabins": 2, "berths_confirmed": 2, "hospital": True}
    no = indexed.post("/ruletrace/copilot/design", headers=ENG,
                      json={"readings_id": r["readings_id"], "ship": {"name": "Test", "length_m": 60}})
    assert no.status_code == 428  # an engineer must approve the readings first
    d = indexed.post("/ruletrace/copilot/design", headers=ENG,
                     json={"readings_id": r["readings_id"], "ship": {"name": "Test", "length_m": 60},
                           "approved": True, "note": "checked against the sheet"}).json()
    assert d["elements"] == 3  # the unconfirmed cabin is left out
    design = indexed.get(f"/ruletrace/designs/{d['id']}", headers=ENG).json()["design"]
    cabins = {e["name"]: e for e in design["elements"] if e["type"] == "cabin"}
    assert cabins["Crew Double 01-34-0"]["props"]["berths"] == 2
    assert "berths" not in cabins["Science 01-36-2"]["props"]  # Gemini's own count is not used
    res = indexed.post(f"/ruletrace/designs/{d['id']}/check", headers=ENG).json()
    assert res["counts"] and indexed.get("/audit/verify", headers=ENG).json()["ok"]
    # with the full ship profile the rule engine checks the Gemini-read arrangement itself
    full = indexed.post("/ruletrace/copilot/design", headers=ENG, json={"readings_id": r["readings_id"], "approved": True, "ship": {
        "name": "Test", "persons_total": 4, "keel_laid": "2026-01-15", "complete_arrangement": True}}).json()
    res = indexed.post(f"/ruletrace/designs/{full['id']}/check", headers=ENG).json()
    status = {f["rule_id"]: f["status"] for f in res["findings"]}
    assert status.get("GA-CONSISTENCY-TOTAL-BERTHS") == "fail"  # 2 stated berths on the sheet vs 4 persons


def test_read_needs_gemini_key(indexed, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = indexed.post("/ruletrace/copilot/read", headers=ENG, files={"file": ("x.png", b"123", "image/png")})
    assert r.status_code == 503


def test_check_values_against_the_full_texts(indexed):
    rows = [{"space": "Accessible cabin bathroom", "parameter": "door clear width", "value": "30", "unit": "in"},
            {"space": "Accessible cabin", "parameter": "turning space diameter", "value": "1500", "unit": "mm"},
            {"space": "Interior stairway", "parameter": "stairway clear width", "value": "2'-6\""}]
    r = indexed.post("/ruletrace/copilot/check", headers=ENG, json={"rows": rows}).json()
    v = {x["parameter"]: x for x in r["results"]}
    assert v["door clear width"]["verdict"] == "FAIL" and "404.2.3" in v["door clear width"]["deciding"]["clause"]
    assert v["turning space diameter"]["verdict"] == "FAIL"  # 1500 mm = 59.06 in < 60 in (304.3.1)
    assert v["stairway clear width"]["verdict"] == "PASS"  # 2'-6" = 30 in >= 28 in (46 CFR 190.10-25(b))
    assert indexed.get("/audit/verify", headers=ENG).json()["ok"]


def test_check_example_csv(indexed):
    csv = indexed.get("/ruletrace/copilot/example.csv", headers=ENG).text
    r = indexed.post("/ruletrace/copilot/check/csv", headers=ENG, data={"docs": "36CFR1191AppD"},
                     files={"file": ("example.csv", csv.encode(), "text/csv")}).json()
    assert r["counts"] == {"PASS": 5, "FAIL": 4, "NEEDS REVIEW": 0, "NO RULE": 0, "CHECK INPUT": 0}


def test_browse_search_and_clause_navigation(indexed):
    out = indexed.get("/ruletrace/copilot/browse?doc=46CFR190", headers=ENG).json()
    assert out["mode"] == "outline" and any(x["clause_id"] == "190.10-25(b)" for x in out["items"])
    hits = indexed.get("/ruletrace/copilot/browse?q=guard rail height", headers=ENG).json()["items"]
    assert any(h["clause_id"] == "190.25-5(a)" for h in hits[:5])
    c = indexed.get("/ruletrace/copilot/clause/46CFR190::190.10-25", headers=ENG).json()
    assert [x["clause_id"] for x in c["children"]] == ["190.10-25(a)", "190.10-25(b)", "190.10-25(c)"]
    b = indexed.get("/ruletrace/copilot/clause/46CFR190::190.10-25(b)", headers=ENG).json()
    assert b["parent"]["clause_id"] == "190.10-25" and "28 inches" in b["text"]


def test_check_file_pdf_measures_stairs_without_gemini(indexed, monkeypatch):
    """A drawing page without a Gemini key: the stair angles are still measured from the vector lines."""
    import rulemate.design.measure as measure
    import rulemate.design.extract as extract
    from rulemate.design.measure import Flight
    from pathlib import Path

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(extract, "ocr_page", lambda path, page, dpi=200: [])
    monkeypatch.setattr(measure, "stair_flights", lambda path, page, words: [
        Flight(angle=52.0, bbox=(1, 1, 2, 2), stringers=2, label="STAIRWAY", room_no="1-25-0", raw_angles=(52.0, 52.0))])
    pdf = Path(__file__).parents[1] / "ruletrace" / "samples" / "demo_ga_drawing.pdf"
    r = indexed.post("/ruletrace/copilot/check/file", headers=ENG, data={"page": "1", "docs": "46CFR190"},
                     files={"file": ("ga.pdf", pdf.read_bytes(), "application/pdf")}).json()
    assert r["kind"] == "pdf" and r["counts"]["FAIL"] == 1  # 52 degrees > 50 (46 CFR 190.10-25(b))
    assert "no Gemini key" in " ".join(r["notes"])


def test_check_file_csv(indexed):
    csv = indexed.get("/ruletrace/copilot/example.csv", headers=ENG).text
    r = indexed.post("/ruletrace/copilot/check/file", headers=ENG, data={"docs": "36CFR1191AppD"},
                     files={"file": ("example.csv", csv.encode(), "text/csv")}).json()
    assert r["kind"] == "csv" and r["counts"]["FAIL"] == 4



def test_values_no_rule_covers_are_no_rule(indexed):
    rows = [{"space": "Main dimensions", "parameter": "length overall", "value": "60", "unit": "m"},
            {"space": "Main dimensions", "parameter": "frame spacing", "value": "600", "unit": "mm"}]
    r = indexed.post("/ruletrace/copilot/check", headers=ENG, json={"rows": rows}).json()
    assert r["counts"]["NO RULE"] == 2 and r["counts"]["PASS"] == 0  # never a false PASS


def test_too_few_rooms_make_no_design(indexed, monkeypatch):
    from pathlib import Path

    import rulemate.design.extract as extract
    import rulemate.design.vision as vision
    from rulemate.design.vision import VisionRoom

    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(vision, "read_sheet", lambda path, page, llm, words, dpi=150: [
        VisionRoom("CAPTAIN", None, "stateroom", 1, (600, 400, 640, 428), True, False)])
    monkeypatch.setattr(extract, "ocr_page", lambda path, page, dpi=200: [])
    pdf = Path(__file__).parents[1] / "ruletrace" / "samples" / "demo_ga_drawing.pdf"
    r = indexed.post("/ruletrace/copilot/read", headers=ENG, data={"page": "1"},
                     files={"file": ("ga.pdf", pdf.read_bytes(), "application/pdf")}).json()
    d = indexed.post("/ruletrace/copilot/design", headers=ENG,
                     json={"readings_id": r["readings_id"], "ship": {}, "approved": True})
    assert d.status_code == 422 and "too few" in d.json()["detail"]


def test_bot_never_uses_gemini(monkeypatch):
    from ruletrace.copilot_api import provider

    monkeypatch.delenv("COPILOT_LLM_PROVIDER", raising=False)
    assert provider() == "groq,ollama"
    monkeypatch.setenv("COPILOT_LLM_PROVIDER", "gemini,groq")
    assert provider() == "groq"


def test_question_outside_the_rules_gets_a_labelled_general_answer(indexed, monkeypatch):
    import rulemate.llm

    monkeypatch.setattr(rulemate.llm, "get_llm", lambda spec=None: FakeLLM("NOT_FOUND"))
    import rulemate.general

    real = rulemate.general.general_answer
    monkeypatch.setattr(rulemate.general, "general_answer",
                        lambda q, llm=None, retriever=None: real(q, FakeLLM(GENERAL), retriever))
    a = indexed.post("/ruletrace/copilot/ask", headers=ENG,
                     json={"question": "What is the difference between SOLAS and MLC for a cruise ship?"}).json()
    assert a["status"] == "not_found" and a["general"]["ok"]
    assert "not from the loaded rules" in a["general"]["label"] and "SOLAS" in a["general"]["text"]
    assert not a["citations"]  # a general answer never cites a clause
    refs = {r["source"]: r for r in a["general"]["references"]}
    assert refs["SOLAS Chapter II-2"]["status"] == "named source, not loaded"
    assert refs["ADA 404.2.3"]["status"] == "number NOT in this clause"  # 30 inches is not in 404.2.3
    assert refs["Ship Interiors Blog"]["status"] == "not identified" and not refs["Ship Interiors Blog"]["url"]
    assert any("NOT in" in w or "not attributed" in w or "does not contain" in w for w in a["general"]["warnings"])


GENERAL = """{"answer": "SOLAS covers safety; MLC covers crew. An accessible door needs 30 inches.",
 "references": [{"source": "SOLAS Chapter II-2", "supports": "fire safety", "url": "https://www.imo.org/en/About/Conventions"},
                {"source": "ADA 404.2.3", "supports": "door clear width 30 inches"},
                {"source": "Ship Interiors Blog", "supports": "MLC covers crew", "url": "https://example.com/blog"}],
 "certainty": "medium"}"""


def test_choose_the_bot_model(indexed, monkeypatch):
    s = indexed.get("/ruletrace/copilot/status", headers=ENG).json()
    ids = [m["id"] for m in s["models"]]
    assert ids[0] == "auto" and "groq:openai/gpt-oss-120b" in ids and not any("gemini" in i for i in ids)
    bad = indexed.post("/ruletrace/copilot/ask", headers=ENG, json={"question": "door width?", "model": "gemini:x"})
    assert bad.status_code == 400
    import rulemate.llm

    seen = []

    class P(FakeLLM):
        def complete(self, messages, **kw):
            seen.append(self.model)
            return "Door openings shall provide a clear width of 32 inches minimum [36CFR1191AppD § 404.2.3]."

    monkeypatch.setattr(rulemate.llm, "make_provider", lambda name: P(""))
    a = indexed.post("/ruletrace/copilot/ask", headers=ENG, json={
        "question": "What clear width must an accessible door have?", "model": "groq:openai/gpt-oss-20b"}).json()
    assert seen and seen[0] == "openai/gpt-oss-20b" and a["status"] == "verified"


def test_review_is_recorded_and_listed(indexed):
    r = indexed.post("/ruletrace/copilot/review", headers=ENG, json={
        "kind": "verdict", "ref": "D03 berth clear height 30 in", "decision": "approve", "note": "berths not stacked"})
    assert r.status_code == 200 and r.json()["by"]
    assert indexed.post("/ruletrace/copilot/review", headers=ENG, json={
        "kind": "anything", "ref": "x", "decision": "approve"}).status_code == 422
    lst = indexed.get("/ruletrace/copilot/reviews", headers=ENG).json()
    assert lst[0]["ref"] == "D03 berth clear height 30 in" and lst[0]["decision"] == "approve"
    assert indexed.get("/audit/verify", headers=ENG).json()["ok"]
    assert indexed.post("/ruletrace/copilot/review", headers=CEO, json={
        "kind": "verdict", "ref": "x", "decision": "approve"}).status_code == 403


def test_accuracy_and_chart_numbers(indexed):
    acc = indexed.get("/ruletrace/copilot/accuracy", headers=ENG).json()
    assert acc["groups"] and acc["caveats"]
    r = indexed.get("/ruletrace/copilot/example.csv", headers=ENG).text
    out = indexed.post("/ruletrace/copilot/check/csv", headers=ENG,
                       files={"file": ("ex.csv", r.encode(), "text/csv")}).json()
    d = next(x["deciding"] for x in out["results"] if x["deciding"])
    assert {"limit_value", "value_in_limit_unit", "unit", "comparator"} <= set(d)


def test_mlc_clause_references():
    from rulemate.retrieval import extract_clause_ids

    assert extract_clause_ids("What does Standard A3.1, paragraph 6(a) require?") == ["A3.1/6(a)"]
    assert extract_clause_ids("What does Standard A3.1 paragraph 12 require?") == ["A3.1/12"]  # not "paragraph 12"
    assert extract_clause_ids("Explain regulation II-2/14.2.2") == ["II-2/14.2.2"]


def test_mlc_parser_and_limits():
    from rulemate.compliance.limits import extract_regex, validate
    from rulemate.ingest.loaders import Block
    from rulemate.ingest.parsers import MlcParser, detect_style
    from rulemate.ingest.pipeline import build_clauses

    lines = [("Regulation 3.1 – Accommodation and recreational facilities", 2),
             ("Standard A3.1 – Accommodation and recreational facilities", 3), ("9. With respect to requirements for sleeping rooms:", None),
             ("(e) the minimum inside dimensions of a berth shall be at least 198 centimetres by 80 centimetres;", None),
             ("(f) in single berth seafarers’ sleeping rooms the floor area shall not be less than:", None),
             ("(i) 4.5 square metres in ships of less than 3,000 gross tonnage;", None),
             ("(ii) 5.5 square metres in ships of 3,000 gross tonnage or over but less than 10,000 gross tonnage;", None),
             ("(g) more text", None), ("(h) more", None), ("(i) on passenger ships the floor area shall be large;", None),
             ("Standard A3.2 – Food", 3), ("Standard A3.3 – X", 3)]
    blocks = [Block(t, 45, "text", lvl) for t, lvl in lines]
    assert detect_style(blocks) == "mlc"
    p = MlcParser()
    cl = {c.clause_id: c for c in build_clauses("MLC2006", p.parse(blocks), p, False)}
    assert {"R3.1", "A3.1", "A3.1/9", "A3.1/9(e)", "A3.1/9(f)", "A3.1/9(i)"} <= set(cl)
    assert cl["A3.1/9(e)"].parent_id == "A3.1/9" and cl["A3.1"].parent_id == "R3.1"
    assert "(ii) 5.5 square metres" in cl["A3.1/9(f)"].text  # roman items stay inside the lettered item
    berth = [validate(r, cl["A3.1/9(e)"], i, "regex")[0] for i, r in enumerate(extract_regex(cl["A3.1/9(e)"]))]
    assert [(x.value, x.unit) for x in berth] == [(198, "centimetres"), (80, "centimetres")]
    area = [validate(r, cl["A3.1/9(f)"], i, "regex")[0] for i, r in enumerate(extract_regex(cl["A3.1/9(f)"]))]
    assert [(x.value, x.condition) for x in area] == [(4.5, "in ships of less than 3,000 gross tonnage"),
        (5.5, "in ships of 3,000 gross tonnage or over but less than 10,000 gross tonnage")]
