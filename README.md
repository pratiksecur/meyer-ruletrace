# RuleTrace: ship design compliance checking

**Since AI Hackathon 2026 · Meyer Turku challenge: "Automated class and regulatory compliance checking"**

RuleTrace checks a ship design (an IFC 3D model or a structured design file) against a curated
set of rules from SOLAS, the FSS Code, the Load Line Convention, the Tonnage Convention, COLREG, MARPOL Annex VI, MLC, PVAG and ADA. It shows each
problem **on the 3D model** and says **why** it is not compliant: the rule, its paragraph, the quoted
text, what was required, what the design has, and how to fix it.

> AI reads the rules at machine speed. Engineers stay accountable. Every compliance claim can be traced to a paragraph and a person.

## What it does

| Meyer's criterion | How RuleTrace answers it |
| --- | --- |
| **Accuracy**: compliant vs non-compliant, few misleading findings | Rules are explicit, tested checks; every comparison (numbers with units, slopes, fire classes) is done in code. A value the design doesn't give is reported as **"can't check"**, never guessed. Labelled sample designs measure precision and recall (`scripts/evaluate.py`). |
| **Design interpretation**: documents, drawings, 3D models; show *why* | Reads **general-arrangement drawings (vector PDF)**, **IFC 3D models** and a simple JSON format. From a GA it measures the scale from the frame numbers (cross-checked with the title block), finds each deck plan and its height, and reads every room label ("2 PERS. 15 m2", "SICKBAY 20 m2") into a positioned room. Failing parts turn red in the 3D viewer; each finding shows required vs actual and the source text. |
| **Quality and usability**: a solid foundation for an engineering tool | Engineer-first workflow (upload, see issues on the model, fix, re-check), "what-if" ship-profile changes, a tamper-evident audit record of every check, role-based access, tests. |

Also built in:
- **Applicability:** the ship profile (type, passengers, length, gross tonnage, keel-laying date, flag, operating areas) decides which rules apply, and the report lists the rules that *don't* apply and why. The rules are only for **new ships**, by keel-laying date.
- **Geographic rules:** tick where the ship will operate (US ports, North American / Baltic / North Sea / Canadian Arctic emission control areas…) and re-check. PVAG/ADA and MARPOL Tier III switch on or off.
- **Human approval:** every finding needs an engineer's decision and every check a second person's sign-off (see below).
- **Conflicts between regulations:** when two rules set limits no single value can meet, RuleTrace says so and leaves the decision to people.
- **Knock-on effects:** fixing one finding can break another rule. The sample `demo_cruise_masthead_cascade` raises the forward masthead light to fix COLREG 2(a)(i), and the 4.5 m separation rule 2(a)(ii) then fails. RuleTrace flags it straight away.

## Architecture

```
 GA drawing (PDF) ─┐
 IFC 3D model     ─┼─▶ importers ─▶ design model (ship profile + elements with 3D boxes and properties)
 JSON design      ─┘                              │
                                                ▼
 rule pack (YAML, each rule: source, quote, ─▶ rule engine ─▶ findings (pass / fail / warn / can't check)
   applicability, check, fix)                   │                         │
                                                ▼                         ▼
                                      audit chain (Vartija core)   3D viewer + findings panel
```

