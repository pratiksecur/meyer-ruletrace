# RuleTrace: ship design compliance checking

**Since AI Hackathon 2026 · Meyer Turku challenge: "Automated class and regulatory compliance checking"**

RuleTrace checks a ship design (an IFC 3D model or a structured design file) against a curated
set of rules from SOLAS, the FSS Code, COLREG, MARPOL Annex VI, PVAG and ADA. It shows each
problem **on the 3D model** and says **why** it is not compliant: the rule, its paragraph, the quoted
text, what was required, what the design has, and how to fix it.

> AI reads the rules at machine speed. Engineers stay accountable. Every compliance claim can be traced to a paragraph and a person.

## What it does

| Meyer's criterion | How RuleTrace answers it |
| --- | --- |
| **Accuracy**: compliant vs non-compliant, few misleading findings | Rules are explicit, tested checks; every comparison (numbers with units, slopes, fire classes) is done in code. A value the design doesn't give is reported as **"can't check"**, never guessed. Labelled sample designs measure precision and recall (`scripts/evaluate.py`). |
| **Design interpretation**: documents, drawings, 3D models; show *why* | Reads **IFC** models (geometry plus property sets) and a simple JSON design format. Fire-zone lengths are measured from the 3D shape. Failing parts turn red in the 3D viewer; each finding shows required vs actual and the source text. |
| **Quality and usability**: a solid foundation for an engineering tool | Engineer-first workflow (upload, see issues on the model, fix, re-check), "what-if" ship-profile changes, a tamper-evident audit record of every check, role-based access, tests. |

Also built in:
- **Applicability:** the ship profile (type, passengers, length, gross tonnage, keel-laying date, flag, operating areas) decides which rules apply, and the report lists the rules that *don't* apply and why. The rules are only for **new ships**, by keel-laying date.
- **Geographic rules:** tick where the ship will operate (US ports, North American / Baltic / North Sea / Canadian Arctic emission control areas…) and re-check. PVAG/ADA and MARPOL Tier III switch on or off.
- **Knock-on effects:** fixing one finding can break another rule. The sample `demo_cruise_masthead_cascade` raises the forward masthead light to fix COLREG 2(a)(i), and the 4.5 m separation rule 2(a)(ii) then fails. RuleTrace flags it straight away.

## Architecture

```
 IFC 3D model ─┐
 JSON design  ─┼─▶ importers ─▶ design model (ship profile + elements with 3D boxes and properties)
 (drawings: next)┘                              │
                                                ▼
 rule pack (YAML, each rule: source, quote, ─▶ rule engine ─▶ findings (pass / fail / warn / can't check)
   applicability, check, fix)                   │                         │
                                                ▼                         ▼
                                      audit chain (Vartija core)   3D viewer + findings panel
```

- `ruletrace/model.py`: design model and JSON loader
- `ruletrace/ifc_import.py`: IFC reader (ifcopenshell): class mapping, property sets, world-space bounding boxes
- `ruletrace/engine.py`: applicability, safe expressions, checks, findings
- `ruletrace/rules/*.yaml`: the rule pack (17 rules: SOLAS/FSS fire and escape, COLREG lights, PVAG/ADA, MARPOL VI Tier III)
- `ruletrace/api.py`: HTTP API; `ruletrace/app.py`: adds it to the Vartija core app
- `ruletrace/static/index.html`: 3D viewer (three.js, bundled so it works offline)
- Built on **Vartija core** (prior work, tag `pre-event-2026-11-05`): login and roles, audit hash chain, document sources, dashboard

## Deployment

### Local (Windows / macOS / Linux), about 5 minutes

Needs Python 3.10+.

```bash
# 1. Vartija core (prior work)
git clone https://github.com/sahilpimple/Vartija.git
# 2. this repo
git clone <this repo> meyer-ruletrace
cd meyer-ruletrace
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1    macOS/Linux: source .venv/bin/activate
python -m pip install -e ../Vartija/vartija-core -e ".[ifc,dev]"
python -m pytest -q                       # all tests should pass
python -m uvicorn ruletrace.app:app --port 8001
```

Open **http://localhost:8001/ruletrace**, then *Load a sample → Demo cruise ship: Deck 7*.
The Vartija dashboard (sources, audit, executive view) is at http://localhost:8001/dashboard.

### Settings (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `VARTIJA_DATABASE_URL` | `sqlite:///./vartija.db` | Audit chain database. Use PostgreSQL in production; keep SQLite **outside OneDrive/Dropbox** folders |
| `VARTIJA_UPLOAD_DIR` | `./data` | Uploaded designs and documents (git-ignored) |
| `VARTIJA_AUTH` / `VARTIJA_JWT_SECRET` | `headers` | Use `jwt` with the company's single sign-on token in production |
| `VARTIJA_EVENT_SECRET` | dev default | Change for any shared deployment |
| `VARTIJA_MAX_UPLOAD_MB` | `50` | Upload size limit |

