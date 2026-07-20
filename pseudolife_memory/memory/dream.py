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


class _ClaimRequired(TypedDict):
    entity: str
    attribute: str
    value: str
    confidence: float
    origin: str          # "user" | "action" | "agent"


class Claim(_ClaimRequired, total=False):
    # 0-based index into the extract() texts batch this claim came from, for
    # per-claim source attribution (slot->episode traces). Absent when the
    # model didn't cite a note (or cited one out of range).
    source: int


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
    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
        """Return canonical claims for ``texts``. ``vocab`` is the existing
        ``entity.attribute`` slot keys, so an extractor can REUSE them instead of
        reinventing variants. ``known_facts`` (when the known-facts window is
        enabled) is ``(entity, attribute, current value)`` triples the batch
        plausibly updates — shown so updates land on the SAME slot. The caller
        only passes it when non-empty, so extractors without the parameter
        keep working on window-off deployments. Must never raise — return
        ``[]`` on any failure."""
        ...


class RegexExtractor:
    """Deterministic no-LLM floor. Wraps ``slots.extract_slots`` (the one regex
    implementation) and shapes its output into ``Claim`` dicts."""

    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
        from pseudolife_memory.memory.slots import extract_slots
        claims: list[Claim] = []
        for i, t in enumerate(texts or []):
            for s in extract_slots(t or ""):
                value = s.value if s.polarity != "-" else ("NOT " + s.value)
                claims.append(Claim(
                    entity=s.entity, attribute=s.attribute, value=value,
                    confidence=0.55, origin="agent", source=i,
                ))
        return claims


class NoOpExtractor:
    """No-LLM, no-write floor. Returns no claims, so a dream with no configured
    extractor writes nothing to the cortex. Single-writer cortex: the LLM dream
    is the sole *automatic* writer of canonical facts; the regex (``extract_slots``)
    is for the recall-time slot-view only, and ``RegexExtractor`` is an explicit
    opt-in, never reached automatically."""

    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
        return []


_SYSTEM_PROMPT = (
    "You consolidate numbered notes into canonical facts. Extract durable, "
    'current-state facts as JSON: {"claims":[{"entity":..,"attribute":..,'
    '"value":..,"confidence":0..1,"source":<number of the note the fact came '
    "from>}]}. One slot per real fact; skip narrative, opinions, and obsolete "
    "states. When several notes state or update the SAME fact, use one "
    "consistent entity and attribute for it and emit only the CURRENT value "
    "(source = the note stating it). Reuse existing slot keys when they fit. "
    "When a note quotes or summarizes a DOCUMENT (a spec, policy, protocol, "
    "runbook, or guide), what the document prescribes is itself a durable "
    "fact — extract it with entity = the document's subject, even when other "
    "notes show something different being done.\n"
    "Example. Notes: [1] we moved the deploy target from staging to prod-eu. "
    "[2] the release runbook says every release needs a signed tag. Output: "
    '{"claims":[{"entity":"deploy target","attribute":"environment",'
    '"value":"prod-eu","confidence":0.9,"source":1},'
    '{"entity":"releases","attribute":"documented requirement",'
    '"value":"signed tag (per release runbook)","confidence":0.8,'
    '"source":2}]}\n'
    'Return {"claims":[]} if nothing qualifies.'
)


def _vocab_hint(vocab: list[str]) -> str:
    if not vocab:
        return ""
    return "\n\nExisting slot keys (reuse if applicable): " + ", ".join(vocab[:60])


_FACTS_HINT_HEAD = (
    "\n\nCurrent known facts (for key reuse — if a note updates one of "
    "these, emit the claim under the SAME entity and attribute with the new "
    "current value; never emit a claim the notes do not state):\n"
)


def _facts_hint(known_facts: list[tuple[str, str, str]] | None) -> str:
    if not known_facts:
        return ""
    return _FACTS_HINT_HEAD + "\n".join(
        f"- {e} — {a}: {v}" for e, a, v in known_facts)


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