- `ruletrace/model.py`: design model and JSON loader
- `ruletrace/ga_import.py`: GA drawing reader (PDFium): scale from frame numbers, deck plans and heights, room labels, principal particulars, title-block statements
- `ruletrace/ifc_import.py`: IFC reader (ifcopenshell): class mapping, property sets, world-space bounding boxes
- `ruletrace/dashboard.py` + `static/dashboard.html`: engineer and executive dashboard, built from the recorded design checks
- `ruletrace/rulesets.py` + `rulesets_api.py` + `static/rulesets.html`: read a regulation document into draft rules and analyse them against the designs and the current rules
- `ruletrace/engine.py`: applicability, safe expressions, checks, findings, conflicts between regulations
- `ruletrace/reviews.py`: engineer decisions on findings, four-eyes sign-off
- `ruletrace/revisions.py`: change measurements and re-check, compare two revisions
- `ruletrace/posture.py`, `ruletrace/admin.py`: compliance (CISO) posture view and the Administration tab
- `ruletrace/llm_assist.py`, `llm_actions.py`, `llm_api.py`: optional local AI helpers (Ollama); they never decide compliance
- `ruletrace/static/parts.js`, `report.js`: detailed 3D parts, printable compliance report
- `ruletrace/access.yaml`: who sees what (engineer, compliance, executive, administrator)
- `ruletrace/rules/*.yaml`: the rule pack (35 rules: Tonnage Convention gross tonnage from the enclosed volume, SOLAS/FSS fire and escape, SOLAS II-1 collision bulkhead and double bottom, Load Line deck openings and guard rails, COLREG lights, PVAG/ADA, MARPOL VI Tier III, MLC 2006 crew accommodation, drawing consistency)
- `ruletrace/api.py`: HTTP API; `ruletrace/app.py`: adds it to the Vartija core app
- `ruletrace/static/index.html`: 3D viewer (three.js, bundled so it works offline)
- Built on **Vartija core** (prior work; feature freeze at tag `pre-event-2026-11-05`, installed from release `v0.2.0`, which only adds a packaging fix): login and roles, audit hash chain, document sources. RuleTrace uses only Vartija's public API (`create_app`, `Principal`, `current_principal`, `audit.append`, `quantities.parse_quantity` / `check_requirement`, `settings.Settings`).

## RuleMate Copilot: ask the regulations, read drawings with Gemini

A side panel in the 3D viewer (**✦ Copilot** in the header, or **Ask the regulations** on any finding). It adds
tools the rule pack does not have on its own, behind the same login, roles and audit chain:

| Tab | What it does | How it stays trustworthy |
| --- | --- | --- |
| **Ask the regulations** | Answers questions from the **full official texts** (46 CFR Part 190 for research vessels and the ADA/ABA technical requirements, 825 clauses, public domain, in `ruletrace/copilot_rules/`), with clause citations you can click open. | Every sentence must cite a retrieved clause that supports it; every number, unit and quote must be in that clause; otherwise the clause excerpts are shown, or "not found in the rules". Checked in code, not by the model. A question the texts do not cover (e.g. "difference between SOLAS and MLC") also gets a **general answer**, labelled *general knowledge, not from the loaded rules, not verified*, with no citations and no verdict. The bot uses **Groq and Ollama only**. |
| **Check values** | Design values from a form, a **CSV**, a **picture** or a **drawing page (PDF)**: Gemini reads the written dimensions, code confirms each on the page and measures stair angles from a PDF's vector lines; then each value (`30`, `1500 mm`, `2'-6"`) against limits extracted from the full texts: PASS / FAIL / NEEDS REVIEW with the deciding limit and clause; example CSV and download. | Limits are validated against the clause text; units converted with pint; the verdict is computed in code. A limit with a condition code cannot confirm gives NEEDS REVIEW. A limit only applies to a value of the same order of magnitude that shares a specific word with it, so a ship's 60 m length is never "checked" against a berth width: unmatched values are NEEDS REVIEW, "no matching limit". |
| **Browse** | The full texts as an outline per document, search, and every clause with its parent, sub-clauses, references and "referred to by"; *Ask about this clause*. | Verbatim official text (eCFR), split by its own numbering. |
| **Accuracy** | Measured accuracy of every part (answers, checks, drawing reading), with sample sizes and caveats, and the log of engineer decisions. | Numbers from `eval/run.py` (RuleMate repo), stored in `ruletrace/copilot_rules/accuracy.json`; general answers are shown as *not measured*. |
| **Gemini (optional)** | A **picture** (screenshot from the design tool, sketch, photo with dimensions) or a **drawing page** (PDF, including **scanned** drawings, which the GA reader rejects). Pictures: every written value is checked against the full texts (e.g. a 1500 mm turning circle = 59.06 in, ADA 304.3.1 needs 60 in: FAIL). Drawings: the cabins and hospital become a RuleTrace design, checked by the rule engine and shown in 3D. | Gemini only proposes. A reading is used only when local OCR finds its label, number or value on the image at the position Gemini reports; berths only when the drawing states them ("CREW DOUBLE", "SCIENTIST (2)"). Everything else is shown as "Gemini only" and not used. PASS/FAIL comes from code. |

**Human approval.** Wherever code cannot settle something alone, an engineer decides and the decision is written
to the audit chain (`copilot.reviewed`, `copilot.approved`): NEEDS REVIEW verdicts (*does the condition hold?*),
values read by Gemini, general answers, and turning a drawing into a RuleTrace design (the engineer unticks wrong
rooms and confirms; without `approved=true` the API refuses with 428).
**Comparison charts.** Check results show a verdict breakdown and *design value vs rule limit* bars (same unit, the
limit as a line); readings show *read by Gemini vs confirmed in code*; the Accuracy tab shows each metric as a bar.

