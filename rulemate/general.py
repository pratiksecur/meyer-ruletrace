"""Answers to questions the loaded rule texts do not cover (general ship design, how-to, definitions...).

Kept strictly apart from rule answers: a general answer is never presented as a rule, carries no citations, and is
labelled "general knowledge - not from the loaded rules, not verified". It never gives a PASS / FAIL verdict and
tells the user which rule set would govern the question, so an engineer can confirm it there.
Only the bot providers are used (Groq, then the local Ollama model); Gemini is kept for reading drawings.
"""

from __future__ import annotations

import os

from .llm import LLMError, get_llm

LABEL = "General knowledge - not from the loaded rules, not verified"

PROMPT = """You are RuleMate, an assistant for ship designers (interior designers and engineers) at a shipyard.
The question below is NOT answered by the rule texts loaded in the tool, so answer from general knowledge.
- Answer clearly and briefly (at most 6 sentences or a short list), in plain language a non-expert understands.
- If the question is about a requirement, name the rule set that would normally govern it (e.g. SOLAS, MLC 2006,
  class rules, flag state rules) and say that the exact value must be confirmed there. Never state that a design
  passes or fails.
- If you are not sure, say so. Do not invent clause numbers, values or quotes.
- For questions unrelated to ships, answer normally and briefly."""

BOT_PROVIDERS = "groq,ollama"


def bot_provider() -> str:
    """Providers for the chat bot: Groq and Ollama only. Gemini is never used for answers."""
    spec = os.getenv("COPILOT_LLM_PROVIDER", BOT_PROVIDERS)
    names = [n.strip() for n in spec.split(",") if n.strip() and n.strip().lower() != "gemini"]
    return ",".join(names) or BOT_PROVIDERS


def general_answer(question: str, llm=None) -> dict:
    llm = llm or get_llm(bot_provider())
    try:
        text = llm.complete([{"role": "system", "content": PROMPT}, {"role": "user", "content": question}],
                            temperature=0.2, max_tokens=900).strip()
    except LLMError as e:
        return {"ok": False, "label": LABEL, "text": "", "model": "",
                "error": f"No bot model answered (Groq / Ollama): {str(e)[:160]}"}
    from .answer import strip_reasoning

    return {"ok": True, "label": LABEL, "text": strip_reasoning(text), "model": llm.label}
