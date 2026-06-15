"""Pluggable dream extractors — turn recent memory text into cortex claims.

A dream consolidates the recent associative stream into canonical
``(entity, attribute, value)`` facts. The *extraction* step is pluggable:
``RegexExtractor`` is the zero-dependency floor; an OpenAI-compatible LLM
extractor (Tier 2) lands in a later phase. The shared driver lives in
``MemoryService.dream_run`` so cursor discipline and fallback live in one place.
"""
from __future__ import annotations

import logging
from typing import Protocol, TypedDict

logger = logging.getLogger(__name__)


class Claim(TypedDict):
    entity: str
    attribute: str
    value: str
    confidence: float
    origin: str          # "user" | "action" | "agent"


class DreamExtractor(Protocol):
    def extract(self, texts: list[str], vocab: list[str]) -> list[Claim]:
        """Return canonical claims for ``texts``. ``vocab`` is the existing
        ``entity.attribute`` slot keys, so an extractor can REUSE them instead of
        reinventing variants. Must never raise — return ``[]`` on any failure."""
        ...


class RegexExtractor:
    """Deterministic no-LLM floor. Wraps ``slots.extract_slots`` (the one regex
    implementation) and shapes its output into ``Claim`` dicts."""

    def extract(self, texts: list[str], vocab: list[str]) -> list[Claim]:
        from pseudolife_memory.memory.slots import extract_slots
        claims: list[Claim] = []
        for t in texts or []:
            for s in extract_slots(t or ""):
                value = s.value if s.polarity != "-" else ("NOT " + s.value)
                claims.append(Claim(
                    entity=s.entity, attribute=s.attribute, value=value,
                    confidence=0.55, origin="agent",
                ))
        return claims


_SYSTEM_PROMPT = (
    "You consolidate notes into canonical facts. Extract durable, current-state "
    'facts as JSON: {"claims":[{"entity":..,"attribute":..,"value":..,'
    '"confidence":0..1}]}. One slot per real fact; skip narrative, opinions, and '
    'obsolete states. Reuse existing slot keys when they fit. Return {"claims":[]} '
    "if nothing qualifies."
)


def _vocab_hint(vocab: list[str]) -> str:
    if not vocab:
        return ""
    return "\n\nExisting slot keys (reuse if applicable): " + ", ".join(vocab[:60])


class OpenAICompatExtractor:
    """Tier 2 — extract claims via any OpenAI-compatible ``/chat/completions``
    endpoint (Ollama, LM Studio, Anthropic/Haiku, OpenRouter, a self-hosted
    model — all the same slot). Bounded by ``max_tokens`` + a hard timeout; on
    ANY failure (network, timeout, malformed JSON) returns ``[]`` so the dream
    driver falls back to the regex floor. Uses stdlib urllib — no new deps."""

    def __init__(self, base_url: str, model: str, *, api_key: str | None = None,
                 max_tokens: int = 400, timeout_seconds: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or None
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout_seconds)

    def extract(self, texts: list[str], vocab: list[str]) -> list[Claim]:
        import json
        import urllib.request

        texts = [t for t in (texts or []) if t]
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system",
                     "content": _SYSTEM_PROMPT + _vocab_hint(vocab)},
                    {"role": "user", "content": "\n\n".join(texts)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            raw = parsed.get("claims", []) if isinstance(parsed, dict) else []
        except Exception as exc:  # noqa: BLE001 — never break a dream
            logger.warning("OpenAICompatExtractor failed: %s", exc)
            return []
        claims: list[Claim] = []
        for c in raw if isinstance(raw, list) else []:
            if not isinstance(c, dict):
                continue
            entity = str(c.get("entity", "")).strip()
            attribute = str(c.get("attribute", "")).strip()
            value = str(c.get("value", "")).strip()
            if not (entity and attribute and value):
                continue
            try:
                conf = max(0.0, min(1.0, float(c.get("confidence", 0.7))))
            except (TypeError, ValueError):
                conf = 0.7
            claims.append(Claim(entity=entity, attribute=attribute, value=value,
                                confidence=conf, origin="agent"))
        return claims


def build_extractor(cfg) -> DreamExtractor:
    """Pick the extractor from config: an OpenAI-compatible endpoint when a
    base-URL + model are set (env vars ``PSEUDOLIFE_DREAM_BASE_URL`` /
    ``_MODEL`` / ``_API_KEY`` override the dataclass), else the regex floor."""
    import os

    base_url = os.environ.get("PSEUDOLIFE_DREAM_BASE_URL") or cfg.extractor_base_url
    model = os.environ.get("PSEUDOLIFE_DREAM_MODEL") or cfg.extractor_model
    api_key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or cfg.extractor_api_key
    if base_url and model:
        return OpenAICompatExtractor(
            base_url, model, api_key=api_key,
            max_tokens=cfg.extractor_max_tokens,
            timeout_seconds=cfg.extractor_timeout_seconds,
        )
    return RegexExtractor()
