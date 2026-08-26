"""LLM explanation layer.

The detector says "window 47 scores 0.91". Nobody can act on that. This layer
turns a flagged window into a readable account of what appears to be wrong.

Design notes worth defending in an interview:

1. The LLM does NOT decide what is anomalous. Detection is statistical and
   reproducible; the LLM only explains an already-flagged window. Letting an LLM
   do the detection would be non-deterministic, unauditable and expensive.

2. Prompts include *contrast* - what this window contains versus what a normal
   window looks like. Without the contrast the model just paraphrases the logs.

3. Responses are cached by window content hash. Templates repeat, and you should
   never pay twice for the same explanation.

4. There is a stub backend so the whole pipeline runs and is testable with no
   API key. Never let a network dependency block your test suite.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .window import Window

SYSTEM_PROMPT = """You are a site reliability engineer triaging system logs.

You are given log templates from a window that an anomaly detector flagged, plus
templates from a typical normal window for contrast.

Respond with strict JSON and nothing else:
{
  "summary": "one sentence on what appears to be wrong",
  "severity": "low" | "medium" | "high",
  "evidence": ["specific template or pattern that supports this", ...],
  "likely_cause": "most plausible mechanism, or null if unclear",
  "suggested_action": "concrete next step for the on-call engineer"
}

Rules:
- Cite only templates actually present in the flagged window.
- If the window looks benign and the detector likely false-positived, say so in
  summary and set severity to "low". Do not invent a fault to justify the alert.
- No preamble, no markdown fences."""


@dataclass
class Explanation:
    summary: str
    severity: str
    evidence: list[str]
    likely_cause: str | None
    suggested_action: str
    backend: str
    cached: bool = False

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def build_prompt(window: Window, normal_templates: list[str], score: float) -> str:
    flagged = "\n".join(f"  - {t}" for t in dict.fromkeys(window.templates))
    normal = "\n".join(f"  - {t}" for t in list(dict.fromkeys(normal_templates))[:12])
    return (
        f"FLAGGED WINDOW (index {window.index}, anomaly score {score:.3f}, "
        f"{window.size} lines)\nDistinct templates:\n{flagged}\n\n"
        f"TYPICAL NORMAL WINDOW for contrast:\n{normal}\n"
    )


class LlmClient(ABC):
    name: str = "abstract"

    @abstractmethod
    def complete(self, system: str, user: str) -> str: ...


class StubClient(LlmClient):
    """Deterministic offline backend. Lets tests and demos run with no API key."""

    name = "stub"

    def complete(self, system: str, user: str) -> str:
        lines = [line.removeprefix("  - ") for line in user.splitlines() if line.startswith("  - ")]
        keywords = ("error", "fail", "fatal", "exception", "timeout", "corrupt", "abort")
        hits = [line for line in lines if any(k in line.lower() for k in keywords)][:3]
        return json.dumps(
            {
                "summary": (
                    f"Window contains {len(hits)} template(s) matching failure keywords."
                    if hits
                    else "No obvious failure keywords; may be a false positive."
                ),
                "severity": "high" if len(hits) >= 2 else "medium" if hits else "low",
                "evidence": hits,
                "likely_cause": None,
                "suggested_action": (
                    "Inspect the raw lines for this window."
                    if hits
                    else "Compare against the normal baseline before escalating."
                ),
            }
        )


class ApiClient(LlmClient):
    """Wire this to your provider. Keep the key in an env var, never in code."""

    name = "api"

    def __init__(self, model: str = "claude-sonnet-4-6", api_key_env: str = "ANTHROPIC_API_KEY"):
        self.model = model
        self.api_key = os.environ.get(api_key_env)

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError(
            "Implement with your provider SDK. Set max_tokens ~600, temperature 0 "
            "for reproducibility, retry on 429 with backoff, and validate the JSON "
            "before trusting it - models do occasionally return prose."
        )


class Explainer:
    def __init__(self, client: LlmClient | None = None):
        self.client = client or StubClient()
        self._cache: dict[str, Explanation] = {}

    @staticmethod
    def _key(window: Window) -> str:
        blob = "|".join(sorted(set(window.templates)))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def explain(self, window: Window, normal_templates: list[str], score: float) -> Explanation:
        key = self._key(window)
        if key in self._cache:
            hit = self._cache[key]
            return Explanation(**{**hit.as_dict(), "cached": True})

        prompt = build_prompt(window, normal_templates, score)
        raw = self.client.complete(SYSTEM_PROMPT, prompt)

        try:
            data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError:
            data = {
                "summary": "Model returned unparseable output.",
                "severity": "low",
                "evidence": [],
                "likely_cause": None,
                "suggested_action": "Check the raw model response and prompt.",
            }

        exp = Explanation(
            summary=data.get("summary", ""),
            severity=data.get("severity", "low"),
            evidence=data.get("evidence", []) or [],
            likely_cause=data.get("likely_cause"),
            suggested_action=data.get("suggested_action", ""),
            backend=self.client.name,
        )
        self._cache[key] = exp
        return exp

    @property
    def cache_size(self) -> int:
        return len(self._cache)
