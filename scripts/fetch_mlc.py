"""Download the Maritime Labour Convention, 2006 (as amended) from the ILO into the rule folder as Markdown.

    python scripts/fetch_mlc.py                    # -> data/rules/MLC2006.md (RuleMate)
    python scripts/fetch_mlc.py --out ruletrace/copilot_rules/MLC2006.md
    python scripts/fetch_mlc.py --pdf mlc.pdf      # from a PDF you already have

The ILO's text is not redistributed with this repository (the output file is git-ignored).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rulemate.ingest.mlc_fetch import URL, fetch  # noqa: E402

DEFAULT = (ROOT / "ruletrace" / "copilot_rules" if (ROOT / "ruletrace" / "copilot_rules").is_dir()
           else ROOT / "data" / "rules") / "MLC2006.md"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT))
    ap.add_argument("--pdf", help="convert this PDF instead of downloading it")
    a = ap.parse_args()
    if not a.pdf:
        print(f"Downloading {URL}")
    print("Wrote", fetch(Path(a.out), Path(a.pdf) if a.pdf else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
