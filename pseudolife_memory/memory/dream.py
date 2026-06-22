"""Pluggable dream extractors — turn recent memory text into cortex claims.

A dream consolidates the recent associative stream into canonical
``(entity, attribute, value)`` facts. The *extraction* step is pluggable:
the ``OpenAICompatExtractor`` (an OpenAI-compatible LLM) is the cortex writer;
``NoOpExtractor`` is the default when none is configured (single-writer cortex:
the LLM dream is the sole *automatic* writer, so no extractor means no automatic
cortex writes). ``RegexExtractor`` remains as an explicit opt-in only — it is
never selected automatically (the store-path auto-promote and the old
``dream_run`` regex fallback are both gone). The shared driver lives in
``MemoryService.dream_run`` so cursor discipline lives in one place.
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


class LessonClaim(TypedDict):
    task: str            # the task-type ("deploy engine to host")
    aspect: str          # approach | pitfall | tool-choice | correction
    lesson: str          # the actionable takeaway
    about: str           # the tool/source/approach the lesson concerns
    polarity: str        # "+" do-this | "-" avoid (dead end)
    outcome: str         # success | failure | correction
    confidence: float


class RelationClaim(TypedDict):
    src: str
    relation: str
    dst: str
    confidence: float


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


class NoOpExtractor:
    """No-LLM, no-write floor. Returns no claims, so a dream with no configured
    extractor writes nothing to the cortex. Single-writer cortex: the LLM dream
    is the sole *automatic* writer of canonical facts; the regex (``extract_slots``)
    is for the recall-time slot-view only, and ``RegexExtractor`` is an explicit
    opt-in, never reached automatically."""

    def extract(self, texts: list[str], vocab: list[str]) -> list[Claim]:
        return []


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


_LESSON_SYSTEM_PROMPT = (
    "You consolidate an agent's work-outcome signals into reusable LESSONS. Each "
    "signal records something that happened while doing a task: a success, a "
    "failure/dead-end, or a user correction. Produce durable, actionable lessons "
    'as JSON: {"lessons":[{"task":..,"aspect":..,"lesson":..,"about":..,'
    '"polarity":"+"|"-","outcome":"success"|"failure"|"correction",'
    '"confidence":0..1}]}.\n'
    "- task = the kind of task, reusing stable wording across signals.\n"
    "- aspect = approach | pitfall | tool-choice | correction.\n"
    "- lesson = the actionable takeaway, phrased as what to DO (or what to avoid).\n"
    "- about = the tool/source/approach the lesson concerns.\n"
    "- outcome = the signal class it came from.\n"
    '- polarity = "+" when the lesson is something to DO — an approach that worked, '
    'or the corrected, now-correct way; "-" ONLY when the lesson is something to '
    'AVOID (a dead-end), phrased as "avoid X". A CORRECTION is almost always "+": '
    "state the new correct behavior to follow, never the mistake.\n"
    "Cluster related signals into one lesson. SKIP trivial or non-durable signals "
    "— generic knowledge any competent agent already has (e.g. basic "
    "language/library usage), one-off chatter, or anything a future run would not "
    'benefit from recalling. Return {"lessons":[]} if nothing qualifies.'
)


_RELATIONS_PROMPT_HEAD = (
    "You extract durable RELATIONSHIPS between named entities from notes, as "
    'JSON: {"relations":[{"src":..,"relation":..,"dst":..}]}. Use ONLY these '
    "relation names:\n"
)
_RELATIONS_PROMPT_TAIL = (
    "\nIf a real connection fits none of the specific ones, use 'related-to'. "
    "src and dst are entity names (services, hosts, tools, components). Skip "
    "opinions, chit-chat, and anything with no entity-to-entity relationship. "
    'Return {"relations":[]} if nothing qualifies.'
)


def _relations_prompt(relations: list[tuple[str, str]]) -> str:
    body = "\n".join(f"- {n}: {d}" for n, d in relations)
    return _RELATIONS_PROMPT_HEAD + body + _RELATIONS_PROMPT_TAIL


def _format_signals(signals: list[dict]) -> str:
    """Render outcome signals as compact lines for the synthesis prompt."""
    lines = []
    for s in signals or []:
        parts = [f"[{s.get('outcome', '?')}]", f"task={s.get('task', '')!r}"]
        if s.get("about"):
            parts.append(f"about={s['about']!r}")
        if s.get("detail"):
            parts.append(f"detail={s['detail']!r}")
        if s.get("polarity"):
            parts.append(f"polarity={s['polarity']}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


class ExtractorError(Exception):
    """An extractor call failed (network, timeout, HTTP error, malformed
    response) — as opposed to succeeding with zero claims. Callers use this to
    distinguish a transient failure (don't advance the dream cursor / leave
    signals pending, retry next sweep) from a genuine empty result."""


class OpenAICompatExtractor:
    """Tier 2 — extract claims via any OpenAI-compatible ``/chat/completions``
    endpoint (Ollama, LM Studio, Anthropic/Haiku, OpenRouter, a self-hosted
    model — all the same slot). Bounded by ``max_tokens`` + a hard timeout. On
    failure (network, timeout, malformed JSON) it **raises** :class:`ExtractorError`
    so the caller can tell failure from a genuine empty result and avoid skipping
    memories (advancing the cursor) on a transient blip. A successful call with no
    extractable claims returns ``[]``. Uses stdlib urllib — no new deps."""

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
                # Reasoning models (Qwen3, etc.) otherwise spend the entire
                # token budget on a <think> trace and return EMPTY content, so
                # extraction yields nothing and the cortex gets no write this
                # cycle. Templates that don't define this kwarg (e.g. Gemma)
                # just ignore it.
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
            # Chatty/reasoning models often wrap the object in ```json fences or
            # emit leading prose; parse the outermost {...} object.
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
            raw = parsed.get("claims", []) if isinstance(parsed, dict) else []
        except Exception as exc:  # noqa: BLE001
            # Signal failure (vs genuine empty) so the dream doesn't advance its
            # cursor past these memories on a transient timeout/network blip.
            raise ExtractorError(f"extract failed: {exc}") from exc
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

    def extract_lessons(self, signals: list[dict]) -> list[LessonClaim]:
        """Synthesise procedural lessons from outcome signals via the same
        endpoint. Returns ``[]`` on any failure (single-writer: the dream then
        writes no lessons this cycle and the signals stay pending)."""
        import json
        import urllib.request

        if not signals:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _LESSON_SYSTEM_PROMPT},
                    {"role": "user", "content": _format_signals(signals)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
            raw = parsed.get("lessons", []) if isinstance(parsed, dict) else []
        except Exception as exc:  # noqa: BLE001
            # Raise (vs return []) so synthesize_lessons leaves the signals
            # pending and retries, rather than consuming them on a failed call.
            raise ExtractorError(f"extract_lessons failed: {exc}") from exc
        out: list[LessonClaim] = []
        for c in raw if isinstance(raw, list) else []:
            if not isinstance(c, dict):
                continue
            task = str(c.get("task", "")).strip()
            lesson = str(c.get("lesson", "")).strip()
            if not (task and lesson):
                continue
            aspect = str(c.get("aspect", "") or "lesson").strip() or "lesson"
            about = str(c.get("about", "") or "").strip() or None
            polarity = "-" if str(c.get("polarity", "+")).strip() == "-" else "+"
            outcome = str(c.get("outcome", "success")).strip()
            if outcome not in ("success", "failure", "correction"):
                outcome = "success"
            try:
                conf = max(0.0, min(1.0, float(c.get("confidence", 0.6))))
            except (TypeError, ValueError):
                conf = 0.6
            out.append(LessonClaim(
                task=task, aspect=aspect, lesson=lesson, about=about,
                polarity=polarity, outcome=outcome, confidence=conf))
        return out

    def extract_relations(self, texts: list[str],
                          relations: list[tuple[str, str]]) -> list[RelationClaim]:
        """Extract (src, relation, dst) triples from ``texts`` via the same
        endpoint. ``relations`` are (name, description) pairs seeding the closed
        vocabulary. Raises ExtractorError on failure (vs a genuine empty [])."""
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
                    {"role": "system", "content": _relations_prompt(relations)},
                    {"role": "user", "content": "\n\n".join(texts)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
            raw = parsed.get("relations", []) if isinstance(parsed, dict) else []
        except Exception as exc:  # noqa: BLE001
            raise ExtractorError(f"extract_relations failed: {exc}") from exc
        out: list[RelationClaim] = []
        for r in raw if isinstance(raw, list) else []:
            if not isinstance(r, dict):
                continue
            src = str(r.get("src", "")).strip()
            rel = str(r.get("relation", "")).strip()
            dst = str(r.get("dst", "")).strip()
            if not (src and rel and dst):
                continue
            try:
                conf = max(0.0, min(1.0, float(r.get("confidence", 0.6))))
            except (TypeError, ValueError):
                conf = 0.6
            out.append(RelationClaim(src=src, relation=rel, dst=dst,
                                     confidence=conf))
        return out


def build_extractor(cfg) -> DreamExtractor:
    """Pick the extractor from config: an OpenAI-compatible endpoint when a
    base-URL + model are set (env vars ``PSEUDOLIFE_DREAM_BASE_URL`` /
    ``_MODEL`` / ``_API_KEY`` / ``_TIMEOUT_SECONDS`` / ``_MAX_TOKENS`` override
    the dataclass), else a no-op (no automatic regex writes — single-writer
    cortex; see the 2026-06-19 design)."""
    import os

    def _env_num(name, fallback, cast):
        raw = os.environ.get(name)
        if not raw:
            return fallback
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return fallback

    base_url = os.environ.get("PSEUDOLIFE_DREAM_BASE_URL") or cfg.extractor_base_url
    model = os.environ.get("PSEUDOLIFE_DREAM_MODEL") or cfg.extractor_model
    api_key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or cfg.extractor_api_key
    if base_url and model:
        return OpenAICompatExtractor(
            base_url, model, api_key=api_key,
            max_tokens=_env_num("PSEUDOLIFE_DREAM_MAX_TOKENS",
                                 cfg.extractor_max_tokens, int),
            timeout_seconds=_env_num("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS",
                                     cfg.extractor_timeout_seconds, float),
        )
    return NoOpExtractor()


def run_sweep_once(service) -> dict:
    """One headless sweep tick: if dreaming is enabled and the backlog+quiescence
    trigger would fire, run a dream with the configured extractor. Session-
    agnostic by construction (it keys on the cursor, not on session lifecycle).
    Returns ``{"fired": bool, ...}``; never raises into the daemon's timer."""
    cfg = service.config.memory.dream
    if not cfg.enabled:
        return {"fired": False, "reason": "disabled"}
    status = service.dream_status()
    if not status["would_fire"]:
        return {"fired": False, "reason": "below_threshold",
                "backlog": status["backlog"]}
    result = service.dream_run(build_extractor(cfg))
    logger.info("dream sweep fired: %s", result)
    return {"fired": True, **result}
