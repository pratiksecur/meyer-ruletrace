"""LLM provider adapter: one interface for Groq, any OpenAI-compatible endpoint, and Ollama.

LLM_PROVIDER may list several providers in order of preference, e.g. "groq,ollama":
if the first one fails or times out, the next is tried. Keys come from .env only.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import httpx

from . import config  # noqa: F401  (loads .env)


RATE_LIMIT_WAIT = float(os.getenv("LLM_RATE_LIMIT_WAIT", "30"))


def _retry_after(r: httpx.Response) -> float:
    """Seconds to wait from a 429: Retry-After header, or "try again in 1.5s" / "in 2m3.4s" in the message."""
    try:
        return max(0.5, float(r.headers.get("retry-after", "")))
    except ValueError:
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", r.text)
    if m:
        secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1) + 60 * int(m.group(1) or 0)
        return max(0.5, secs + 0.2)
    return 5.0


class LLMError(RuntimeError):
    pass


@dataclass
class OpenAICompatible:
    base_url: str
    api_key: str
    model: str
    name: str = "openai"
    timeout: float = 25.0
    reasoning_effort: str | None = None

    def complete(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 900) -> str:
        if not self.api_key:
            raise LLMError(f"{self.name}: no API key set in .env")
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if self.reasoning_effort and "gpt-oss" in self.model:
            body["reasoning_effort"] = self.reasoning_effort
        waited = 0.0
        while True:
            try:
                r = httpx.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as e:
                raise LLMError(f"{self.name}: {type(e).__name__}: {e}") from e
            if r.status_code not in (429, 503):  # rate limit / temporarily overloaded: wait and retry
                break
            if r.status_code == 429 and ("PerDay" in r.text or ("quota" in r.text.lower() and "retry" not in r.text.lower())):
                break  # daily quota used up (Gemini free tier: 20 requests/day/model) - waiting will not help
            # rate limited (free tiers cap tokens per minute): wait as told, up to LLM_RATE_LIMIT_WAIT seconds
            # in total, then fail so a chained provider (e.g. ollama) can take over
            wait = _retry_after(r)
            if waited + wait > RATE_LIMIT_WAIT:
                break
            time.sleep(wait)
            waited += wait
        if r.status_code != 200:
            raise LLMError(f"{self.name}: HTTP {r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"].get("content") or ""
        if not content.strip():
            raise LLMError(f"{self.name}: empty response")
        return content.strip()

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"


@dataclass
class Ollama:
    base_url: str
    model: str
    name: str = "ollama"
    timeout: float = 60.0

    def _resolve_model(self) -> str:
        """'llama3.2' -> an installed tag such as 'llama3.2:3b'."""
        try:
            tags = httpx.get(f"{self.base_url}/api/tags", timeout=3).json().get("models", [])
        except httpx.HTTPError:
            return self.model
        names = [t["name"] for t in tags]
        if self.model in names:
            return self.model
        return next((n for n in names if n.split(":")[0] == self.model.split(":")[0]), self.model)

    def complete(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 900) -> str:
        model = self._resolve_model()
        try:
            r = httpx.post(
                f"{self.base_url.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"ollama: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"ollama: HTTP {r.status_code}: {r.text[:300]}")
        content = r.json().get("message", {}).get("content", "")
        if not content.strip():
            raise LLMError("ollama: empty response")
        return content.strip()

    @property
    def label(self) -> str:
        return f"ollama:{self.model}"


@dataclass
class ChainLLM:
    """Try providers in order; the first that answers wins."""

    providers: list
    last_used: str = ""

    def complete(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 900) -> str:
        errors = []
        for p in self.providers:
            try:
                out = p.complete(messages, temperature=temperature, max_tokens=max_tokens)
                self.last_used = p.label
                return out
            except LLMError as e:
                errors.append(str(e))
        raise LLMError(" | ".join(errors) or "no LLM provider configured")

    @property
    def label(self) -> str:
        return self.last_used or "+".join(p.label for p in self.providers)


def make_provider(name: str):
    name = name.strip().lower()
    timeout = float(os.getenv("LLM_TIMEOUT", "25"))
    effort = os.getenv("LLM_REASONING_EFFORT", "low") or None
    if name == "groq":
        return OpenAICompatible(
            base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            api_key=os.getenv("GROQ_API_KEY", ""),
            model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            name="groq",
            timeout=timeout,
            reasoning_effort=effort,
        )
    if name == "openai":
        return OpenAICompatible(
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            name="openai",
            timeout=timeout,
            reasoning_effort=effort,
        )
    if name == "gemini":
        # Google Gemini through its OpenAI-compatible endpoint (text and images); key from Google AI Studio
        return OpenAICompatible(
            base_url=os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
            api_key=os.getenv("GEMINI_API_KEY", ""),
            model=os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
            name="gemini",
            timeout=float(os.getenv("GEMINI_TIMEOUT", str(timeout))),
        )
    if name == "ollama":
        return Ollama(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.getenv("OLLAMA_MODEL", "llama3.2"),
            timeout=float(os.getenv("OLLAMA_TIMEOUT", "90")),
        )
    raise ValueError(f"Unknown LLM provider '{name}' (use groq, gemini, openai or ollama)")


def get_llm(spec: str | None = None) -> ChainLLM:
    spec = spec or os.getenv("LLM_PROVIDER", "groq,ollama")
    return ChainLLM([make_provider(n) for n in spec.split(",") if n.strip()])
