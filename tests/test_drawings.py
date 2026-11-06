"""General-arrangement PDF reading (synthetic drawing with planted problems)."""

import json
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pypdfium2")

from ruletrace.engine import check_design  # noqa: E402
from ruletrace.ga_import import design_from_ga_pdf  # noqa: E402
from tests.conftest import ENG  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "ruletrace" / "samples"
PDF = SAMPLES / "demo_ga_drawing.pdf"


def test_reads_scale_particulars_and_rooms():
    d = design_from_ga_pdf(PDF)
    it = d.interpretation
    assert "1:150" in it["scale"] and "agrees" in it["scale"]
    assert d.ship.ship_type == "special_purpose" and d.ship.persons_total == 16 and d.ship.breadth_m == 11.0
    assert d.ship.complete_arrangement
    assert {x["deck"]: x["height_m"] for x in it["decks"]} == {"Shelter deck": 5.6, "Fo'c'sle deck": 8.4}
    cabins = [e for e in d.elements if e.type == "cabin"]
    assert len(cabins) == 12 and sum(e.props["berths"] for e in cabins) == 14
    captain = next(e for e in cabins if e.props.get("occupant") == "Captain")
    assert captain.props["floor_area"] == "16 m2" and captain.deck == "Fo'c'sle deck"
    # position in metres: frame 10 x 0.6 m = 6 m from frame 0, port side
    assert 5.5 < captain.box[0] + captain.box[3] / 2 < 8.5 and captain.box[1] < 0


def test_planted_problems_found_exactly():
    d = design_from_ga_pdf(PDF)
    spec = json.loads((SAMPLES / "demo_ga_drawing.expected.json").read_text())
    d.ship.keel_laid = date.fromisoformat(spec["ship_overrides"]["keel_laid"])
    got = {(f["rule_id"], f["element_id"], f["status"]) for f in check_design(d)["findings"] if f["status"] != "pass"}
    assert got == {(x["rule_id"], x["element_id"], x["status"]) for x in spec["expected"]}


def test_unknown_keel_date_does_not_guess():
    d = design_from_ga_pdf(PDF)
    r = check_design(d)
    assert all(not f["rule_id"].startswith("MLC") for f in r["findings"])
    assert any("keel-laying date not given" in " ".join(x["reasons"]) for x in r["rules_not_applicable"])


def test_scanned_or_textless_pdf_rejected(tmp_path):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    from ruletrace.model import DesignError
    p = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(p))
    c.rect(10, 10, 100, 100)
    c.save()
    with pytest.raises(DesignError, match="no readable text"):
        design_from_ga_pdf(p)


def test_upload_pdf_via_api(client):
    d = client.post("/ruletrace/designs", headers=ENG, files={"file": ("ga.pdf", PDF.read_bytes())}).json()
    assert d["format"] == "pdf-drawing"
    r = client.post(f"/ruletrace/designs/{d['id']}/check", headers=ENG,
                    json={"ship_overrides": {"keel_laid": "2025-06-01"}}).json()
    assert r["counts"]["fail"] == 5 and r["interpretation"]["found"]["cabin"] == 12