Tested on the brief's public drawings (not in this repo, owners' notices): the scanned 1990 NOAA research-ship
study, second deck: 34 of 34 cabins and the hospital read and confirmed; the rule engine then passes
MLC A3.1 para 12 (hospital) and fails the berth total (57 stated on the plan vs 60 persons: one room has no
berth number). The RCRV GA (vector CAD): 143 of 160 labels confirmed; 8 science and 7 + 3 crew cabins match the
RCRV design summary.

```bash
python -m pip install -e ".[copilot]"     # adds the Copilot's dependencies (embeddings, OCR, units)
ollama pull qwen3:8b                      # the local bot model (confidential mode), ~5 GB
python -m uvicorn ruletrace.app:app --port 8001
```
Open the viewer, click **✦ Copilot**, then *Build the index* once (about 30 s; it also downloads the Maritime
Labour Convention 2006 from the ILO, which is not committed; `scripts/fetch_mlc.py` does the same from the command line).

**Confidential mode is ON by default (for Meyer designs): nothing leaves this computer.** The bot answers with the
local Ollama model only; Groq and Gemini are off even if their keys are set (the server refuses them, and the panel
shows *🔒 Confidential mode*). Retrieval, OCR, unit conversion and every PASS / FAIL always run locally. For
confidential work keep **no Gemini key** in `.env`.

**Switching it off (public documents only).** In the panel, *Turn off (public documents only)* asks the engineer to
confirm that no Meyer data will be used and to give a reason; it then allows Groq (and Gemini, if a key is set) for
60 minutes and switches back on by itself. Every switch is in the audit log. `COPILOT_CONFIDENTIAL_LOCK=1` locks it
on for a Meyer installation.

