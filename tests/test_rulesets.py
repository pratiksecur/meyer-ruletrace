from pathlib import Path

from ruletrace import rulesets as rs
from ruletrace.model import load_design
from tests.conftest import CEO, ENG

SAMPLES = Path(__file__).resolve().parent.parent / "ruletrace" / "samples"


def _verdicts():
    rules, manual, _ = rs.extract_rules(rs.read_text(SAMPLES / "rules" / "demo_owner_spec.md"), "Owner spec")
    designs = [(p.name, load_design(p)) for p in sorted(SAMPLES.glob("*.json")) if not p.name.endswith(".expected.json")]
    a = rs.analyze(rules, designs)
    return {v["id"].split("-", 3)[-1]: v for v in a["verdicts"]}, manual, a


def test_extraction_quotes_and_flags_what_it_cannot_check():
    v, manual, _ = _verdicts()
    assert len(v) == 9 and all(x["quote"] for x in v.values())
    reasons = {m["ref"]: m["reason"] for m in manual}
    assert "metric" in reasons["1.10"] and "property" in reasons["1.11"] and "models" in reasons["1.12"]


def test_analysis_finds_conflicts_weaker_rules_gaps_and_duplicates():
    v, _, a = _verdicts()
    by_ref = {x["ref"].split()[-1]: x for x in v.values()}
    assert by_ref["1.3"]["kind"] == "flaw" and "PVAG-V404.2.3-DOOR-WIDTH" in by_ref["1.3"]["verdict"]   # 700 mm max vs 815 mm min
    assert by_ref["1.4"]["kind"] == "flaw"                                                              # conflicts with 1.3
    assert by_ref["1.2"]["kind"] == "flaw" and by_ref["1.2"]["hides"]                                   # 850 mm hides 880 mm corridor
    assert by_ref["1.5"]["kind"] == "flaw" and "ADA-405.2-RAMP-SLOPE" in by_ref["1.5"]["verdict"]       # 1:8 weaker than 1:12
    assert by_ref["1.6"]["kind"] == "info"                                                              # same as Load Line 900 mm
    assert by_ref["1.9"]["kind"] == "gap"                                                               # no design has a helideck
    assert by_ref["1.1"]["kind"] == "ok" and "Stricter" in by_ref["1.1"]["verdict"]
    assert a["overall"].startswith("Not ready")


def test_ruleset_api_roles_and_yaml(client):
    r = client.post("/ruletrace/rulesets/sample/demo_owner_spec", headers=ENG).json()
    assert r["counts"]["flaw"] >= 3 and r["designs_source"] == "RuleTrace's sample designs"
    y = client.get(f"/ruletrace/rulesets/{r['id']}/yaml", headers=ENG).text
    assert "verified: false" in y and "DRAFT" in y
    body = (SAMPLES / "rules" / "demo_owner_spec.md").read_bytes()
    up = client.post("/ruletrace/rulesets", headers=ENG, files={"file": ("spec.txt", body)}, data={"name": "Spec"}).json()
    assert up["rules"] == 9
    assert client.post("/ruletrace/rulesets", headers=ENG, files={"file": ("x.exe", b"MZ")}).status_code == 400
    assert client.get("/ruletrace/rulesets", headers=CEO).status_code == 403
    assert "rulesets" in client.get("/ruletrace/rulesets/page").text
    assert client.get("/audit/verify", headers=ENG).json()["ok"]


def test_word_and_pdf_documents_are_read(tmp_path):
    import zipfile
    body = "\n".join(f'<w:p><w:r><w:t>{line}</w:t></w:r></w:p>' for line in
                     ["2.1 Stairways", "Stairways on escape routes shall have a clear width of not less than 1000 mm."])
    p = tmp_path / "spec.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    rules, _, _ = rs.extract_rules(rs.read_text(p), "Spec")
    assert rules[0]["check"] == {"property": "clear_width", "requirement": "at least 1000 mm"}
    import pytest
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    pdf = tmp_path / "spec.pdf"
    c = canvas.Canvas(str(pdf))
    c.drawString(72, 720, "3.1 Guard rails")
    c.drawString(72, 700, "Guard rails shall be at least 1100 mm high.")
    c.save()
    rules, _, _ = rs.extract_rules(rs.read_text(pdf), "Spec")
    assert rules[0]["target"]["type"] == "guard_rail"
