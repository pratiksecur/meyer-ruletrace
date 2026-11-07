"""Structure-aware clause parsers.

Each numbering style gets a small parser:

* ``SolasParser``   - "Chapter II-2" / "Regulation 13" / "3.1.1 ..."  -> "II-2/13.3.1"
* ``ClassParser``   - "Pt.3 Ch.2" / "Section 4" / "[2.1.3] ..."      -> "Pt.3 Ch.2 Sec.4 [2.1.3]"
* ``CfrParser``     - US CFR "Subpart 190.10" / "§ 190.10-25" / "(b)" -> "190.10-25(b)"
* ``AdaParser``     - ADA / ABA standards "403 Walking Surfaces" / "403.5.1 Clear Width. ..." -> "403.5.1"
* ``MlcParser``     - ILO MLC 2006 "Standard A3.1" / "6." / "(a)"                    -> "A3.1/6(a)"
* ``DecimalParser`` - plain "4.2.1 ..." numbering                     -> "4.2.1"
* ``FallbackParser``- headings only (or nothing): sections "S1", "S2", ...

All numbered parsers share one state machine (``NumberedParser.parse``); a
style only defines its context headings, how IDs are written and how
cross-references are resolved. ``pick_parser`` chooses a style per document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .loaders import Block

NUM = r"\d+(?:\.\d+){0,6}"
CLAUSE_LINE = re.compile(rf"^\[?(?P<num>{NUM})\]?\.?(?:\s+(?P<rest>\S.*))?$")
CAPTION = re.compile(r"^Table\s+(?P<n>[A-Z]?\d+(?:[.\-]\d+)*)\s*[-–—:.]?\s*(?P<title>.*)$")
LIST_ITEM = re.compile(r"^(\.\d+|\([a-z0-9ivx]+\)|[a-z]\)|[-•*]\s)")
# numbers followed by a unit are values, not cross-references
UNITS = (
    r"(?:mm|cm|m|m²|m2|m3|km|s|sec|min|h|degrees?|deg|°|%|N|kN|kg|t|bar|kPa|MPa|"
    r"lux|lx|persons?|people|passengers|kW|W|V|A|dB|inch(?:es)?|ft|feet|foot|square|cubic|"
    r"centimet(?:er|re)s?|millimet(?:er|re)s?|met(?:er|re)s?)\b"
)
NOT_A_VALUE = rf"(?!\.?\d)(?!\s*{UNITS})"


@dataclass
class Draft:
    clause_id: str
    title: str
    page: int | None
    kind: str
    parent_id: str | None
    ctx: dict
    lines: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        out = ""
        for line in self.lines:
            if not out:
                out = line
            elif line.startswith("|") or out.endswith("|") or LIST_ITEM.match(line):
                out += "\n" + line
            elif out.endswith("-") and line[:1].islower():
                out += line  # hyphenated line wrap
            else:
                out += " " + line
        return out.strip()


def parse_num(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.split("."))


def plausible_next(prev: tuple[int, ...] | None, new: tuple[int, ...], jump: int = 5) -> bool:
    """Is ``new`` a believable next clause number after ``prev``?

    Filters out lines that only *look* numbered (wrapped values, page numbers,
    years). Allows children, siblings and ancestors' siblings, with small gaps
    for deleted paragraphs.
    """
    if prev is None:
        return new[0] <= 99 and all(1 <= x <= 30 for x in new[1:])
    c = 0
    while c < min(len(prev), len(new)) and prev[c] == new[c]:
        c += 1
    if c == len(new):  # new equals prev or is one of its ancestors
        return False
    tail = new[c + 1 :]
    if not all(1 <= x <= jump for x in tail):
        return False
    if c == len(prev):  # descends into a child
        return 0 <= new[c] <= jump
    return prev[c] < new[c] <= prev[c] + jump


def looks_like_title(rest: str) -> bool:
    return (
        len(rest) <= 90
        and len(rest.split()) <= 12
        and not re.search(r"[.;:,]$", rest)
        and not re.search(r"\b(shall|must|should|may not)\b", rest, re.I)
    )


def _clean_heading_title(t: str) -> str:
    return re.sub(r"^[\s\-–—:.]+", "", t).strip()


class NumberedParser:
    name = "decimal"

    # ---- style hooks -------------------------------------------------------
    def reset(self, blocks: list[Block]) -> None:
        self.ctx: dict = {}

    def match_context(self, line: str) -> tuple[str, str] | None:
        """If ``line`` is a structural heading (chapter, regulation, section),
        update self.ctx and return (clause_id, title)."""
        m = re.match(r"^chapter\s+(?P<n>\d+)\b\.?\s*(?P<t>.*)$", line, re.I)
        if m and _heading_ok(m.group("t")):
            self.ctx = {"chapter": m.group("n")}
            return f"Ch.{m.group('n')}", _clean_heading_title(m.group("t"))
        return None

    def make_id(self, num: str) -> str:
        chap = self.ctx.get("chapter")
        if chap and num.split(".")[0] != chap:
            return f"Ch.{chap} {num}"
        return num

    def context_id(self) -> str | None:
        chap = self.ctx.get("chapter")
        return f"Ch.{chap}" if chap else None

    def table_id(self, n: str) -> str:
        return f"Table {n}"

    def find_refs(self, text: str, ctx: dict) -> list[str]:
        saved, self.ctx = self.ctx, ctx
        refs = [
            self.make_id(m.group(1))
            for m in re.finditer(
                rf"\b(?:see|section|clause|paragraphs?|in|with|under|to|and)\s+({NUM}){NOT_A_VALUE}",
                text,
                re.I,
            )
            if "." in m.group(1) or re.match(r"(?i)section|clause|paragraph", m.group(0))
        ]
        refs += [self.table_id(m.group(1)) for m in re.finditer(r"\bTable\s+([A-Z]?\d+(?:\.\d+)*)", text)]
        self.ctx = saved
        return refs

    # ---- shared state machine ---------------------------------------------
    def parse(self, blocks: list[Block]) -> list[Draft]:
        self.reset(blocks)
        drafts: list[Draft] = []
        cur: Draft | None = None  # where continuation text goes
        prev_num: tuple[int, ...] | None = None
        table_n = 0

        def start(cid, title, page, kind, parent) -> Draft:
            d = Draft(cid, title, page, kind, parent, dict(self.ctx))
            drafts.append(d)
            return d

        for b in blocks:
            if b.kind == "table":
                if cur is not None and cur.kind == "table" and not any(l.startswith("|") for l in cur.lines):
                    cur.add(b.text)  # table body right after its caption
                else:
                    table_n += 1
                    owner = cur.clause_id if cur else (self.context_id() or "doc")
                    t = start(f"{owner} table {table_n}", cur.title if cur else "", b.page, "table", owner)
                    t.add(b.text)
                continue

            line = b.text.strip()
            if not line:
                continue

            ctx_head = self.match_context(line)
            if ctx_head:
                cid, title = ctx_head
                cur = start(cid, title, b.page, "heading", self._ctx_parent(cid))
                prev_num = None
                continue

            cap = CAPTION.match(line)
            if cap:
                cur = start(self.table_id(cap.group("n")), cap.group("title").strip(), b.page, "table",
                            self.context_id() or (cur.clause_id if cur else None))
                cur.add(line)
                continue

            m = CLAUSE_LINE.match(line)
            if m:
                rest = (m.group("rest") or "").strip()
                num_t = parse_num(m.group("num"))
                starts_ok = not rest or rest[0].isupper() or rest[0] in "(\"'“["
                if starts_ok and plausible_next(prev_num, num_t):
                    prev_num = num_t
                    num = m.group("num")
                    parent = self.make_id(num.rsplit(".", 1)[0]) if "." in num else self.context_id()
                    if rest and looks_like_title(rest):
                        cur = start(self.make_id(num), rest, b.page, "heading", parent)
                    else:
                        cur = start(self.make_id(num), "", b.page, "clause", parent)
                        if rest:
                            cur.add(rest)
                    continue

            if cur is None:
                cur = start("preamble", "", b.page, "section", None)
            if cur.kind == "table" and cur.lines and cur.lines[-1].startswith("|") and not line.startswith(("Note", "NOTE", "*")):
                # text after a finished table belongs to the clause that owns the table
                owner = next((d for d in reversed(drafts) if d.clause_id == cur.parent_id), None)
                if owner is not None:
                    cur = owner
            cur.add(line)
        return drafts

    def _ctx_parent(self, cid: str) -> str | None:
        return None


def _heading_ok(title: str) -> bool:
    t = _clean_heading_title(title)
    return len(t) <= 120 and not t.endswith(".") and (not t or t[0].isupper() or t[0].isdigit())


# ---------------------------------------------------------------- SOLAS style

_ROMAN_CHAP = r"[IVX]+(?:-\d+)?(?:-[A-Z])?"


class SolasParser(NumberedParser):
    """IMO style: Chapter II-2 > Regulation 13 > 3.1.1  =>  II-2/13.3.1"""

    name = "solas"
    CHAPTER = re.compile(rf"^chapter\s+(?P<chap>{_ROMAN_CHAP})\b\.?\s*(?P<t>.*)$", re.I)
    REG = re.compile(
        rf"^(?:regulation|reg\.)\s*(?:(?P<chap>{_ROMAN_CHAP})/)?(?P<reg>\d+(?:-\d+)?)\b\.?\s*(?P<t>.*)$", re.I
    )

    def reset(self, blocks):
        self.ctx = {}

    def match_context(self, line):
        m = self.CHAPTER.match(line)
        if m and _heading_ok(m.group("t")):
            self.ctx = {"chapter": m.group("chap").upper()}
            return self.ctx["chapter"], _clean_heading_title(m.group("t"))
        m = self.REG.match(line)
        if m and _heading_ok(m.group("t")):
            if m.group("chap"):
                self.ctx["chapter"] = m.group("chap").upper()
            self.ctx["reg"] = m.group("reg")
            return self._reg_prefix(), _clean_heading_title(m.group("t"))
        return None

    def _reg_prefix(self, ctx=None) -> str:
        ctx = ctx or self.ctx
        chap, reg = ctx.get("chapter"), ctx.get("reg")
        if reg is None:
            return chap or ""
        return f"{chap}/{reg}" if chap else f"Reg.{reg}"

    def _ctx_parent(self, cid):
        chap = self.ctx.get("chapter")
        return chap if chap and cid != chap else None

    def make_id(self, num):
        prefix = self._reg_prefix()
        return f"{prefix}.{num}" if prefix else num

    def context_id(self):
        return self._reg_prefix() or None

    def table_id(self, n):
        chap = self.ctx.get("chapter")
        return f"{chap} Table {n}" if chap else f"Table {n}"

    def find_refs(self, text, ctx):
        saved, self.ctx = self.ctx, ctx
        refs: list[str] = []
        masked = text

        def take(pattern, fn, flags=0):
            nonlocal masked
            for m in re.finditer(pattern, masked, flags):
                r = fn(m)
                if r:
                    refs.append(r)
            masked = re.sub(pattern, lambda m: " " * len(m.group(0)), masked, flags=flags)

        take(rf"\b({_ROMAN_CHAP})/(\d+(?:\.\d+)*){NOT_A_VALUE}", lambda m: f"{m.group(1).upper()}/{m.group(2)}")
        chap = ctx.get("chapter")
        take(
            rf"\bregulations?\s+(\d+(?:\.\d+)*){NOT_A_VALUE}",
            lambda m: f"{chap}/{m.group(1)}" if chap else f"Reg.{m.group(1)}",
            re.I,
        )
        take(r"\bTable\s+([A-Z]?\d+(?:\.\d+)*)", lambda m: self.table_id(m.group(1)))
        take(rf"\bparagraphs?\s+({NUM}){NOT_A_VALUE}", lambda m: self.make_id(m.group(1)), re.I)
        take(
            rf"\b(?:see|in|with|under|of|to|and|or)\s+(\d+(?:\.\d+)+){NOT_A_VALUE}",
            lambda m: self.make_id(m.group(1)),
            re.I,
        )
        self.ctx = saved
        return refs


# ---------------------------------------------------------------- class-society style

PTCH = re.compile(r"\b(?:Pt\.?\s*(?P<pt>\d+)\s*Ch\.?\s*(?P<ch>\d+)|Part\s+(?P<pt2>\d+)\s*,?\s*Chapter\s+(?P<ch2>\d+))", re.I)


class ClassParser(NumberedParser):
    """Class-society style: Pt.3 Ch.2 > Section 4 > [2.1.3]  =>  Pt.3 Ch.2 Sec.4 [2.1.3]"""

    name = "class"
    SECTION = re.compile(r"^(?:section|sec\.)\s*(?P<sec>\d+)\b\.?\s*(?P<t>.*)$", re.I)
    PART = re.compile(r"^part\s+(?P<pt>\d+)\b\.?\s*(?P<t>.*)$", re.I)
    CHAPTER = re.compile(r"^chapter\s+(?P<ch>\d+)\b\.?\s*(?P<t>.*)$", re.I)

    def reset(self, blocks):
        self.ctx = {}
        for b in blocks[:200]:  # Pt/Ch usually appear on the title page or running header
            m = PTCH.search(b.text)
            if m:
                self.ctx = {"pt": m.group("pt") or m.group("pt2"), "ch": m.group("ch") or m.group("ch2")}
                break

    def match_context(self, line):
        for rx, key in ((self.PART, "pt"), (self.CHAPTER, "ch"), (self.SECTION, "sec")):
            m = rx.match(line)
            if m and _heading_ok(m.group("t")):
                self.ctx[key] = m.group(key)
                if key == "pt":
                    self.ctx.pop("ch", None)
                if key in ("pt", "ch"):
                    self.ctx.pop("sec", None)
                return self._prefix(), _clean_heading_title(m.group("t"))
        return None

    def _prefix(self, ctx=None) -> str:
        ctx = ctx or self.ctx
        parts = []
        if ctx.get("pt"):
            parts.append(f"Pt.{ctx['pt']}")
        if ctx.get("ch"):
            parts.append(f"Ch.{ctx['ch']}")
        if ctx.get("sec"):
            parts.append(f"Sec.{ctx['sec']}")
        return " ".join(parts)

    def make_id(self, num):
        p = self._prefix()
        return f"{p} [{num}]" if p else f"[{num}]"

    def context_id(self):
        return self._prefix() or None

    def table_id(self, n):
        p = self._prefix()
        return f"{p} Table {n}" if p else f"Table {n}"

    def find_refs(self, text, ctx):
        saved, self.ctx = self.ctx, ctx
        refs: list[str] = []
        masked = text

        def take(pattern, fn, flags=0):
            nonlocal masked
            for m in re.finditer(pattern, masked, flags):
                refs.append(fn(m))
            masked = re.sub(pattern, lambda m: " " * len(m.group(0)), masked, flags=flags)

        def fmt(pt, ch, sec, num):
            c = {"pt": pt, "ch": ch, "sec": sec}
            return f"{self._prefix(c)} [{num}]" if num else self._prefix(c)

        take(
            rf"Pt\.?\s*(\d+)\s*Ch\.?\s*(\d+)\s*Sec\.?\s*(\d+)(?:\s*\[({NUM})\])?",
            lambda m: fmt(m.group(1), m.group(2), m.group(3), m.group(4)),
        )
        take(
            rf"\bSec(?:\.|tion)?\s*(\d+)(?:\s*\[({NUM})\])?",
            lambda m: fmt(ctx.get("pt"), ctx.get("ch"), m.group(1), m.group(2)),
        )
        take(r"\bTable\s+(\d+(?:\.\d+)*)", lambda m: self.table_id(m.group(1)))
        take(rf"\[({NUM})\]", lambda m: self.make_id(m.group(1)))
        self.ctx = saved
        return refs


# ---------------------------------------------------------------- fallback


class FallbackParser:
    """No usable numbering: split on headings, then on size. IDs are S1, S2, S2.1 ..."""

    name = "fallback"
    MAX_CHARS = 1500

    def parse(self, blocks: list[Block]) -> list[Draft]:
        drafts: list[Draft] = []
        cur: Draft | None = None
        n = 0
        tables = 0
        size = 0
        part = 0

        for b in blocks:
            if b.kind == "table":
                tables += 1
                owner = cur.clause_id if cur else "doc"
                t = Draft(f"{owner} table {tables}", cur.title if cur else "", b.page, "table", owner, {})
                t.add(b.text)
                drafts.append(t)
                continue
            line = b.text.strip()
            is_head = b.heading_level is not None or (
                line.isupper() and 3 <= len(line) <= 80 and not line.endswith(".")
            )
            if is_head or cur is None:
                n += 1
                part, size = 0, 0
                cur = Draft(f"S{n}", line if is_head else "", b.page, "section", None, {})
                drafts.append(cur)
                if is_head:
                    continue
            if size + len(line) > self.MAX_CHARS and cur.lines:
                part += 1
                size = 0
                base = f"S{n}"
                cur = Draft(f"{base}.{part}", cur.title, b.page, "section", base, {})
                drafts.append(cur)
            cur.add(line)
            size += len(line)
        return drafts

    def find_refs(self, text: str, ctx: dict) -> list[str]:
        return []


class CfrParser:
    """US Code of Federal Regulations (e.g. 46 CFR Part 190, as written by scripts/fetch_cfr.py).

    "Subpart 190.10—Means of Escape" -> heading 190.10
    "§ 190.10-25 Stairway size."     -> section 190.10-25 (heading if it has lettered paragraphs)
    "(b) All interior stairways ..."  -> clause 190.10-25(b); numbered sub-items (1), (i) stay inside it
    """

    name = "cfr"
    PART = re.compile(r"^(?:\d+\s+CFR\s+)?Part\s+(?P<n>\d+)\b\s*[-—–:]?\s*(?P<t>.*)$", re.I)
    SUBPART = re.compile(r"^Subpart\s+(?P<n>\d+\.\d+|[A-Z])\s*[-—–:]?\s*(?P<t>.*)$")
    SECTION = re.compile(r"^§\s*(?P<n>\d+\.\d+(?:-\d+)?)\s+(?P<t>.*)$")
    PARA = re.compile(r"^\((?P<p>[a-z])\)\s*(?P<rest>.*)$")
    REF = re.compile(r"§§?\s*(\d+\.\d+(?:-\d+)?)(\([a-z]\))?")
    LOCAL = re.compile(r"paragraphs?\s+\(([a-z])\)(?:\(\d+\))?\s+of\s+this\s+section", re.I)

    def parse(self, blocks: list[Block]) -> list[Draft]:
        drafts: list[Draft] = []
        part = subpart = section = None
        cur: Draft | None = None
        tables = 0

        def start(cid, title, page, kind, parent, ctx) -> Draft:
            d = Draft(cid, title, page, kind, parent, ctx)
            drafts.append(d)
            return d

        for b in blocks:
            if b.kind == "table":
                tables += 1
                owner = cur.clause_id if cur else "doc"
                start(f"{owner} table {tables}", cur.title if cur else "", b.page, "table", owner, {}).add(b.text)
                continue
            line = b.text.strip()
            if m := self.SUBPART.match(line):
                subpart, section = m.group("n"), None
                cur = start(subpart, m.group("t").strip(" ."), b.page, "heading", part, {})
                continue
            if m := self.SECTION.match(line):
                section = m.group("n")
                cur = start(section, m.group("t").strip(" ."), b.page, "heading", subpart or part, {"section": section})
                continue
            if section and (m := self.PARA.match(line)):
                cur = start(f"{section}({m.group('p')})", "", b.page, "clause", section, {"section": section})
                cur.add(line)
                continue
            if b.heading_level == 1 or (part is None and (m := self.PART.match(line))):
                m = self.PART.match(line)
                if m and part is None:
                    part = m.group("n")
                    cur = start(part, m.group("t").strip(" .—-") or line, b.page, "heading", None, {})
                    continue
            if cur is None:
                cur = start("preamble", "", b.page, "section", None, {})
            if cur.kind == "heading" and section and cur.clause_id == section:
                cur.kind = "clause"  # undivided section: its text is the clause
            cur.add(line)
        return drafts

    def find_refs(self, text: str, ctx: dict) -> list[str]:
        refs = [m.group(1) + (m.group(2) or "") for m in self.REF.finditer(text)]
        if sec := ctx.get("section"):
            refs += [f"{sec}({m.group(1)})" for m in self.LOCAL.finditer(text)]
        return refs


class AdaParser:
    """ADA / ABA accessibility standards (36 CFR 1191 App. D): three-digit sections per chapter.

    "403 Walking Surfaces"            -> heading 403
    "403.5.1 Clear Width. The ..."    -> clause 403.5.1 (parent 403.5, then 403)
    "Exception: ..." and numbered exception items stay inside the clause above them.
    """

    name = "ada"
    LINE = re.compile(r"^(?P<num>[1-9]\d{2,3}(?:\.\d+){0,5})\s+(?P<rest>[A-Z].*)$")
    REF = re.compile(rf"\b(?:comply with|complying with|in|of|and|or|see)\s+([1-9]\d{{2,3}}(?:\.\d+)*){NOT_A_VALUE}")

    def parse(self, blocks: list[Block]) -> list[Draft]:
        drafts: list[Draft] = []
        cur: Draft | None = None
        tables = 0
        for b in blocks:
            if b.kind == "table":
                tables += 1
                owner = cur.clause_id if cur else "doc"
                if cur is not None and cur.kind == "table" and not any(l.startswith("|") for l in cur.lines):
                    cur.add(b.text)
                    continue
                t = Draft(f"{owner} table {tables}", cur.title if cur else "", b.page, "table", owner, {})
                t.add(b.text)
                drafts.append(t)
                continue
            line = b.text.strip()
            cap = CAPTION.match(line)
            if cap:
                cur = Draft(f"Table {cap.group('n')}", cap.group("title").strip(), b.page, "table",
                            cur.clause_id if cur else None, {})
                cur.add(line)
                drafts.append(cur)
                continue
            m = self.LINE.match(line)
            if m:
                num, rest = m.group("num"), m.group("rest")
                parent = num.rsplit(".", 1)[0] if "." in num else None
                title_m = re.match(r"^([^.]{2,80})\.\s*(.*)$", rest)
                if "." not in num and not title_m:  # "403 Walking Surfaces"
                    cur = Draft(num, rest.strip(), b.page, "heading", None, {})
                else:
                    title = title_m.group(1) if title_m else ""
                    cur = Draft(num, title.strip(), b.page, "clause", parent, {})
                    cur.add(line)
                drafts.append(cur)
                continue
            if cur is None:
                cur = Draft("preamble", "", b.page, "section", None, {})
                drafts.append(cur)
            if b.heading_level is not None and not cur.lines and cur.kind == "heading":
                continue
            cur.add(line)
        return drafts

    def find_refs(self, text: str, ctx: dict) -> list[str]:
        return [m.group(1) for m in self.REF.finditer(text)] + [
            f"Table {m.group(1)}" for m in re.finditer(r"\bTable\s+(\d{3}(?:\.\d+)*)", text)]


class MlcParser:
    """Maritime Labour Convention, 2006 (as written by scripts/fetch_mlc.py).

    "Article V"                         -> Art.V,   paragraphs Art.V/1
    "Regulation 3.1 – Accommodation..." -> R3.1,    paragraphs R3.1/1
    "Standard A3.1 – ..."               -> A3.1,    paragraphs A3.1/6, lettered items A3.1/6(a)
    "Guideline B3.1.1 – ..."            -> B3.1.1,  paragraphs B3.1.1/1, B3.1.1/1(a)
    Standards hang under their Regulation, Guidelines under the Standard of the same number, so a cited
    paragraph shows its whole context. Roman sub-items (i), (ii) stay inside the lettered item.
    "Title 5. ..." -> Title 5 (with its introduction, paragraphs Title 5/1); Appendices -> App.A5-I.
    """

    name = "mlc"
    HEAD = re.compile(r"^(?:(?P<art>Article)\s+(?P<an>[IVX]+)|(?P<kind>Regulation|Standard|Guideline)\s+"
                      r"(?P<n>[AB]?\d+(?:\.\d+){1,3})\s*[–—-]\s*(?P<t>.+)|(?P<app>Appendix)\s+(?P<pn>[AB]\d-[IVX]+)\s*(?P<pt>.*)"
                      r"|(?P<pre>Preamble)|(?P<note>Explanatory note to the Regulations and Code))$")
    PARA = re.compile(r"^(?P<n>\d{1,2})\.\s+(?P<rest>.*)$")
    ITEM = re.compile(r"^\((?P<l>[a-z])\)\s+(?P<rest>.*)$")
    REF = re.compile(r"\b(?:(Regulation)\s+(\d+(?:\.\d+){1,2})|(Standard)\s+(A\d+(?:\.\d+){1,2})|(Guideline)\s+(B\d+(?:\.\d+){1,3})"
                     r"|(Article)\s+([IVX]+)\b)(?:,?\s+paragraphs?\s+(\d{1,2})(?:\s*\(([a-z])\))?)?")
    LOCAL = re.compile(r"\bparagraphs?\s+(\d{1,2})(?:\s*\(([a-z])\))?\s+of\s+this\s+(Standard|Guideline|Article|Regulation)")

    def parse(self, blocks: list[Block]) -> list[Draft]:
        drafts: list[Draft] = []
        unit: Draft | None = None   # current Article / Regulation / Standard / Guideline / Appendix
        last_letter = ""
        para: Draft | None = None
        cur: Draft | None = None
        for b in blocks:
            line = b.text.strip()
            if b.heading_level == 1:  # document title, or "Title 5. ...": its own short introduction, if any
                if t := re.match(r"^Title\s+(\d)\b\.?\s*(.*)$", line):
                    unit = Draft(f"Title {t.group(1)}", t.group(2).strip(), b.page, "heading", None, {"unit": f"Title {t.group(1)}"})
                    drafts.append(unit)
                    para = cur = None
                    last_letter = ""
                continue
            if m := self.HEAD.match(line):
                if m.group("art"):
                    cid, title, parent = f"Art.{m.group('an')}", "", None
                elif m.group("kind"):
                    n = m.group("n")
                    cid = ("R" + n) if m.group("kind") == "Regulation" else n
                    parent = None if m.group("kind") == "Regulation" else (
                        "R" + n[1:] if m.group("kind") == "Standard" else "A" + ".".join(n[1:].split(".")[:2]))
                    last_letter = ""
                    title = m.group("t").strip()
                elif m.group("app"):
                    cid, title, parent = f"App.{m.group('pn')}", m.group("pt").strip(" –-"), None
                elif m.group("pre"):
                    cid, title, parent = "preamble", "Preamble", None
                else:
                    cid, title, parent = "Note", "Explanatory note to the Regulations and Code", None
                unit = Draft(cid, title, b.page, "heading", parent, {"unit": cid})
                last_letter = ""
                drafts.append(unit)
                para = cur = None
                continue
            if unit is None:
                continue
            if (m := self.PARA.match(line)) and not unit.clause_id.startswith("App."):
                last_letter = ""
                para = cur = Draft(f"{unit.clause_id}/{m.group('n')}", unit.title, b.page, "clause", unit.clause_id,
                                   {"unit": unit.clause_id})
                cur.add(line)
                drafts.append(cur)
                continue
            m = self.ITEM.match(line)
            if m and m.group("l") in "ivx" and chr(ord(m.group("l")) - 1) != last_letter:
                m = None  # roman numeral (i), (v), (x) of a sub-list, not the next letter: stays in the item
            if para is not None and m:
                last_letter = m.group("l")
                cur = Draft(f"{para.clause_id}({m.group('l')})", unit.title, b.page, "clause", para.clause_id,
                            {"unit": unit.clause_id})
                cur.add(line)
                drafts.append(cur)
                continue
            if cur is None:  # text right under a heading ("Purpose: ...", an unnumbered Article, an Appendix)
                cur = unit
                unit.kind = "section" if unit.clause_id.startswith(("App.", "R", "preamble", "Note")) else "clause"
            elif cur is not para and para is not None and re.match(r"^\(?[ivx]+\)", line) is None and \
                    cur.clause_id.endswith(")") and not line[:1].islower():
                cur = para  # text after the lettered list belongs to the paragraph again
            cur.add(line)
        return drafts

    def find_refs(self, text: str, ctx: dict) -> list[str]:
        refs = []
        for m in self.REF.finditer(text):
            if m.group(1):
                base = "R" + m.group(2)
            elif m.group(3):
                base = m.group(4)
            elif m.group(5):
                base = m.group(6)
            else:
                base = "Art." + m.group(8)
            refs.append(base + (f"/{m.group(9)}" if m.group(9) else "") + (f"({m.group(10)})" if m.group(10) else ""))
        unit = ctx.get("unit", "")
        for m in self.LOCAL.finditer(text):
            if unit:
                refs.append(f"{unit}/{m.group(1)}" + (f"({m.group(2)})" if m.group(2) else ""))
        return refs


# ---------------------------------------------------------------- style detection

PARSERS = {p.name: p for p in (SolasParser, ClassParser, NumberedParser, CfrParser, AdaParser, MlcParser, FallbackParser)}


def detect_style(blocks: list[Block]) -> str:
    lines = [b.text.strip() for b in blocks if b.kind == "text"]
    if sum(1 for l in lines if re.match(r"^Standard\s+A\d\.\d\s*[–—-]", l)) >= 3:
        return "mlc"
    if sum(1 for l in lines if CfrParser.SECTION.match(l)) >= 3:
        return "cfr"
    if sum(1 for l in lines if re.match(r"^[1-9]\d{2}\.\d+(?:\.\d+)*\s+[A-Z][^.]{2,60}\.", l)) >= 20:
        return "ada"
    regs = sum(1 for l in lines if (m := SolasParser.REG.match(l)) and _heading_ok(m.group("t")))
    secs = sum(1 for l in lines if (m := ClassParser.SECTION.match(l)) and _heading_ok(m.group("t")))
    ptch = any(PTCH.search(l) for l in lines[:300])
    numbered = sum(1 for l in lines if (m := CLAUSE_LINE.match(l)) and m.group("rest") and m.group("rest")[0].isupper())
    if regs >= 1 and numbered >= 3 and regs >= secs:
        return "solas"
    if secs >= 1 and numbered >= 3 and (ptch or secs > regs):
        return "class"
    if numbered >= 5:
        return "decimal"
    return "fallback"


def pick_parser(blocks: list[Block], style: str | None = None):
    style = style or detect_style(blocks)
    return PARSERS[style]()