_OUTCOME_INFER_SYSTEM_PROMPT = (
    "You review the stored record of one work session and infer what "
    "OUTCOMES it reached. Reply with JSON only: {\"outcomes\": [{\"task\": "
    "<short stable task-type phrase>, \"outcome\": \"success\" | "
    "\"failure\" | \"correction\", \"about\": <tool/approach concerned, or "
    "null>, \"detail\": <one sentence of evidence quoted or paraphrased "
    "from the record>}]}.\n"
    "- Claim only outcomes the record actually evidences; prefer fewer, "
    "better-grounded claims.\n"
    "- failure = an approach was TRIED and hit a dead-end; correction = "
    "the USER explicitly corrected the assistant's belief or approach "
    "(an approach failing on its own is failure, not correction); "
    "success = something verifiably worked.\n"
    "- An outcome requires an ATTEMPT: something was tried, deployed, "
    "fixed, or decided, and its result is visible in the record. Sessions "
    "that only read, browse, take notes, or collect facts have NO "
    "outcome. An unfinished task, a deferred decision, or 'revisit "
    "later' is NOT an outcome — abstain. When unsure, abstain — a missed "
    "outcome is cheap, an invented one poisons downstream lessons.\n"
    "- Abstain example: record = 'Session: reading about css grid\\n"
    "- (notes) grid-template-areas allows named layout regions' -> "
    "{\"outcomes\": []} — a fact was noted, nothing was attempted.\n"
    "- If the record shows no clear outcome, return {\"outcomes\": []}."
)


_RELATIONS_PROMPT_HEAD = (
    "You extract durable RELATIONSHIPS between named entities from notes, as "
    'JSON: {"relations":[{"src":..,"relation":..,"dst":..}]}. Use ONLY these '
    "relation names:\n"
)
_RELATIONS_PROMPT_TAIL = (
    "\nAlways prefer the most specific listed relation. Use 'related-to' ONLY "
    "when the text explicitly states a meaningful connection that fits no "
    "listed relation — NEVER for entities that merely appear together in the "
    "same note. When no listed relation fits and no explicit connection is "
    "stated, skip the pair. src and dst are entity names (services, hosts, "
    "tools, components). Skip opinions, chit-chat, and anything with no "
    'entity-to-entity relationship. Return {"relations":[]} if nothing '
    "qualifies."
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
        line = " ".join(parts)
        if s.get("origin") == "inferred":
            line = f"[machine-inferred] {line}"
        lines.append(line)
    return "\n".join(lines)


def _parse_outcome_claims(content: str, cap: int) -> list[dict] | None:
    """Parse an outcome-inference reply. ``None`` = malformed (retryable),
    ``[]`` = the model found nothing (valid, advance), else claims.
    Enum violations are dropped, never coerced (record_outcome rule)."""
    import json as _json

    if cap <= 0:
        return []

    s, e = content.find("{"), content.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        parsed = _json.loads(content[s:e + 1])
    except ValueError:
        return None
    if not isinstance(parsed, dict) or "outcomes" not in parsed:
        return None
    raw = parsed["outcomes"]
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        task = str(c.get("task", "")).strip()
        outcome = str(c.get("outcome", "")).strip()
        if not task or outcome not in ("success", "failure", "correction"):
            continue
        out.append({
            "task": task, "outcome": outcome,
            "about": str(c.get("about", "") or "").strip() or None,
            "detail": str(c.get("detail", "") or "").strip() or None,
        })
        if len(out) >= cap:
            break
    return out


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
                 max_tokens: int = 400, timeout_seconds: float = 20.0,
                 system_prompt: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or None
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout_seconds)
        # Base system prompt for claims extraction. Defaults to the shipped
        # ``_SYSTEM_PROMPT`` (the daemon never passes this arg, so its behaviour
        # is byte-identical). Off-label harnesses (e.g. the LME-V2 trajectory
        # smoke) pass a domain-specific variant; the vocab/known-facts hints are
        # still appended, so key-reuse across a batch is preserved.
        self.system_prompt = system_prompt if system_prompt is not None else _SYSTEM_PROMPT

    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
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
                     "content": self.system_prompt + _vocab_hint(vocab)
                                + _facts_hint(known_facts)},
                    # Numbered so the model can cite which note each claim came
                    # from ("source") — per-claim attribution without giving up
                    # the one-batch call that keeps cross-note naming consistent.
                    {"role": "user", "content": "\n\n".join(
                        f"[{i + 1}] {t}" for i, t in enumerate(texts))},
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
            claim = Claim(entity=entity, attribute=attribute, value=value,
                          confidence=conf, origin="agent")
            try:
                idx = int(c.get("source")) - 1     # 1-based in the prompt
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(texts):
                claim["source"] = idx
            claims.append(claim)
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

    def infer_outcomes(self, context_text: str, *,
                       cap: int = 3) -> list[dict] | None:
        """Infer outcome signals from one closed episode's stored record.
        Transport failure raises ExtractorError (stage holds its cursor);
        malformed content returns None (bounded retry); [] is a valid
        nothing-found."""
        import json
        import urllib.request

        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _OUTCOME_INFER_SYSTEM_PROMPT},
                    {"role": "user", "content": context_text},
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
        except Exception as exc:  # noqa: BLE001 — transport, not content
            raise ExtractorError(f"infer_outcomes failed: {exc}") from exc
        return _parse_outcome_claims(content, cap)


