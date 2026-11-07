"""Settings read from the environment / .env. No secrets are hard-coded."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(var: str, default: str) -> Path:
    p = Path(os.getenv(var, default))
    return p if p.is_absolute() else ROOT / p


RULES_DIR = _path("RULES_DIR", "data/rules")
INDEX_DIR = _path("INDEX_DIR", "data/index")
CLAUSES_FILE = INDEX_DIR / "clauses.json"
CACHE_DIR = _path("RULEMATE_CACHE_DIR", "data/cache")  # answers, explanations, OCR and Gemini readings
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".html", ".htm", ".txt", ".md"}

# Demo questions and the rule documents each one is asked against (UI sidebar + scripts/warm_demo.py).
REAL_US_RULES = ["46CFR190"]
SYNTHETIC_RULES = ["SAMPLE_SYNTHETIC_rules"]
DEMO_QUESTIONS: list[tuple[str, list[str]]] = [
    ("How high must guard rails be on a research vessel?", REAL_US_RULES),
    ("How is the corridor width calculated when many people use it?", SYNTHETIC_RULES),
    ("What does 46 CFR 190.10-30 say?", REAL_US_RULES),
    ("What is the required size of a helicopter landing area?", REAL_US_RULES),
]