The cloud keys go in `.env` (git-ignored); `COPILOT_CONFIDENTIAL=0` starts with confidential mode off:
`GROQ_API_KEY=...` (bot, free; then you can choose among three Groq models and see each
one's measured accuracy), and optionally `GEMINI_API_KEY=...` (reads drawings and pictures only). The panel then
shows *⚠ Cloud models allowed*. Free Gemini keys allow about 20 requests per model per day; the Copilot rotates over
the free flash models (`rulemate/llm.py`, `GEMINI_FREE_MODELS`), and readings are cached per image under
`VARTIJA_UPLOAD_DIR/copilot`.

Code: `ruletrace/copilot_api.py` (endpoints, added like `llm_api.py`), `ruletrace/static/copilot.js` / `.css` (panel),
`rulemate/` (retrieval, answer checks, rule-text parsers, unit-aware checks, Gemini readers), `tests/test_copilot.py`.

## Deployment

### Local (Windows / macOS / Linux), about 5 minutes

Needs Python 3.10+ (tested on 3.13) and git.

```bash
git clone https://github.com/pratiksecur/meyer-ruletrace.git
cd meyer-ruletrace
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1    macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[ifc,drawings,dev]"   # also installs Vartija core v0.2.0 from GitHub
python -m pytest -q                              # all tests should pass
python -m uvicorn ruletrace.app:app --port 8001
```

Open **http://localhost:8001/ruletrace**, then *Load a sample → Demo cruise ship: Deck 7*.

Then open **http://localhost:8001/ruletrace/dashboard** (or click *Dashboard*). It is built from the
design checks you just ran, read back from the audit chain. Switch person with *Signed in as* (demo users:
Mikko, design engineer; Aino, compliance lead and approver; Ella, admin). Who sees what is set in `ruletrace/access.yaml`
and enforced by the API:
- **Engineer view** (roles `engineer`, `naval_architect`, `design_engineer`, `admin`): every design with its status and
  failing rules, open non-compliances by regulation (with simple bar and pie charts), each design's review state, a
  link back to each design in 3D, and the audit trail of checks (verify the record, download it as JSON for class).
- **Compliance view** (roles `ciso`, `compliance_lead`, `class_manager`, `grc_manager`, `auditor`): read-only
  organisation posture (AT RISK / WATCH / CLEAR, with the rating rule shown), rule coverage, AI use, human-approval
  progress and the audit record. These roles can view designs and approve sign-offs but cannot upload or change designs.
- **Executive view** (roles `ceo`, `management`, `executive`, `board`): designs ready vs needing changes, where the
  issues are, and an estimate of rework cost avoided and engineer time saved. The money figures use placeholder
  assumptions in `ruletrace/value.yaml`, shown and editable on screen; replace them with the yard's own numbers.
- **Administration tab** (role `admin`): roles and what each can do, recent activity, system health (rules
  verified, audit chain intact, AI helper status).
- *Remove* on a design hides it from the designs list and the dashboard (the audit record keeps it).
The database (`vartija.db`) and uploads (`data/`) are created in the folder you start the server from.

**Working on Vartija at the same time?** Clone it next to this repo and override the pinned copy:
`python -m pip install -e ../Vartija/vartija-core`. Undo with `python -m pip install -e ".[ifc,drawings,dev]"`.

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

### General-arrangement drawing (vector PDF)
Upload the PDF in the viewer. The reader reports how it read the drawing ("How the drawing was read"):
- **Scale** from the rows of frame numbers along each deck plan (straight-line fit) and the stated frame
  spacing, cross-checked against the title-block scale (e.g. "measured 1:150, title block says 1:150 (agrees)").
- **Deck plans** from the deck titles; **deck heights** from the profile legend (e.g. "11300" → 11.3 m ABL).
- **Rooms** from labels: `2 PERS. 15 m2` → two-berth cabin, `SICKBAY 20 m2` → hospital, `GALLEY 30 m2` → space.
  Positions come from the label position; footprints are squares of the stated area (approximate).
- **Principal particulars** and **title-block statements** ("ACCOMMODATION FOR 38 PERSONS", "SHELTER DECK:
  6 SINGLE AND 10 DOUBLE CABINS"), so the plan is checked against what the drawing itself claims.
- The keel-laying date and gross tonnage are usually not on a GA. Set the keel date in the viewer to apply
  date-based rules. With unknown gross tonnage, the strictest MLC size band is used, so a pass holds for any size.
- Scanned (image-only) PDFs have no text layer; they are read with OCR when Tesseract is installed (see *Real
  drawings*), otherwise rejected with a clear message.
- "Does it exist on board?" checks (e.g. the hospital) only run on complete arrangements, never on partial models.

Tested on the public DANA V general arrangement linked in Meyer Turku's brief (28 cabins, sickbay and
44 other spaces read in under a second; plans agree with the title block). That drawing carries a
confidentiality notice from its owners, so it is **not** included in this repository. A synthetic GA with
planted problems is: `ruletrace/samples/demo_ga_drawing.pdf` (`scripts/make_ga_sample.py`).

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

## Human approval: nothing is accepted automatically

- **Every open finding needs an engineer's decision** (right panel, in each finding): *Confirm: must be fixed*, or
  *Dismiss* with a reason (false finding, doesn't apply, accepted deviation agreed with class, information to
  follow) and a written explanation. A dismissal without one is refused.
- **Sign-off, four-eyes:** when every open finding is reviewed, the engineer signs off *as prepared*; a second
  person with an approver role (`ciso`, `grc_manager`, `admin`, `compliance_lead`, `class_surveyor`) approves it
  or sends it back with a note. The person who prepared it can't approve it. Signed-off checks are locked until
  reopened. The dashboard shows each design's review state; the report prints the decisions and both names.
- **New rules too:** on *New rule sets* an engineer approves or rejects each draft rule (a rejection needs a
  reason); a second person with an approver role puts the approved rules into use. Rules with a flaw (conflict,
  weaker than today) can't go live. Active company rules run on every check; *Take out of use* removes them.
- Every decision is written to the audit chain (`finding.reviewed`, `check.prepared`, `check.approved`,
  `rule.reviewed`, `ruleset.activated`) with the design's fingerprint.

## AI helpers (optional, local)

With [Ollama](https://ollama.com) running on the same machine (`ollama pull mistral`), the viewer offers:
*Explain in plain words* on each finding, *suggest fixes* (each suggestion is re-checked by the rule engine before it is
shown), *what-if* questions in plain language (turned into checked value changes), questions about the rule pack, and
drafting rules from regulation text (always `verified: false`). The model never decides compliance: every number and
pass/fail comes from the rule engine, citations are checked against what was given to the model, and each use is
written to the audit chain. Without Ollama, everything else works. Settings: `RULETRACE_LLM_BASE_URL`
(default `http://localhost:11434`), `RULETRACE_LLM_MODEL` (default `mistral`), `RULETRACE_LLM_TIMEOUT`, `RULETRACE_LLM_NUM_CTX`.

## When two regulations conflict

Some parts are covered by two rules that set opposite limits on the same physical value. RuleTrace finds these
and reports a **"Two regulations conflict"** finding naming both rules, their limits and why no value meets both.
The sample *Demo cruise ship: open-deck door on an accessible route* shows the classic case: a weathertight door
to the open deck that is also on the accessible route, where the Load Line Convention needs a sill of at least
380 mm and the ADA a threshold of at most 12.7 mm (1/2 inch) (a door's sill and threshold are treated as one physical thing).

RuleTrace never decides which rule wins. The finding lists the usual ways out (change the design so both are met;
precedence of mandatory flag-state rules over guidance and owner requirements, confirmed with the Administration;
an equivalent arrangement accepted by the flag or class under SOLAS reg. I/5 / Load Line Article 8), and the
decision goes through the normal engineer review and second-person sign-off, into the audit record and the report.
New rule sets are checked for the same kind of conflict before they are used.

## Changing a design and comparing revisions

- **Change measurements** (in the details of any part): type new values (e.g. a cabin's area or a door's width), or
  edit the **ship particulars** (length, breadth, persons, gross tonnage, keel date, operating areas). Changes are
  shown as pending; *Re-check* saves them as a new revision and checks it, so you can see straight away which rules
  now fail. Example on a GA: a single cabin of 15 m² changed to 6 m² fails MLC 2006 (at least 7 m²).
- **3D detail:** each part type is drawn as a recognisable object (doors with frames and sills, stairs with treads,
  rails with courses, cabins, lights on masts); failing parts are red with a label, and a parts list shows every part.

- **Compare two revisions** (left panel, *Compare two revisions*): pick the older and the newer design, or *Try it:
  the cruise ship before and after fixes*. RuleTrace checks both with the same rules and lists what was **fixed**,
  what is **still open**, and what the change **broke**, plus every part whose values changed. The demo shows a
  knock-on effect: splitting a fire zone fixes the 48 m limit but makes the next zone longer than 40 m.
  Parts are matched by ID, so keep IDs stable between revisions (IFC GlobalIds and drawing labels usually are).
- **Report (PDF)** (above the findings): a printable compliance report of the current check, with every finding,
  the rule quote, required vs design, the fix, the rules that don't apply and why, the design and rule-pack
  fingerprints, the check-history entry, and sign-off boxes. Use the browser's *Save as PDF*.

## Demo start (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\start-demo.ps1        # clean start on port 8002, opens the browser
powershell -ExecutionPolicy Bypass -File .\start-demo.ps1 -Keep  # keep the designs and check history
```

## New rule sets: will they work?

Open **http://localhost:8001/ruletrace/rulesets/page** (or *New rule sets* in the header) and upload a company,
owner, class or flag requirement document: PDF, Word (.docx), text, Markdown, HTML, or RuleTrace YAML. Try
*the sample owner's standard* (`ruletrace/samples/rules/demo_owner_spec.md`, with deliberate flaws).

1. Every "shall / must / not less than" sentence with a measurable value about a part RuleTrace models
   (stairs, corridors, doors, ramps, ventilators, air pipes, guard rails, cabins, double bottom, main vertical
   zones, collision bulkhead) becomes a **draft rule**, quoted word for word and marked `verified: false`.
   Sentences it can't turn into a check are listed for an engineer with the reason (no metric value, a part
   not modelled yet, ...). With *AI helper* ticked and Ollama running, those sentences are also offered to the
   local model, under the same checks as `llm_assist.draft_rule`.
2. The draft rules are run on the designs in RuleTrace (or the samples, if none are loaded) next to the current
   rule pack, and each gets a verdict: **works**, **conflicts** (can't be met together with a current or another
   new rule), **weaker than a current rule** (it would pass parts the current rule fails, listed), **stricter**
   (the new issues it raises, listed), **duplicate**, **can't be checked** (the designs lack the value) or
   **not tested** (no design has that kind of part).
3. Nothing joins the live rule pack automatically: download the draft YAML, fix what was flagged, check it
   against the official text and add a test case. Each analysis is written to the audit chain.

The comparison is exact for fixed values (at least / at most / slope); rules whose limit depends on the
ship (formulas) are run and reported, but not compared value by value.

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
from the ship's breadth), `actual_expr` (a value computed from the geometry), `fire_class_at_least`, `slope_max`,
`equals`, `equals_expr`.
Applicability keys: `ship_types`, `passenger_ship`, `passengers_more_than`, `persons_at_least`, `keel_laid_on_or_after`,
`length_m_at_least`, `gross_tonnage_at_least`, `enclosed_volume_given`, `complete_arrangement`, `operating_areas_any`.
`target.type` can be one part type or a list (e.g. `[door, weather_door]`).

## Accuracy

```bash
python scripts/evaluate.py
```
It reports precision and recall over the labelled designs in `ruletrace/samples/`. **Be honest about it:** the
samples are synthetic and were labelled by the same team that wrote the rules, so 100% shows the
engine does what the rules say. It is not proof that the rules are complete. All 35 rules are marked
`verified: true`: each value was checked against a published source, linked in its `source_url` (consolidated
ICLL text, FSS Code res. MSC.98(73), 2010 ADA Standards, and others). New rules start as `verified: false`.

### Real drawings

```bash
python scripts/real_drawings.py dana.pdf atlantic.pdf gosars.pdf
```
Reads the three real, public GA drawings linked in Meyer's brief (from three different design offices) and
compares what RuleTrace read with what each drawing states. The stated values were typed in by hand into
`evaluation/real_drawings.json`; the drawings are not stored here (download them from the links in that file).

| Drawing | How it is read | Read in | Stated values read correctly |
| --- | --- | --- | --- |
| DTU Dana V | PDF text layer | 1 s | 12 of 12 (dimensions, scale, persons, main-deck height, cabins per deck, hospital) |
| 60 m fishery research vessel (Atlantic Shipdesign) | PDF text layer, two-column dimension table | 0.5 s | 8 of 8 (dimensions, scale, three deck heights) |
| G.O. Sars | no text layer: OCR, plus a sharper second pass over the dimensions table | 85 s | 10 of 10 (dimensions, scale, persons, four deck heights) |

Be honest about it too: the reader was improved on these same drawings (in the morning it could only read
Dana; the other two were rejected), so the fair next test is a drawing it has never seen. Room names on the
Atlantic and G.O. Sars drawings are drawn as lines rather than text, so their cabins are not read yet and
room rules report "can't check" rather than guessing. The G.O. Sars sheet measures 1:215 against a stated
1:150, which RuleTrace reports as "plotted one paper size smaller".

Optional, for drawings without a text layer (like G.O. Sars): install Tesseract OCR and make sure `tesseract`
is on the PATH. Windows: `winget install UB-Mannheim.TesseractOCR`, then add `C:\Program Files\Tesseract-OCR`
to PATH. Linux: `apt install tesseract-ocr`. Without it, such drawings are rejected with a clear message.

## Limits (say these if asked)

- Rule texts are summaries and quotes from public sources. Class rules (e.g. DNV) and full IMO texts must be checked against the official editions before real use.
- Hull and deck rules (collision bulkhead, double bottom, Load Line openings and rails) are the SOLAS / ICLL requirements that class rules repeat. The collision-bulkhead check does not yet model the bulbous-bow reference point (SOLAS II-1/12.2). We could not get DNV's own rule text at the event, so no rule cites a DNV paragraph; add one once access is available.
- The rule pack is a focused starter set, not full coverage. Each rule is small to add and test.
- GA reading uses text labels and their positions. Room outlines (the drawn lines) are not traced yet, so footprints are approximate, and dimension lines are not yet used. Drawings without a text layer are read with OCR (Tesseract), which is slower and reads dimensions and labels, not room outlines.
- Conflicts between regulations are found for fixed limits on the same property of the same part (e.g. sill /
  threshold height); limits from formulas are not compared yet.
- PVAG were published by the US Access Board as proposed guidelines. Confirm their status for each project.

## Data and IP

No Meyer Turku design data was used. All sample designs are synthetic. three.js is MIT licensed
(`ruletrace/static/three.LICENSE`); ifcopenshell is LGPL and pypdfium2 is Apache-2.0/BSD (both installed separately, not bundled).
Third-party drawings (e.g. from the brief's links) must not be committed: they carry their owners' notices.