_EXTRACTOR_MODES = ("auto", "primary", "fallback")


def resolve_endpoints(cfg) -> dict:
    """Resolve primary + fallback endpoint settings honouring the same
    env-vs-config ownership as ``build_extractor``: ``extractor_source ==
    "env"`` (the ops contract) lets PSEUDOLIFE_DREAM_* env vars override the
    dataclass; ``"config"`` uses the config values and ignores env. An
    unknown mode degrades to "auto" (never crash the sweep on a typo'd env
    var). Returns {mode, primary_url, primary_model, fallback_url,
    fallback_model, max_tokens, timeout}."""
    import os

    def _env_num(name, fallback, cast):
        raw = os.environ.get(name)
        if not raw:
            return fallback
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return fallback

    from_config = getattr(cfg, "extractor_source", "env") == "config"
    if from_config:
        out = {
            "primary_url": cfg.extractor_base_url,
            "primary_model": cfg.extractor_model,
            "fallback_url": cfg.fallback_base_url,
            "fallback_model": cfg.fallback_model,
            "mode": cfg.extractor_mode,
            "max_tokens": cfg.extractor_max_tokens,
            "timeout": cfg.extractor_timeout_seconds,
        }
    else:
        out = {
            "primary_url": (os.environ.get("PSEUDOLIFE_DREAM_BASE_URL")
                            or cfg.extractor_base_url),
            "primary_model": (os.environ.get("PSEUDOLIFE_DREAM_MODEL")
                              or cfg.extractor_model),
            "fallback_url": (os.environ.get("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL")
                             or cfg.fallback_base_url),
            "fallback_model": (os.environ.get("PSEUDOLIFE_DREAM_FALLBACK_MODEL")
                               or cfg.fallback_model),
            "mode": (os.environ.get("PSEUDOLIFE_DREAM_EXTRACTOR_MODE")
                     or cfg.extractor_mode),
            "max_tokens": _env_num("PSEUDOLIFE_DREAM_MAX_TOKENS",
                                   cfg.extractor_max_tokens, int),
            "timeout": _env_num("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS",
                                cfg.extractor_timeout_seconds, float),
        }
    if out["mode"] not in _EXTRACTOR_MODES:
        out["mode"] = "auto"
    return out


