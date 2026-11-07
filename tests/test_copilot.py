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
    assert set(r.json()["documents"]) == {"36CFR1191AppD", "46CFR190"}
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
    assert r["kind"] == "picture" and r["n_confirmed"] == 1
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
    d = indexed.post("/ruletrace/copilot/design", headers=ENG,
                     json={"readings_id": r["readings_id"], "ship": {"name": "Test", "length_m": 60}}).json()
    assert d["elements"] == 3  # the unconfirmed cabin is left out
    design = indexed.get(f"/ruletrace/designs/{d['id']}", headers=ENG).json()["design"]
    cabins = {e["name"]: e for e in design["elements"] if e["type"] == "cabin"}
    assert cabins["Crew Double 01-34-0"]["props"]["berths"] == 2
    assert "berths" not in cabins["Science 01-36-2"]["props"]  # Gemini's own count is not used
    res = indexed.post(f"/ruletrace/designs/{d['id']}/check", headers=ENG).json()
    assert res["counts"] and indexed.get("/audit/verify", headers=ENG).json()["ok"]
    # with the full ship profile the rule engine checks the Gemini-read arrangement itself
    full = indexed.post("/ruletrace/copilot/design", headers=ENG, json={"readings_id": r["readings_id"], "ship": {
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
    assert r["counts"] == {"PASS": 5, "FAIL": 4, "NEEDS REVIEW": 0}


def test_browse_search_and_clause_navigation(indexed):
    out = indexed.get("/ruletrace/copilot/browse?doc=46CFR190", headers=ENG).json()
    assert out["mode"] == "outline" and any(x["clause_id"] == "190.10-25(b)" for x in out["items"])
    hits = indexed.get("/ruletrace/copilot/browse?q=guard rail height", headers=ENG).json()["items"]
    assert any(h["clause_id"] == "190.25-5(a)" for h in hits[:5])
    c = indexed.get("/ruletrace/copilot/clause/46CFR190::190.10-25", headers=ENG).json()
    assert [x["clause_id"] for x in c["children"]] == ["190.10-25(a)", "190.10-25(b)", "190.10-25(c)"]
    b = indexed.get("/ruletrace/copilot/clause/46CFR190::190.10-25(b)", headers=ENG).json()
    assert b["parent"]["clause_id"] == "190.10-25" and "28 inches" in b["text"]