### Server / container

```bash
pip install -e /path/to/vartija-core -e ".[ifc]"
VARTIJA_DATABASE_URL=postgresql+psycopg://user:pass@db/ruletrace VARTIJA_AUTH=jwt VARTIJA_JWT_SECRET=... \
  uvicorn ruletrace.app:app --host 0.0.0.0 --port 8001 --workers 2
```
Put it behind the company reverse proxy (TLS). Everything runs on-premises. No design data
leaves the network, and no external AI service is needed for checking.

## Design input format

### IFC (recommended for 3D models)
- Element classes are mapped automatically: `IfcDoor`→door, `IfcStair`→stair, `IfcWall`→bulkhead, `IfcRamp`→ramp, `IfcEngine`→diesel_engine, `IfcSpace` named "corridor" / "fire zone".
- Properties come from every property set (`ClearWidth`→`clear_width`). `Pset_WallCommon.FireRating`→`fire_class`.
- A `RuleTrace` property set can set `Type`, `Id`, `Role`, `EscapeRoute`, `AccessibleRoute`, `Boundary`.
- The ship profile comes from a `RuleTrace_Ship` property set on `IfcProject` (Name, ShipType, Passengers, LengthM, BreadthM, GrossTonnage, KeelLaid, Flag, OperatingAreas).
- A door's `OverallWidth` is **not** used as its clear width (the frame takes space). Model the clear width, or the check says "can't check".
- Example: `ruletrace/samples/demo_deck_section.ifc` (built by `scripts/make_ifc_sample.py`).

### JSON
```json
{"title": "...", "ship": {"name": "...", "ship_type": "passenger", "passengers": 2800, "length_m": 300,
  "breadth_m": 38, "gross_tonnage": 180000, "keel_laid": "2026-03-01", "flag": "FI",
  "operating_areas": ["us_ports", "north_american_eca"]},
 "elements": [{"id": "ST-3B", "type": "stair", "name": "Stairway 3B", "deck": "Deck 7", "zone": "MVZ3",
   "box": [182, 8, 27, 5, 2, 6], "props": {"escape_route": true, "clear_width": "850 mm"}}]}
```
`box` = [x, y, z, length, width, height] in metres (x forward from the aft perpendicular, y to starboard from the centreline, z up from the baseline).
Operating areas: `us_ports`, `north_american_eca`, `us_caribbean_eca`, `baltic_sea`, `north_sea`, `mediterranean`, `canadian_arctic`.

## Adding a rule

Add an entry to a file in `ruletrace/rules/`, then add a labelled sample case and run the tests:

```yaml
- id: FSS-13.2.1.1-STAIR-WIDTH
  title: Stairway clear width on escape routes
  regime: SOLAS / FSS Code
  ref: "FSS Code ch. 13, 2.1.1"
  quote: "Stairways shall not be less than 900 mm in clear width."
  source_url: https://...
  verified: true                      # value checked against published text
  applies_if: {passenger_ship: true, keel_laid_on_or_after: 2002-07-01}
  target: {type: stair, when: {escape_route: true}}
  check: {property: clear_width, requirement: "at least 900 mm"}
  fix: Widen the stairway to at least 900 mm clear width between handrails.
```
Check types: `requirement` (number with units), `requirement_expr` (formula, e.g. COLREG masthead height
from the ship's breadth), `actual_expr` (a value computed from the geometry), `fire_class_at_least`, `slope_max`, `equals`.
Applicability keys: `ship_types`, `passenger_ship`, `passengers_more_than`, `keel_laid_on_or_after`,
`length_m_at_least`, `gross_tonnage_at_least`, `operating_areas_any`.

## Accuracy

```bash
python scripts/evaluate.py
```
It reports precision and recall over the labelled designs in `ruletrace/samples/`. **Be honest about it:** the
samples are synthetic and were labelled by the same team that wrote the rules, so 100% shows the
engine does what the rules say. It is not proof that the rules are complete. Rules marked `verified: false`
(the escape door/corridor widths) need checking against the current FSS Code text before use.

## Limits (say these if asked)

- Rule texts are summaries and quotes from public sources. Class rules (e.g. DNV) and full IMO texts must be checked against the official editions before real use.
- The rule pack is a focused starter set, not full coverage. Each rule is small to add and test.
- General-arrangement PDF drawings are not read yet. Next: extract room labels and dimensions from vector PDFs, with an engineer confirming the result.
- PVAG were published by the US Access Board as proposed guidelines. Confirm their status for each project.

## Data and IP

No Meyer Turku design data was used. All sample designs are synthetic. three.js is MIT licensed
(`ruletrace/static/three.LICENSE`); ifcopenshell is LGPL (installed separately, not bundled).