def probe_endpoint(base_url: str, timeout: float = 3.0) -> bool:
    """Is an OpenAI-compatible endpoint alive? GET /health at the base with
    any trailing /v1 stripped (the sonnet shim serves /health at root and
    answers 503 when its CLI is logged out); a 404 there means a plain
    llama-server, so retry as GET {base_url}/models. Only HTTP 200 counts."""
    import urllib.error
    import urllib.request

    root = base_url.rstrip("/")
    root = root.removesuffix("/v1")
    for url in (f"{root}/health", f"{base_url.rstrip('/')}/models"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404 and url.endswith("/health"):
                continue                      # llama-server: try /models
            return False
        except Exception:  # noqa: BLE001 — connection refused, timeout, DNS
            return False
    return False


def _host_resolves(hostname: str) -> bool:
    import socket
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except OSError:
        return False


_HOST_GATEWAY_NAME = "host.docker.internal"


def startup_extractor_warnings(cfg) -> list[str]:
    """Config-sanity checks for daemon startup — the misconfigurations that
    leave the dream pass silently on the wrong extractor (issues #11/#12).
    Returns human-readable warning strings; the caller logs them. The stock
    single-extractor default (in-stack sidecar, no fallback) stays silent."""
    r = resolve_endpoints(cfg)
    urls = [u for u in (r["primary_url"], r["fallback_url"]) if u]
    has_fallback = bool(r["fallback_url"] and r["fallback_model"])
    out: list[str] = []
    if (any(_HOST_GATEWAY_NAME in u for u in urls)
            and not _host_resolves(_HOST_GATEWAY_NAME)):
        out.append(
            f"an extractor URL uses {_HOST_GATEWAY_NAME} but the name does not "
            "resolve — on Linux Docker Engine the daemon needs the extra_hosts "
            f"'{_HOST_GATEWAY_NAME}:host-gateway' entry in ops/docker-compose.yml "
            "(shipped enabled; restore it if removed). Until it resolves, every "
            "probe fails and dreams silently run on the fallback (or fail).")
    if (r["mode"] == "auto" and not has_fallback
            and r["primary_url"] and _HOST_GATEWAY_NAME in r["primary_url"]):
        out.append(
            f"dream primary {r['primary_url']} is host-side but no fallback is "
            "configured — extractor_mode=auto is inert (single-extractor, no "
            "probe) and dreams fail while the endpoint is down. Set "
            "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL/_MODEL to keep the in-stack "
            "sidecar as automatic fallback; verify with "
            'memory_dream(action="status").')
    if has_fallback and r["primary_url"] == r["fallback_url"]:
        out.append(
            f"dream primary and fallback are the same endpoint "
            f"({r['primary_url']}) — the intended primary is never used; "
            "point PSEUDOLIFE_DREAM_BASE_URL at the primary and verify with "
            'memory_dream(action="status").')
    return out


# Seconds between the two probe attempts in auto mode (tests zero this).
_probe_retry_delay = 2.0


def _probe_primary(url: str) -> bool:
    """Probe with ONE retry: the first probe after a daemon container restart
    reliably fails (host-gateway cold start) while the endpoint is healthy —
    2/2 live dreams on 2026-07-19 fell back spuriously on a healthy shim."""
    import time

    if probe_endpoint(url):
        return True
    time.sleep(_probe_retry_delay)
    return probe_endpoint(url)


def build_extractor_with_fallback(cfg) -> tuple["DreamExtractor", str]:
    """Selection step for the LIVE dream path: returns (extractor, which)
    with which in {"primary", "fallback"}. Fallback unset => exactly
    ``build_extractor`` (no probe, single-extractor behavior). Mode "auto"
    probes the primary per invocation — recovery is automatic at the next
    sweep. Raises ValueError for mode "fallback" with no fallback URL.
    The bench/eval harness never calls this — it constructs extractors
    directly so runs stay pinned to one endpoint."""
    import os

    r = resolve_endpoints(cfg)
    api_key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or cfg.extractor_api_key
    if r["mode"] == "fallback":
        if not (r["fallback_url"] and r["fallback_model"]):
            raise ValueError(
                "extractor_mode=fallback but no fallback endpoint is "
                "configured (fallback_base_url/fallback_model)")
        return OpenAICompatExtractor(
            r["fallback_url"], r["fallback_model"], api_key=api_key,
            max_tokens=r["max_tokens"], timeout_seconds=r["timeout"],
        ), "fallback"
    if not (r["fallback_url"] and r["fallback_model"]) or r["mode"] == "primary":
        return build_extractor(cfg), "primary"
    # mode == "auto" with a configured fallback: probe (with one retry —
    # see _probe_primary), then choose.
    if r["primary_url"] and _probe_primary(r["primary_url"]):
        return build_extractor(cfg), "primary"
    logger.warning("dream primary extractor %s unreachable — using fallback %s",
                   r["primary_url"], r["fallback_url"])
    return OpenAICompatExtractor(
        r["fallback_url"], r["fallback_model"], api_key=api_key,
        max_tokens=r["max_tokens"], timeout_seconds=r["timeout"],
    ), "fallback"


def _status_extractor_fields(cfg, last_dream_extractor) -> dict:
    """Extractor-visibility block for ``dream_status`` (console badge).
    Probes the primary ONLY when a fallback is configured — the inert
    single-extractor deploy pays no probe cost on a status poll."""
    r = resolve_endpoints(cfg)
    has_fallback = bool(r["fallback_url"] and r["fallback_model"])
    return {
        "extractor_mode": r["mode"],
        "primary_url": r["primary_url"],
        "fallback_url": r["fallback_url"] if has_fallback else None,
        "primary_healthy": (probe_endpoint(r["primary_url"], timeout=2.0)
                            if has_fallback and r["primary_url"] else None),
        "last_dream_extractor": last_dream_extractor,
    }


def build_extractor(cfg) -> DreamExtractor:
    """Pick the extractor from config: an OpenAI-compatible endpoint when a
    base-URL + model are set, else a no-op (no automatic regex writes —
    single-writer cortex; see the 2026-06-19 design).

    ``cfg.extractor_source`` decides who owns the endpoint settings:
    ``"env"`` (default, the documented ops contract) lets the
    ``PSEUDOLIFE_DREAM_BASE_URL`` / ``_MODEL`` / ``_TIMEOUT_SECONDS`` /
    ``_MAX_TOKENS`` env vars override the dataclass; ``"config"`` (set by
    the Console's Extractor panel) uses the config values and ignores those
    env vars — otherwise a UI change would silently lose to the env defaults
    the compose file always sets. ``PSEUDOLIFE_DREAM_API_KEY`` is honoured
    in both modes (secrets stay out of config.yaml)."""
    import os

    def _env_num(name, fallback, cast):
        raw = os.environ.get(name)
        if not raw:
            return fallback
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return fallback

    from_config = getattr(cfg, "extractor_source", "env") == "config"
    if from_config:
        base_url, model = cfg.extractor_base_url, cfg.extractor_model
        max_tokens, timeout = cfg.extractor_max_tokens, cfg.extractor_timeout_seconds
    else:
        base_url = os.environ.get("PSEUDOLIFE_DREAM_BASE_URL") or cfg.extractor_base_url
        model = os.environ.get("PSEUDOLIFE_DREAM_MODEL") or cfg.extractor_model
        max_tokens = _env_num("PSEUDOLIFE_DREAM_MAX_TOKENS",
                              cfg.extractor_max_tokens, int)
        timeout = _env_num("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS",
                           cfg.extractor_timeout_seconds, float)
    api_key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or cfg.extractor_api_key
    if base_url and model:
        return OpenAICompatExtractor(
            base_url, model, api_key=api_key,
            max_tokens=max_tokens, timeout_seconds=timeout,
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
    # Superseded-row compaction rides every tick (spec 2026-07-14) — it must
    # run even when no dream fires, or a quiet bank never compacts.
    compacted = service.compact_superseded().get("total", 0)
    status = service.dream_status()
    if not status["would_fire"]:
        return {"fired": False, "reason": "below_threshold",
                "backlog": status["backlog"], "compacted": compacted}
    result = service.dream_run_auto()
    logger.info("dream sweep fired: %s", result)
    return {"fired": True, "compacted": compacted, **result}
