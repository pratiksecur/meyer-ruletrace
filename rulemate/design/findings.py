"""Requirements that a design document triggers, checked against what the document shows.

Some rules are not a limit on a measured value but a requirement that applies when facts about the ship hold:
"each vessel which makes voyages of more than 3 days ... and which carries a crew of 12 or more, must be provided
with a hospital space" (46 CFR 190.20-35(a)). Each trigger below names the clause it comes from; it is only used
when that clause is in the loaded rules, and every finding quotes the facts and the page it used. Findings are for
an engineer to confirm, never a PASS/FAIL verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ingest.pipeline import load_index


@dataclass
class Trigger:
    doc: str
    clause_id: str
    needs: tuple[str, ...]  # fact parameters required
    applies: Callable[[dict], bool]
    check: Callable[[dict, list[dict]], tuple[str, str]]  # -> (status, message); status: met | not shown | review


def _hospital(f: dict, drawings: list[dict]) -> tuple[str, str]:
    if not drawings:
        return "not shown", "no GA drawing was read - confirm a hospital space on the GA"
    from .extract import similar

    hits = [d for d in drawings if any(similar(w, "HOSPITAL", 0.75) for w in d.get("words", []))]
    plans = [d for d in hits if re.search(r"PLAN", f"{d.get('view', '')} {d.get('title', '')}", re.I) or d.get("rooms")]
    if plans:
        d = plans[0]
        what = f"{d['title'] or d.get('view') or 'plan'}".title()
        return "met", f"a space labelled HOSPITAL is on p.{d['page']} (Dwg {d['drawing']}, {what})"
    if hits:
        d = hits[0]
        return "review", (f"HOSPITAL is mentioned on p.{d['page']} (Dwg {d['drawing']}) but not found on a plan view - "
                          "confirm on the GA")
    return "not shown", "no space labelled HOSPITAL on the drawings read - confirm on the GA"


def _mess(f: dict, drawings: list[dict]) -> tuple[str, str]:
    return "review", (f"the mess room seats {f['messroom seats']:g} of {f['total complement']:g} persons at one sitting; "
                      "confirm how many eat at one time (sittings / watches)")


TRIGGERS = [
    Trigger("46CFR190", "190.20-35(a)", ("crew", "endurance"),
            lambda f: f["crew"] >= 12 and f["endurance"] > 3, _hospital),
    Trigger("46CFR190", "190.20-30(b)", ("messroom seats", "total complement"),
            lambda f: f["messroom seats"] < f["total complement"], _mess),
]


def gemini_findings(ex) -> list[str]:
    """What Gemini read on the drawings, what code confirmed, and how it compares with the stated facts."""
    readings = [r for d in ex.drawings for r in d.get("gemini", [])]
    if not readings:
        return []
    conf = [r for r in readings if r["confirmed"]]
    out = [f"Gemini read {len(readings)} labelled spaces on the GA sheets; code confirmed {len(conf)} against the "
           f"drawing text at the reported position ({len(readings) - len(conf)} shown as 'Gemini only', not used)."]
    rooms = {}
    for r in conf:
        if r["kind"] == "stateroom":
            # the same cabin appears on a plan and on the profile ("SCIENCE 01-15-0" / "SCI 01-15-0"): one room
            key = r["room_number"] or f"{r['label'].upper()} {r['bbox']}"
            rooms[key] = r
    crew = [r for r in rooms.values() if "CREW" in r["label"].upper()]
    sci = [r for r in rooms.values() if re.search(r"SCI|TECH", r["label"].upper())]
    facts = {x.parameter: x for x in ex.facts if x.item_id.startswith("T")}
    if rooms:
        out.append(f"Staterooms confirmed on the drawings: {len(rooms)} ({len(sci)} science / marine tech, {len(crew)} "
                   f"labelled CREW, {len(rooms) - len(sci) - len(crew)} named for a role, e.g. CHIEF MATE).")
    if "science double staterooms" in facts and sci:
        stated = facts["science double staterooms"]
        out.append(f"Science staterooms: {len(sci)} on the drawings vs {stated.value:g} in the text "
                   f"(p.{stated.source_page}){'' if len(sci) == stated.value else ' - check'}.")
    crew_rooms = [r for r in rooms.values() if r not in sci and r["kind"] == "stateroom"]
    crew_double = [r for r in crew_rooms if "DOUBLE" in r["label"].upper()]
    # a cabin named for a role (CAPTAIN, CHIEF ENG) exists once; the same name with two room numbers is one cabin
    crew_single = list({" ".join(r["label"].upper().split()): r for r in crew_rooms if r not in crew_double}.values())
    if "crew single berths" in facts and "crew double berths" in facts and crew_rooms:
        fs, fd = facts["crew single berths"].value, facts["crew double berths"].value
        ok = len(crew_single) == fs and len(crew_double) == fd
        out.append(f"Crew cabins: {len(crew_single)} single (named for a role: "
                   f"{', '.join(sorted({r['label'].title() for r in crew_single}))}) and {len(crew_double)} CREW DOUBLE on "
                   f"the drawings vs {fs:g} single and {fd:g} double in the text (p.{facts['crew single berths'].source_page})"
                   f"{'' if ok else ' - check'}.")
    if "science double staterooms" in facts:
        low = [r for r in sci if r["berths"] == 1]
        if low:
            out.append(f"Gemini counted 1 drawn berth in {len(low)} science stateroom(s) that the text calls double: "
                       "upper and lower berths drawn in plan look like one outline. Berth counts not confirmed by the "
                       "label are not used for checks.")
    unlabelled = [r for r in conf if r["kind"] == "stateroom" and r["berths"] and not r["berths_confirmed"]
                  and not re.search(r"\(\d+\)", r["label"])]
    if unlabelled and any(d.get("roles_without_count") for d in ex.drawings):
        add = sum(r["berths"] for r in unlabelled if any(w in r["label"].upper() for d in ex.drawings
                                                         for w in d.get("roles_without_count", [])))
        if add:
            drawn = sum(d["berths"] for d in ex.drawings)
            out.append(f"Gemini counts {add} drawn berth(s) in rooms that have no berth number on the drawing "
                       f"({', '.join(' '.join(r['label'].split()) for r in unlabelled if any(w in r['label'].upper() for d in ex.drawings for w in d.get('roles_without_count', [])))}): "
                       f"{drawn} + {add} = {drawn + add} berths. Not confirmed by a printed number - for an engineer to check.")
    disagree = [r for r in readings if any("but the label says" in n for n in r.get("notes", []))]
    for r in disagree:
        out.append(f"Gemini counted berths that disagree with the label {r['label']} {r['room_number'] or ''}: "
                   f"{'; '.join(r['notes'])} - not used.")
    return out


def rule_findings(ex) -> list[str]:
    facts = {}
    for x in ex.facts:
        if not x.item_id.startswith("G"):
            facts.setdefault(x.parameter, x)
    try:
        loaded = {(c.doc, c.clause_id) for c in load_index().clauses}
    except FileNotFoundError:
        loaded = set()
    out = []
    for t in TRIGGERS:
        if (t.doc, t.clause_id) not in loaded or not all(n in facts for n in t.needs):
            continue
        vals = {n: facts[n].value for n in t.needs}
        if not t.applies(vals):
            continue
        status, msg = t.check(vals, ex.drawings)
        basis = ", ".join(f"{n} {facts[n].value:g} {facts[n].unit} (p.{facts[n].source_page})" for n in t.needs)
        out.append(f"[{t.doc} § {t.clause_id}] applies ({basis}): {msg}. Status: {status}.")
    return out
