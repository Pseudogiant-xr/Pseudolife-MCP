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

import functools
import logging
import re
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
    # Set-membership operation ("add" | "remove"). Absent = scalar supersede.
    # Any other model-emitted value (incl. "set") normalises to absent HERE,
    # at the parse boundary — the 2026-07-31 correction: this field being
    # missing from the parse whitelist silently disabled the whole dream-op
    # path while the model emitted it correctly (c2-gate-verdict.json).
    op: str
    # v29 epistemic stance: the note's own hedge words, near-verbatim
    # ("probably", "per the runbook"), <= 48 chars. Absent = asserted
    # plainly. Blank/non-string/oversize input normalises HERE, at the
    # parse boundary (same rule as op — a field missing from the whitelist
    # silently disables the feature while the model emits it correctly).
    stance: str
    # Provenance quote (span gate, no schema): a verbatim span from the
    # cited note backing the claim, <= 200 chars. Absent = uncited-shaped;
    # the span gate's mode decides what that costs. Same parse-boundary
    # normalisation rule as stance.
    quote: str


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
    "When a note adds or removes an item from a COLLECTION the user "
    'maintains (restaurants tried, bikes owned, pending tasks), add an '
    '"op":"add" or "op":"remove" field to that claim instead of a plain '
    "supersede. op is ONLY for collection membership — a value that simply "
    "changed (a new job, a moved city) stays a plain claim with no op. "
    "Example. Notes: [3] tried Rosa's Diner tonight. [4] sold the road bike, "
    'no longer biking to work. Output: {"claims":['
    '{"entity":"user","attribute":"restaurants tried","value":"Rosa\'s '
    'Diner","op":"add","confidence":0.8,"source":3},'
    '{"entity":"user","attribute":"bikes owned","value":"road bike",'
    '"op":"remove","confidence":0.8,"source":4}]}\n'
    "COUNTS, TOTALS, AND QUANTITIES ARE NEVER MEMBERS: when a note "
    "states or updates how many of something the user has (a running "
    "count, a total, a follower number, a quantity), emit a plain claim "
    'whose value is the NEW number, with no "op" field — even when the '
    "note also names the item that changed the count. For example, the "
    "note [5] saw a Northern Flicker today, that makes 32 species at "
    "the park now — yields the single claim "
    '{"entity":"user","attribute":"bird species seen at park",'
    '"value":"32","confidence":0.9,"source":5} inside the one claims '
    'array, and NO "op":"add" claim for Northern Flicker.\n'
    "HEDGES GO IN A STANCE FIELD: when the note itself hedges a fact "
    '("probably", "might", "unconfirmed", "not final", "per the '
    'runbook"), keep the value CLEAN and put the note\'s own hedge '
    'words in a "stance" field on that claim — never inside the value, '
    "and never invent a stance the note does not carry; a plainly "
    "stated fact has no stance field. A hedged update is STILL an "
    "update: use the same entity and attribute as the fact it changes "
    "and emit only the CURRENT value, exactly as for a plain fact. For "
    "example, a later note [6] we'll probably move the deploy target "
    "again, to eu-west-1 next quarter — updates the deploy target slot "
    "from the earlier example to the single claim "
    '{"entity":"deploy target","attribute":"environment",'
    '"value":"eu-west-1","stance":"probably","confidence":0.6,'
    '"source":6} inside the one claims array.\n'
    'Return {"claims":[]} if nothing qualifies.'
)
# The op block + count-exclusion rule shipped 2026-08-01 (hold reversed by
# maintainer decision after the count-exclusion gate; c2op verdict
# artifacts). The update-anchored stance rule (v10) shipped 2026-08-14 by
# maintainer decision after its gates: probe capture 0.919 / false-stance
# 0.00, ladder 1.0/0.0 x2, KU-oracle cortex EXACTLY unchanged vs a
# same-window control (0.731, net 0, p=1.0); hybrid -0.038 (2W/5L,
# p=0.45, not significant) accepted as a soak watch item
# (stance-v10-ku-paired-verdict.json; soak review due ~2026-08-21). v10's
# two deltas vs the KU-failed v8 block (bank-diff forensics traced that
# failure to a diluted consolidation anchor): the "a hedged update is
# STILL an update" sentence, and a worked example reusing the v0
# example's own deploy-target slot as a later hedged update. This prompt
# must stay byte-identical to the measured artifact
# evals/prompts/ku_op_prompt_v10_stance_update.txt (pinned by
# test_op_prompt_artifact.py). Edit the prompt only through a new
# measured artifact + gate.


# Events-only prompt for the SEPARATE extraction pass (design doc
# 2026-08-04-separate-pass-events-design.md): the claims call runs
# _SYSTEM_PROMPT byte-identically, so events cannot tax claim quality —
# the v7 combined prompt measured -0.053 (p 0.011) on claims for exactly
# that reason and never shipped. Pinned byte-identical to the measured
# artifact evals/prompts/events_pass_v1.txt (test_events_pass.py); edit
# only through a new measured artifact + gate. Language carries over the
# v7 events section's phrasing, which produced well-formed blocks in
# both serving smokes.
_EVENTS_SYSTEM_PROMPT = (
    "You extract EVENTS from numbered notes — dated occurrences (a trip "
    "taken, a purchase, an adoption, a start or an end), not standing "
    'facts. Return JSON: {"events":[{"description":..,"actor":..,'
    '"date":"YYYY-MM-DD","date_phrase":<the note\'s own words about '
    'when>,"source":<number of the note>}]}. Resolve date from dates '
    "written in the note (including a leading [date] stamp); exact "
    "calendar days only — when the note's words cannot pin an exact "
    "day, set date to null and keep date_phrase verbatim. Never invent "
    "a date. For example, the note [7] [2023/05/14 (Sun) 10:02] user: "
    "we finally adopted the kitten yesterday! — yields the single event "
    '{"description":"adopted a kitten","actor":"user",'
    '"date":"2023-05-13","date_phrase":"yesterday","source":7} inside '
    "the one events array. Skip standing facts, opinions, and "
    "narrative; extract each real occurrence once. Return "
    '{"events":[]} if nothing qualifies.'
)

# Merge-proposal judge (autonomous Step C, 2026-08-16 design). The rules are
# the distilled house judgment brief the 2026-08-16 Opus panel ran with,
# grounded in 761 recorded verdicts (23% accept rate — hence the skeptical
# prior stated outright). SHARED between the daemon's shadow/auto judge and
# evals/judge_ladder.py so measured arms and the shipped judge are
# byte-identical; change it only through a new ladder run.
_JUDGE_SYSTEM_PROMPT = (
    "You judge MERGE PROPOSALS for a knowledge-graph deduper. Each numbered "
    "proposal asks: are FROM and INTO two names for the SAME real-world "
    "referent (accept folds FROM into INTO), or different things (reject)? "
    "Historically only ~23% of proposals are true merges — be skeptical.\n"
    "Rules:\n"
    "1. Evidence over name-shape: accept only when BOTH sides' evidence "
    "snippets describe the same referent. Similar names with mismatched or "
    "absent evidence are not enough.\n"
    "2. Differing variant tokens (sizes, quants, versions, dates: v0.8.5 vs "
    "v0.8.6, 26B vs 4B, two dated filenames) mean sibling artifacts — "
    "reject.\n"
    "3. A dated run/session slug paired with a broader name (a project, a "
    "process) is one event vs a category — reject.\n"
    "4. File-vs-concept, feature-vs-phase, tool-vs-its-output, table-vs-tool "
    "pairs are related but distinct — reject.\n"
    "5. Legitimate merge shapes: branch-vs-slug, path-vs-basename of the "
    "same file, bare-vs-qualified name, abbreviation-vs-full name — when "
    "the evidence agrees.\n"
    "6. Use \"leave\" only when the evidence is genuinely insufficient to "
    "decide; do not use it to avoid judging.\n"
    'Return JSON only: {"verdicts":[{"id":<proposal number>,'
    '"verdict":"accept"|"reject"|"leave","confidence":<0..1>,'
    '"note":"<reason, max 25 words>"}]} — one entry per proposal, '
    "confidence is your honest probability that the verdict is correct."
)


def format_judge_proposal(p: dict) -> str:
    """One proposal rendered for the judge prompt. Shared with the ladder
    harness so measurement and production serialize identically.

    ``snippet_chars`` on the proposal caps each snippet (0 = unbounded);
    absent, the frozen 240-char cap applies byte-for-byte, so every
    published judge number keeps its exact prompt. The sweep stamps
    ``deep_dream.judge_snippet_max_chars`` (2026-09-03)."""
    raw = p.get("snippet_chars")
    cap = 240 if raw is None else (max(0, int(raw)) or None)

    def _side(s: dict) -> str:
        snips = "; ".join((str(x)[:cap] if cap else str(x))
                          for x in (s.get("snippets") or [])[:2])
        return (f"'{s.get('display', '?')}' (degree {s.get('degree', 0)}, "
                f"scopes {s.get('scopes') or []})"
                + (f" evidence: {snips}" if snips else " evidence: none"))
    out = (f"[{p['n']}] FROM {_side(p.get('from') or {})}\n"
           f"    INTO {_side(p.get('into') or {})}\n"
           f"    detector: {p.get('reason') or '?'}"
           + (f" (score {p.get('score')})" if p.get("score") is not None
              else ""))
    # Absent key -> byte-identical output: frozen ladder fixtures and every
    # published judge number keep their exact prompts.
    if p.get("low_differential"):
        out += ("\n    caution: LOW-DIFFERENTIAL evidence — the shown "
                "snippets cannot tell the sides apart (heavy overlap, one "
                "side empty, or one side's evidence contained in the "
                "other's). Judge from the name-shape rules (2-5), degrees "
                "and scopes; \"leave\" is legitimate when those are "
                "inconclusive (rule 6).")
    return out


# ── Review-queue judges (2026-09-02 design) ───────────────────────────────
# Four more judgment briefs, distilled from the 2026-09-02 blind Opus panel
# (evals/data/queue_judge_eval_20260902.json: 37 links, 42 candidates, 20
# junk, 40 slot pairs, every verdict ratified and applied). SHARED with
# evals/queue_judge_ladder.py so measured arms and the shipped judges are
# byte-identical; change them only through a new ladder run.

_RELATION_VOCAB = (
    "depends-on (src needs dst), part-of (src is a component of dst), "
    "runs-on (src executes on host/platform dst), hosts (src serves dst), "
    "uses (src makes use of dst), configures (src sets up dst), "
    "stores-data-in, tests (src is a test of dst), implements (src code "
    "realizes concept dst), superseded-by (src replaced by dst), related-to "
    "(untyped; weakest)")

_LINK_JUDGE_SYSTEM_PROMPT = (
    "You judge LINK PROPOSALS for a knowledge graph. Each numbered proposal "
    "is a typed edge SRC --relation--> DST awaiting a verdict. Accepting "
    "writes it as durable structure that recall traverses; the bar is "
    "\"true and useful\", and direction and relation type matter.\n"
    "Relations: " + _RELATION_VOCAB + ".\n"
    "Verdicts:\n"
    "- accept: the relation holds in the stated direction and the evidence "
    "(notes naming both, existing edges, scopes) supports it. Different "
    "project scopes are NOT by themselves a reason to reject.\n"
    "- retype: the pair is genuinely related but the RELATION is wrong; give "
    "the corrected relation (same direction). A wrong direction is reject.\n"
    "- reject: false, vague, an edge that merely re-files one that already "
    "exists between the canonical nodes on a version- or value-suffixed "
    "alias, an endpoint that is an attribute/state name or a captured list "
    "rather than a thing, a relation that asserts something untrue of a "
    "file (a module does not run on a host), or evidence that says the "
    "thing is gone (uninstalled, deleted, superseded, rolled back).\n"
    "- leave: genuinely undecidable.\n"
    'Return JSON only: {"verdicts":[{"id":<number>,"verdict":"accept"|'
    '"retype"|"reject"|"leave","confidence":<0..1>,"relation":<vocab or '
    'null>,"note":"<reason, max 25 words>"}]} — one entry per proposal, '
    "confidence is your honest probability that the verdict is correct."
)

_CANDIDATE_JUDGE_SYSTEM_PROMPT = (
    "You judge LINK CANDIDATES for a knowledge graph: numbered pairs of "
    "currently UNLINKED entities whose context vectors are near. For each, "
    "decide whether a real typed relation holds that the graph should carry "
    "— or whether the similarity is shared context (mentioned in the same "
    "note), siblings under one parent (two options, models, runs, settings "
    "of one thing), or a name-similarity accident.\n"
    "Relations: " + _RELATION_VOCAB + ".\n"
    "Verdicts:\n"
    "- propose: a real relation holds; give relation, src and dst in the "
    "direction the relation reads, and a one-line rationale grounded in the "
    "evidence.\n"
    "- dismiss: no real relation (the pair is marked distinct and never "
    "resurfaces).\n"
    "- leave: undecidable — or the two names look like the SAME referent "
    "(that is a merge question, not a link; never dismiss it).\n"
    'Return JSON only: {"verdicts":[{"id":<number>,"verdict":"propose"|'
    '"dismiss"|"leave","confidence":<0..1>,"relation":<vocab or null>,'
    '"src":<display or null>,"dst":<display or null>,"rationale":"<max 25 '
    'words>"}]} — one entry per pair.'
)

_JUNK_JUDGE_SYSTEM_PROMPT = (
    "You judge JUNK ENTITY proposals for a knowledge graph. A name-shape "
    "detector flagged each numbered entity as an over-extraction artifact; "
    "accepting HARD-DELETES the entity (its edges cascade away; fact and "
    "lesson rows survive, merely unlinked). The detector classes have a "
    "measured false-positive tail (2026-09-02: list-artifact 6/6 correct, "
    "compound 2/4, slot-key 3/10), so judge the REFERENT, not the shape.\n"
    "Verdicts:\n"
    "- delete: no real referent — a comma list or spaced \"A / B\" / "
    "\"A + B\" compound of several things captured as one name, a "
    "flattened slot key (entity.attribute copied into a name), a captured "
    "sentence or task description, a bare number or status word. A "
    "lesson-minted object (its only edges are prefers/avoids from a lesson) "
    "with such a shape is a safe delete: the lesson keeps its text.\n"
    "- keep: a real thing — a file path, git branch or ref (origin/master, "
    "fix/x), a model or server id, a dotted config key, a code symbol "
    "(module.function, Class.method), a systemd unit, a host, a tool. "
    "Typed edges, facts and mentions are evidence of reality.\n"
    "- leave: genuinely undecidable.\n"
    'Return JSON only: {"verdicts":[{"id":<number>,"verdict":"delete"|'
    '"keep"|"leave","confidence":<0..1>,"note":"<max 25 words>"}]} — one '
    "entry per proposal, confidence is your honest probability."
)

_SLOT_JUDGE_SYSTEM_PROMPT = (
    "You judge DUPLICATE SLOT pairs in a slot-keyed store: the LESSON store "
    "(procedural do/avoid guidance keyed by task|aspect) or the WORLD-FACT "
    "store (cited external facts keyed by entity|attribute). Slot "
    "supersession only dedups within one key, so a near-duplicate parked "
    "under a second key accumulates silently.\n"
    "Verdicts:\n"
    "- duplicate: the two slots carry the SAME guidance or fact (a re-mint "
    "under a drifted key: the same task named two ways, the same pitfall, a "
    "low-confidence inferred twin of an observed lesson, the same external "
    "fact about the same entity). Say which to KEEP (\"a\" or \"b\"): the "
    "more general, reusable key; the observed over the inferred; for world "
    "facts the more precise entity and more authoritative source. Put "
    "anything the dropped side adds in \"fold\".\n"
    "- distinct: deliberate siblings — aspect variants of one task (approach "
    "vs pitfall vs correction), two different tools/products/papers that "
    "merely share an attribute, facts about different entities, two tasks "
    "that share wording but not guidance.\n"
    "- leave: undecidable from the values shown.\n"
    'Return JSON only: {"verdicts":[{"id":<number>,"verdict":"duplicate"|'
    '"distinct"|"leave","keep":"a"|"b"|null,"fold":<text or null>,'
    '"confidence":<0..1>,"note":"<max 25 words>"}]} — one entry per pair.'
)


def _snips(items, k=3, chars=240):
    return "; ".join(str(x)[:chars] for x in (items or [])[:k])


def format_link_proposal(p: dict) -> str:
    """One link proposal rendered for the judge prompt (shared with the
    ladder harness)."""
    out = (f"[{p['n']}] SRC '{p.get('src', '?')}' (scopes "
           f"{p.get('src_scopes') or []}) --{p.get('relation', '?')}--> "
           f"DST '{p.get('dst', '?')}' (scopes {p.get('dst_scopes') or []})\n"
           f"    detector: {p.get('rationale') or '?'}\n"
           f"    src edges: {_snips(p.get('src_edges'), 8, 80) or 'none'}\n"
           f"    dst edges: {_snips(p.get('dst_edges'), 8, 80) or 'none'}")
    both = p.get("co_mentions") or []
    if both:
        out += f"\n    notes naming both: {_snips(both)}"
    else:
        out += (f"\n    src notes: {_snips(p.get('src_mentions'), 2) or 'none'}"
                f"\n    dst notes: {_snips(p.get('dst_mentions'), 2) or 'none'}")
    return out


def format_candidate(p: dict) -> str:
    return (f"[{p['n']}] '{p.get('src', '?')}' vs '{p.get('dst', '?')}' "
            f"(cosine {p.get('similarity')})\n"
            f"    src notes: {_snips(p.get('src_snippets')) or 'none'}\n"
            f"    dst notes: {_snips(p.get('dst_snippets')) or 'none'}")


def format_junk_proposal(p: dict) -> str:
    return (f"[{p['n']}] '{p.get('display', '?')}' — detector: "
            f"{p.get('reason') or '?'}; degree {p.get('degree', 0)}; facts "
            f"{p.get('facts', 0)}; lesson-minted object: "
            f"{'yes' if p.get('lesson_object') else 'no'}; scopes "
            f"{p.get('scopes') or []}\n"
            f"    edges: {_snips(p.get('edges'), 8, 100) or 'none'}\n"
            f"    facts: {_snips(p.get('fact_text'), 3, 120) or 'none'}\n"
            f"    notes: {_snips(p.get('mentions')) or 'none'}")


def format_slot_pair(p: dict) -> str:
    def side(tag, s):
        s = s or {}
        extra = ""
        if p.get("store") == "lesson":
            extra = (f" [polarity {s.get('polarity')}, outcome "
                     f"{s.get('outcome')}, about {s.get('about')!r}]")
        elif s.get("source_url"):
            extra = f" [source {s.get('source_url')}]"
        return (f"    {tag} '{s.get('entity', '?')}' | "
                f"'{s.get('attribute', '?')}'{extra}: "
                f"{str(s.get('value', ''))[:240]}")
    return (f"[{p['n']}] store={p.get('store')} cosine {p.get('similarity')}\n"
            + side("A", p.get("a")) + "\n" + side("B", p.get("b")))


def _vocab_hint(vocab: list[str]) -> str:
    if not vocab:
        return ""
    # Spell out the shape. These keys are "entity.attribute", but a bare list
    # of them reads as a list of ENTITY names — so extractors periodically
    # emitted {"entity": "0-9-0-release.deployment-status", "attribute":
    # "value"}, minting a dotted entity that duplicates a correctly-shaped
    # fact (2026-07-26). unflatten_slot_key_claims repairs what still slips.
    return ("\n\nExisting slot keys, each written entity.attribute (reuse when "
            'a note updates one — emit the part BEFORE the dot as "entity" and '
            'the part AFTER it as "attribute"; never put a whole key in '
            '"entity", and never use the literal "value" as an attribute): '
            + ", ".join(vocab[:60]))


def unflatten_slot_key_claims(claims: list, vocab: list[str]) -> list:
    """Repair claims that flattened a vocab slot key into the entity name.

    ``cortex.vocab()`` renders keys as ``entity.attribute`` where both halves
    are already separator-collapsed, so a key holds EXACTLY ONE dot and the
    split is unambiguous. An extractor "reusing" such a key sometimes copies
    the whole string into ``entity`` and writes the literal ``"value"`` as the
    attribute; the result is a dotted entity duplicating a correctly-shaped
    fact (``0-9-0-release.deployment-status``, 2026-07-26).

    Splits only when EVERY guard holds — the attribute is literally ``value``,
    the entity contains a dot, and the prefix is a known entity (or the whole
    string is a known key). Genuinely dotted entities (``llama.cpp``,
    ``host.docker.internal``) therefore survive untouched. Pure; the caller
    passes the same vocab it handed the extractor."""
    from pseudolife_memory.memory.cortex import _norm_key

    keys = {str(k) for k in (vocab or [])}
    entities = {k.split(".", 1)[0] for k in keys if "." in k}
    if not entities:
        return claims
    out = []
    for c in claims:
        head, dot, tail = str(c.get("entity", "")).rpartition(".")
        if (dot and head and tail
                and _norm_key(str(c.get("attribute", ""))) == "value"
                and (_norm_key(head) in entities
                     or f"{_norm_key(head)}.{_norm_key(tail)}" in keys)):
            logger.debug("unflattened slot-key claim: %r -> %r . %r",
                         c.get("entity"), head, tail)
            c = {**c, "entity": head, "attribute": tail}
        out.append(c)
    return out


def events_from_parsed(parsed: dict, n_texts: int) -> list[dict]:
    """Validate the extractor's ``events`` array (chronicle, schema v28).

    Events ride the same batched call as claims and travel back in the
    same list, marked ``kind: "event"`` — the service claim loop routes
    them before slot resolution. Conservative by construction: an event
    without a non-empty description is dropped; ``date`` must be an exact
    ``YYYY-MM-DD`` (anything else — "May 2023", a phrase, a fabricated
    format — degrades to None, keeping the verbatim ``date_phrase``);
    ``source`` maps to the 0-based note index exactly like claims and is
    omitted when out of range. Pure; returns ``[]`` for anything that is
    not a list of dicts."""
    from datetime import datetime

    raw = parsed.get("events", []) if isinstance(parsed, dict) else []
    out: list[dict] = []
    for ev in raw if isinstance(raw, list) else []:
        if not isinstance(ev, dict):
            continue
        description = str(ev.get("description", "")).strip()
        if not description:
            continue
        date = ev.get("date")
        if date is not None:
            date = str(date).strip()
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                date = None
        phrase = ev.get("date_phrase")
        item: dict = {
            "kind": "event",
            "description": description,
            "actor": str(ev.get("actor") or "user").strip() or "user",
            "date": date,
            "date_phrase": (str(phrase).strip() or None
                            if phrase is not None else None),
        }
        try:
            idx = int(ev.get("source")) - 1     # 1-based in the prompt
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < n_texts:
            item["source"] = idx
        out.append(item)
    return out


# ── literal-faithfulness gate (2026-08-02 design doc) ────────────────────
# Date-like spans are exempt from gating: format variance ("2026-08-01" vs
# "August 1, 2026") makes digit matching unsafe, and the prompt's
# KEEP-LITERALS rule owns dates. The gate owns fabricated numbers,
# versions, and identifiers. Masking only ever removes tokens from the
# gateable set, so an over-broad date pattern fails open, never drops.
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATE_LIKE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"                        # 2026-09-30
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"           # 9/30/26, 30-09-2026
    r"|\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b"             # 2026/9/30
    r"|\b\d{1,2}-[a-z]{3}-\d{2,4}\b"                # 30-Sep-2026
    rf"|\b(?:{_MONTHS})[a-z]*\.?\s+"                # September 30(, 2026)
    r"(?:\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|\d{4})\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?"   # 30(th) (of) September
    rf"(?:{_MONTHS})[a-z]*\.?(?:,?\s+\d{{4}})?\b",
    re.IGNORECASE)
_ORDINAL_RE = re.compile(r"^(\d+)(?:st|nd|rd|th)$")
_STRIP_PUNCT = ".,;:!?()[]{}<>\"'`#*"
# Single-word spelled numbers a note may use where the extractor writes the
# digit ("three week break" -> "3-week"). Compound forms ("twenty-five")
# arrive as hyphen parts and compose from these entries.
_SPELLED_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100", "thousand": "1000",
}


def _norm_literal(token: str) -> str:
    """Normalize one token for literal matching: casefold, shed surrounding
    punctuation, currency/percent/approx marks, a trailing ``+`` (``2+`` =
    "2 or more"), thousands separators, a leading ``v`` on a version, and
    ordinal suffixes."""
    t = token.casefold().strip(_STRIP_PUNCT)
    t = t.lstrip("$€£~").rstrip("%+")
    t = t.replace(",", "").replace("_", "")
    if len(t) > 1 and t[0] == "v" and t[1].isdigit():
        t = t[1:]
    return _ORDINAL_RE.sub(r"\1", t)


def _literal_tokens(text: str, *, mask_dates: bool,
                    exempt_approx: bool = False) -> list[str]:
    src = _DATE_LIKE_RE.sub(" ", text) if mask_dates else text
    out = []
    for raw in src.split():
        if (exempt_approx
                and raw.casefold().strip(_STRIP_PUNCT).startswith("~")):
            # A value the extractor itself marks approximate ("~3 months")
            # is not a hard literal — same rationale as the date exemption.
            continue
        t = _norm_literal(raw)
        if not t:
            continue
        if "-" in t.strip("-"):
            # Internal hyphen: ranges ("1-3" ~ "1 to 3") and unit compounds
            # ("3-week", "66-acre") gate on their digit-bearing parts.
            out.extend(p for p in t.split("-")
                       if any(ch.isdigit() for ch in p))
        elif any(ch.isdigit() for ch in t):
            out.append(t)
    return out


def hard_literals(value: str) -> list[str]:
    """The gateable literals in a claim value: normalized digit-bearing
    tokens outside date-like spans, excluding extractor-marked
    approximations. Empty means the gate has nothing to check."""
    return _literal_tokens(value or "", mask_dates=True, exempt_approx=True)


def literal_violations(value: str, corpus: str) -> list[str]:
    """Gateable literals in ``value`` that ``corpus`` (source note text)
    does not back. Empty corpus abstains. A token passes on exact
    normalized match, numeric equality (``08`` ↔ ``8``, ``3.20`` ↔ ``3.2``),
    a spelled corpus form (``three week`` backs ``3-week``), or — for
    identifier-like tokens only, never bare numbers — a bidirectional
    substring match (``pr-81`` ↔ ``81``). Hyphenated ranges/compounds gate
    per digit part; ``~``-marked approximations are exempt."""
    if not (corpus or "").strip():
        return []
    gateable = hard_literals(value)
    if not gateable:
        return []
    # The corpus is evidence, not a claim — leave dates unmasked so their
    # digit parts can still back a token; extra tokens only fail open.
    corpus_tokens = set(_literal_tokens(corpus, mask_dates=False))
    # Spelled numbers back their digit forms ("three week" backs "3-week"):
    # exact single-word matches only — "hundreds" does not back 100.
    for raw in corpus.split():
        for word in raw.casefold().strip(_STRIP_PUNCT).split("-"):
            if word in _SPELLED_NUMBERS:
                corpus_tokens.add(_SPELLED_NUMBERS[word])
    bad = []
    for tok in gateable:
        if tok in corpus_tokens:
            continue
        try:
            num = float(tok)
        except ValueError:
            num = None
        for ct in corpus_tokens:
            if num is not None:
                try:
                    if float(ct) == num:
                        break
                except ValueError:
                    pass
            if (not tok.isdigit() and len(tok) >= 2 and len(ct) >= 2
                    and (tok in ct or ct in tok)):
                break
        else:
            bad.append(tok)
    return bad


# Span-gate normalisation: NFKC-normalise + casefold, then collapse every
# non-word run to a single space, so quoting/punctuation/whitespace and
# Unicode-form differences (NFC vs NFD "café", curly vs straight quotes)
# never fail a genuinely verbatim span, while word order and word identity
# must match exactly (paraphrase is not a quote). \w keeps letters of every
# script — an ASCII-only class would fragment accented words and reduce
# CJK notes to nothing. Known residual false-drop class, accepted and
# documented: a model that STRIPS diacritics while quoting ("cafe" for
# "café") fails containment — that is an altered quote, not a formatting
# difference; log-mode firing data decides if it ever matters. lru_cache:
# the claim loop verifies many claims against the same note text, and
# per-object string hashing makes repeat lookups near-free.
_SPAN_NORM_RE = re.compile(r"\W+")


@functools.lru_cache(maxsize=512)
def _span_norm(s: str) -> str:
    import unicodedata
    return _SPAN_NORM_RE.sub(
        " ", unicodedata.normalize("NFKC", (s or "").casefold())).strip()


def span_unbacked(quote: str | None, corpus: str) -> str | None:
    """Reason a claim's provenance quote fails admission, or ``None`` when
    the quote is a verbatim (normalised) span of ``corpus`` — the CITED
    note's text, deliberately not the batch union: a quote is from one
    note by construction, so source scope carries none of the literal
    gate's measured batch-vs-note false-drop classes.

    ``"quote_missing"`` — no usable quote at all (absent, blank, or
    nothing left after normalisation). ``"quote_unverified"`` — a quote
    that is not a span of the cited note (paraphrase, cross-note lift,
    or fabrication; an empty corpus cannot verify anything and lands
    here too). The ROUTING cost of each reason belongs to the caller's
    ``span_gate`` mode, never to this function."""
    q = _span_norm(quote or "")
    if not q:
        return "quote_missing"
    if q in _span_norm(corpus):
        return None
    return "quote_unverified"


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


_DIGEST_SYSTEM_PROMPT = (
    "You write a factual narrative digest of one work session from its "
    "stored record. Reply with JSON only: {{\"digest\": <the digest text>}}.\n"
    "- Target length about {target_chars} characters of plain prose.\n"
    "- Cover, in order: what the session set out to do; the phases or steps "
    "it went through; key decisions and the stated reasons for them; "
    "problems hit and how each was resolved; stated preferences or changes "
    "of direction.\n"
    "- Use ONLY events present in the record. Omit anything uncertain "
    "rather than inferring it — a missing detail is cheap, an invented one "
    "poisons later recall.\n"
    "- Keep explicit dates, versions, and numbers exactly as recorded.\n"
    "- Write in past tense anchored to the session (\"in this session…\", "
    "\"the user then…\") so the digest reads as history, never as a claim "
    "about the present.\n"
    "- No headings, no bullet lists — one compact narrative paragraph "
    "(two at most)."
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


def _parse_digest(content: str) -> str | None:
    """Parse a summarize_session reply. ``None`` = malformed (retryable);
    a digest is mandatory prose, so an empty/blank string is malformed
    too — there is no valid nothing-found for a non-empty session."""
    import json as _json

    s, e = content.find("{"), content.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        parsed = _json.loads(content[s:e + 1])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    digest = parsed.get("digest")
    if not isinstance(digest, str) or not digest.strip():
        return None
    return digest.strip()


def split_session_context(text: str, cap: int) -> list[str]:
    """Split a session-context render into sequential segments of at most
    ``cap`` characters, on line boundaries where possible (a single line
    over the cap is hard-split). Order-preserving and lossless up to the
    joining newlines: ``"\\n".join(parts)`` round-trips when no hard split
    occurred. Used by the digest stage's map-reduce path — no middle
    truncation, because the failure the digest layer fixes is exactly
    "the middle of the arc went missing" (spec 2026-08-24, decision 3)."""
    cap = max(1, int(cap))   # cap<=0 would spin forever on the hard split
    if len(text) <= cap:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        while len(line) > cap:                 # pathological single line
            if current:
                parts.append("\n".join(current))
                current, length = [], 0
            parts.append(line[:cap])
            line = line[cap:]
        extra = len(line) + (1 if current else 0)
        if length + extra > cap and current:
            parts.append("\n".join(current))
            current, length = [], 0
            extra = len(line)
        current.append(line)
        length += extra
    if current:
        parts.append("\n".join(current))
    return parts


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
                 # Defaults match DreamConfig.extractor_max_tokens/-timeout
                 # (kept in lockstep by test_judge_thinking_payload) — the old
                 # (400, 20s) pair was a pre-2026-06-22 remnant that only
                 # direct constructors hit; syncing only max_tokens would
                 # recreate the documented big-budget/tiny-timeout failure.
                 max_tokens: int = 2048, timeout_seconds: float = 240.0,
                 system_prompt: str | None = None,
                 events_prompt: str | None = None,
                 extra_body: dict | None = None,
                 judge_thinking: bool | str = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or None
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout_seconds)
        # Extra top-level request fields, merged under the real fields (real
        # keys win). The daemon never passes this; the eval harness pins the
        # llama-server prompt cache off (``cache_prompt: false``) because a
        # warm server's cache changes output on identical temperature-0 input
        # (measured 2026-08-09, evals/results/warm-cache-probe-0809.json).
        self.extra_body = dict(extra_body or {})
        # Experiment knob (2026-08-17, judge-ladder harness only — the daemon
        # never passes it, so shipped judge payloads stay byte-identical):
        # lets judge_merges leave thinking to the server/template default
        # instead of pinning it off. Server-side reasoning kwargs are inert
        # while the pin is present (a reasoning_effort=xhigh server produced
        # a byte-identical judge ladder). True = server/template default;
        # "low"/"medium" pins an explicit per-request reasoning_effort.
        self.judge_thinking = judge_thinking
        # Set by _judge_request from the response: the model name the
        # endpoint reported serving (None until the first judge call).
        self.served_model: str | None = None
        # Base system prompt for claims extraction. Defaults to the shipped
        # ``_SYSTEM_PROMPT`` (the daemon never passes this arg, so its behaviour
        # is byte-identical). Off-label harnesses (e.g. the LME-V2 trajectory
        # smoke) pass a domain-specific variant; the vocab/known-facts hints are
        # still appended, so key-reuse across a batch is preserved.
        self.system_prompt = system_prompt if system_prompt is not None else _SYSTEM_PROMPT
        # Events-pass prompt, same override pattern: the daemon never passes
        # this arg (shipped behaviour byte-identical to the measured v1);
        # A/B harnesses use it to gate candidate prompts (e.g.
        # evals/prompts/events_pass_v2.txt) before any ship decision.
        self.events_prompt = (events_prompt if events_prompt is not None
                              else _EVENTS_SYSTEM_PROMPT)

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
            body = json.dumps({**self.extra_body,
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
            # Chronicle events (schema v28) ride the same call; validated
            # here and routed by the claim loop via their kind marker. An
            # events-less prompt (the shipped v5) simply yields none.
            events = events_from_parsed(parsed, len(texts))
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
            if c.get("op") in ("add", "remove"):
                claim["op"] = c["op"]
            # v29 stance: strings only, stripped, capped at 48 chars;
            # anything else degrades to absent (asserted plainly).
            stance = c.get("stance")
            if isinstance(stance, str) and stance.strip():
                claim["stance"] = stance.strip()[:48]
            # Span-gate quote: same rule, capped at 200 chars — a truncated
            # prefix still verifies containment against the cited note.
            quote = c.get("quote")
            if isinstance(quote, str) and quote.strip():
                claim["quote"] = quote.strip()[:200]
            try:
                idx = int(c.get("source")) - 1     # 1-based in the prompt
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(texts):
                claim["source"] = idx
            claims.append(claim)
        return claims + events

    def extract_events(self, texts: list[str]) -> list[dict]:
        """The separate events pass: same endpoint and numbered-notes
        message, events-only system prompt (``self.events_prompt``,
        default the shipped ``_EVENTS_SYSTEM_PROMPT``),
        parsed by :func:`events_from_parsed`. Raises
        :class:`ExtractorError` on failure — the caller treats that as
        non-fatal (events are additive enrichment; claims must commit)."""
        import json
        import urllib.request

        texts = [t for t in (texts or []) if t]
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({**self.extra_body,
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.events_prompt},
                    {"role": "user", "content": "\n\n".join(
                        f"[{i + 1}] {t}" for i, t in enumerate(texts))},
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
        except Exception as exc:  # noqa: BLE001
            raise ExtractorError(f"events pass failed: {exc}") from exc
        return events_from_parsed(parsed, len(texts))

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
            body = json.dumps({**self.extra_body,
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
            body = json.dumps({**self.extra_body,
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

    def judge_merges(self, proposals: list[dict]) -> list[dict]:
        """Judge merge proposals (autonomous Step C). Each proposal dict
        carries ``n`` (1-based number), ``from``/``into`` sides (display,
        degree, scopes, snippets), ``reason`` and ``score`` — see
        :func:`format_judge_proposal`. Returns validated verdict dicts
        ``{"n", "verdict", "confidence", "note"}``; proposals the model
        skipped are simply absent. Raises :class:`ExtractorError` on
        transport/parse failure so the caller can tell failure from a
        genuine empty result."""
        proposals = [p for p in (proposals or []) if p]
        if not proposals:
            return []
        raw = self._judge_request(
            _JUDGE_SYSTEM_PROMPT,
            "\n\n".join(format_judge_proposal(p) for p in proposals),
            len(proposals), label="judge_merges")
        known = {int(p["n"]) for p in proposals}
        out: list[dict] = []
        for v in raw if isinstance(raw, list) else []:
            if not isinstance(v, dict):
                continue
            try:
                n = int(v.get("id"))
            except (TypeError, ValueError):
                continue
            verdict = str(v.get("verdict", "")).strip().lower()
            if n not in known or verdict not in ("accept", "reject", "leave"):
                continue
            try:
                conf = max(0.0, min(1.0, float(v.get("confidence", 0.5))))
            except (TypeError, ValueError):
                conf = 0.5
            note = str(v.get("note", "")).strip()[:200]
            out.append({"n": n, "verdict": verdict, "confidence": conf,
                        "note": note})
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
            body = json.dumps({**self.extra_body,
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

    def _judge_request(self, system_prompt: str, user_text: str,
                       n_rows: int, *, label: str) -> list:
        """One JSON-object judge call shared by every review-queue judge:
        the payload the merge judge shipped with (pinned by
        test_judge_thinking_payload), returning the raw ``verdicts`` list
        for the caller to validate. Raises :class:`ExtractorError` on
        transport/parse failure so a failed batch marks nothing."""
        import json
        import urllib.request

        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            # The judge owns its thinking dimension (judge_thinking / the
            # enable_thinking pin below) — the dreamer's effort knob rides
            # extra_body on the shared primary extractor (judge_url unset)
            # and must NOT reach this payload: the CLI shims honour a
            # top-level reasoning_effort, which would silently override the
            # pin the moment an operator tunes the dreamer.
            extra = {k: v for k, v in self.extra_body.items()
                     if k != "reasoning_effort"}
            payload = {**extra,
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "response_format": {"type": "json_object"},
                # Verdict rows are short but one is needed PER row; the
                # 120/row floor protects large batches from a caller that
                # constructed us with a small extraction budget.
                "max_tokens": max(self.max_tokens, 120 * n_rows),
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if self.judge_thinking:
                # Let thinking run, and give the reasoning trace headroom the
                # verdict budget lacks (reasoning tokens count against
                # max_tokens). True defers to the server/template default;
                # a string pins an explicit reasoning_effort level.
                if isinstance(self.judge_thinking, str):
                    payload["chat_template_kwargs"] = {
                        "reasoning_effort": self.judge_thinking}
                else:
                    del payload["chat_template_kwargs"]
                payload["max_tokens"] += 4096
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            # The model the endpoint actually SERVED (OpenAI-compatible
            # responses echo it). A name-agnostic endpoint (llama-server
            # serving one model under any requested name) reports the same
            # served model for two configured names — which is what the
            # merge judge's distinct-second-model check must compare.
            served = data.get("model") if isinstance(data, dict) else None
            self.served_model = str(served) if served else None
            content = data["choices"][0]["message"]["content"] or ""
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
            raw = parsed.get("verdicts", []) if isinstance(parsed, dict) else []
        except Exception as exc:  # noqa: BLE001
            raise ExtractorError(f"{label} failed: {exc}") from exc
        return raw if isinstance(raw, list) else []

    @staticmethod
    def _verdict_head(v, known: set, allowed: tuple):
        """``(n, verdict, confidence)`` for a well-formed verdict row whose
        id the batch knows and whose verdict is in ``allowed``, else None."""
        if not isinstance(v, dict):
            return None
        try:
            n = int(v.get("id"))
        except (TypeError, ValueError):
            return None
        verdict = str(v.get("verdict", "")).strip().lower()
        if n not in known or verdict not in allowed:
            return None
        try:
            conf = max(0.0, min(1.0, float(v.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        return n, verdict, conf

    @staticmethod
    def _text(v, key, cap=400):
        val = v.get(key)
        return str(val)[:cap] if val not in (None, "") else None

    def judge_links(self, proposals: list[dict]) -> list[dict]:
        """Judge pending link proposals (see :func:`format_link_proposal`).
        Returns ``{"n", "verdict", "confidence", "note", "relation"}`` rows;
        ``relation`` is set only on ``retype`` — a retype naming no relation
        cannot be applied and degrades to ``leave``."""
        proposals = [p for p in (proposals or []) if p]
        if not proposals:
            return []
        raw = self._judge_request(
            _LINK_JUDGE_SYSTEM_PROMPT,
            "\n\n".join(format_link_proposal(p) for p in proposals),
            len(proposals), label="judge_links")
        known = {int(p["n"]) for p in proposals}
        out: list[dict] = []
        for v in raw:
            head = self._verdict_head(v, known, ("accept", "retype", "reject", "leave"))
            if head is None:
                continue
            n, verdict, conf = head
            relation = self._text(v, "relation", 64) if verdict == "retype" else None
            if verdict == "retype" and not relation:
                verdict = "leave"
            out.append({"n": n, "verdict": verdict, "confidence": conf,
                        "note": self._text(v, "note"), "relation": relation})
        return out

    def judge_candidates(self, rows: list[dict]) -> list[dict]:
        """Judge Step-C link candidates (see :func:`format_candidate`).
        Returns ``{"n", "verdict", "confidence", "relation", "src", "dst",
        "rationale"}``; a ``propose`` naming no relation degrades to
        ``leave``; src/dst default to the pair's own order."""
        rows = [r for r in (rows or []) if r]
        if not rows:
            return []
        raw = self._judge_request(
            _CANDIDATE_JUDGE_SYSTEM_PROMPT,
            "\n\n".join(format_candidate(r) for r in rows),
            len(rows), label="judge_candidates")
        by_n = {int(r["n"]): r for r in rows}
        out: list[dict] = []
        for v in raw:
            head = self._verdict_head(v, set(by_n), ("propose", "dismiss", "leave"))
            if head is None:
                continue
            n, verdict, conf = head
            relation = src = dst = None
            if verdict == "propose":
                relation = self._text(v, "relation", 64)
                src = self._text(v, "src", 200) or by_n[n].get("src")
                dst = self._text(v, "dst", 200) or by_n[n].get("dst")
                if not relation:
                    verdict, src, dst = "leave", None, None
            out.append({"n": n, "verdict": verdict, "confidence": conf,
                        "relation": relation, "src": src, "dst": dst,
                        "rationale": self._text(v, "rationale")})
        return out

    def judge_junk(self, rows: list[dict]) -> list[dict]:
        """Judge junk proposals (see :func:`format_junk_proposal`). Returns
        ``{"n", "verdict", "confidence", "note"}`` rows."""
        rows = [r for r in (rows or []) if r]
        if not rows:
            return []
        raw = self._judge_request(
            _JUNK_JUDGE_SYSTEM_PROMPT,
            "\n\n".join(format_junk_proposal(r) for r in rows),
            len(rows), label="judge_junk")
        known = {int(r["n"]) for r in rows}
        out: list[dict] = []
        for v in raw:
            head = self._verdict_head(v, known, ("delete", "keep", "leave"))
            if head is None:
                continue
            n, verdict, conf = head
            out.append({"n": n, "verdict": verdict, "confidence": conf,
                        "note": self._text(v, "note")})
        return out

    def judge_slot_pairs(self, rows: list[dict]) -> list[dict]:
        """Judge lesson/world duplicate listings (see
        :func:`format_slot_pair`). Returns ``{"n", "verdict", "keep",
        "fold", "confidence", "note"}``; a ``duplicate`` naming no survivor
        degrades to ``leave``."""
        rows = [r for r in (rows or []) if r]
        if not rows:
            return []
        raw = self._judge_request(
            _SLOT_JUDGE_SYSTEM_PROMPT,
            "\n\n".join(format_slot_pair(r) for r in rows),
            len(rows), label="judge_slot_pairs")
        known = {int(r["n"]) for r in rows}
        out: list[dict] = []
        for v in raw:
            head = self._verdict_head(v, known, ("duplicate", "distinct", "leave"))
            if head is None:
                continue
            n, verdict, conf = head
            keep = fold = None
            if verdict == "duplicate":
                keep = str(v.get("keep") or "").strip().lower() or None
                fold = self._text(v, "fold", 600)
                if keep not in ("a", "b"):
                    verdict, keep, fold = "leave", None, None
            out.append({"n": n, "verdict": verdict, "keep": keep, "fold": fold,
                        "confidence": conf, "note": self._text(v, "note")})
        return out

    def summarize_session(self, context_text: str, *,
                          # Default matches DreamConfig.digest_target_chars
                          target_chars: int = 1200) -> str | None:
        """Digest one closed session's stored record into narrative prose
        (spec 2026-08-24). Transport failure raises ExtractorError (the
        stage holds its cursor); malformed/empty content returns None
        (bounded retry)."""
        import json
        import urllib.request

        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({**self.extra_body,
                "model": self.model,
                "messages": [
                    {"role": "system", "content":
                        _DIGEST_SYSTEM_PROMPT.format(
                            target_chars=int(target_chars))},
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
            raise ExtractorError(f"summarize_session failed: {exc}") from exc
        return _parse_digest(content)


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
    # Model-only override (console dreamer picker): wins over BOTH ownership
    # modes, primary only — URLs and the fallback model keep their owner.
    override = getattr(cfg, "extractor_model_override", None)
    if override:
        out["primary_model"] = override
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

# Launch-default alias names the Claude shim resolves to its own launch
# model (claude_shim.resolve_model): a status display showing one of these
# hides which model actually serves, so dream_status resolves the alias via
# the endpoint's /models listing (first entry = the shim's launch model;
# llama-server lists its --alias, which for the sidecar IS "extractor").
_ALIAS_MODEL_NAMES = ("extractor", "bench")
# base_url -> (served model id | None, monotonic stamp). Status is polled by
# the console and session hooks; the TTL bounds cost to one small GET per
# endpoint per window, and failures are cached too so a down endpoint can't
# stall every poll for the full timeout (the 2026-07-19 shim-health lesson).
_served_model_cache: dict[str, tuple[str | None, float]] = {}
_SERVED_MODEL_TTL = 300.0


def fetch_served_model(base_url: str, timeout: float = 1.5) -> str | None:
    """First model id from ``GET {base_url}/models`` — the concrete model a
    launch-default alias resolves to. ``None`` on any failure (unresolved is
    a display degradation, never an error)."""
    import json
    import time
    import urllib.request

    now = time.monotonic()
    hit = _served_model_cache.get(base_url)
    if hit and now - hit[1] < _SERVED_MODEL_TTL:
        return hit[0]
    served: str | None = None
    try:
        url = f"{base_url.rstrip('/')}/models"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        entries = data.get("data") or []
        if entries and isinstance(entries[0], dict) and entries[0].get("id"):
            served = str(entries[0]["id"])
    except Exception:  # noqa: BLE001 — connection refused, timeout, bad JSON
        served = None
    _served_model_cache[base_url] = (served, now)
    return served


def _probe_primary(url: str) -> bool:
    """Probe with ONE retry: the first probe after a daemon container restart
    reliably fails (host-gateway cold start) while the endpoint is healthy —
    2/2 live dreams on 2026-07-19 fell back spuriously on a healthy shim."""
    import time

    if probe_endpoint(url):
        return True
    time.sleep(_probe_retry_delay)
    return probe_endpoint(url)


def _cache_extra_body(cfg) -> dict | None:
    """The daemon-side ``cache_prompt`` pin (measured decision, 2026-08-09):
    llama-server's default prompt cache changes extractor output once
    populated (warm-cache-probe-0809), and pinning it off costs +7.25s/call
    of shared-prefix prefill on the live sidecar (sidecar-cache-latency) —
    noise for a background sweep. ``extractor_cache_prompt=None`` restores
    the server default; non-llama.cpp endpoints ignore the field."""
    v = getattr(cfg, "extractor_cache_prompt", False)
    return None if v is None else {"cache_prompt": bool(v)}


def _primary_extra_body(cfg) -> dict | None:
    """extra_body for the PRIMARY extractor: the cache pin plus, when the
    effort knob is set, an explicit ``reasoning_effort``. The CLI shims map
    the field to their CLI's effort flag per request; OpenAI-compatible
    servers read it natively; servers that don't know it ignore it. The
    fallback sidecar deliberately never receives it — same rule as the
    model-only override: primary-side tuning never perturbs the measured
    sidecar config, so fallback sites keep :func:`_cache_extra_body`."""
    body = dict(_cache_extra_body(cfg) or {})
    effort = getattr(cfg, "extractor_reasoning_effort", None)
    if effort:
        body["reasoning_effort"] = effort
    return body or None


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
            extra_body=_cache_extra_body(cfg),
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
        extra_body=_cache_extra_body(cfg),
    ), "fallback"


def _status_extractor_fields(cfg, last_dream_extractor) -> dict:
    """Extractor-visibility block for ``dream_status`` (console badge).
    Probes the primary ONLY when a fallback is configured — the inert
    single-extractor deploy pays no probe cost on a status poll."""
    r = resolve_endpoints(cfg)
    has_fallback = bool(r["fallback_url"] and r["fallback_model"])
    # Resolve a launch-default alias to the endpoint's concrete model; a
    # concrete name (including any override) needs no wire trip.
    served = (fetch_served_model(r["primary_url"])
              if r["primary_url"] and r["primary_model"] in _ALIAS_MODEL_NAMES
              else None)
    return {
        "extractor_mode": r["mode"],
        "primary_url": r["primary_url"],
        "primary_model": r["primary_model"],
        "primary_model_served": served,
        "fallback_url": r["fallback_url"] if has_fallback else None,
        "fallback_model": r["fallback_model"] if has_fallback else None,
        "extractor_source": getattr(cfg, "extractor_source", "env"),
        "model_override": getattr(cfg, "extractor_model_override", None),
        "reasoning_effort": getattr(cfg, "extractor_reasoning_effort",
                                    None) or None,
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
    in both modes (secrets stay out of config.yaml).

    Resolution is delegated to :func:`resolve_endpoints` — the single
    authority the status display also reads, so what ``dream_status`` shows
    (including the model-only override) is what this builder constructs.
    A private copy of the env-vs-config logic here previously let the two
    drift."""
    import os

    r = resolve_endpoints(cfg)
    api_key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or cfg.extractor_api_key
    if r["primary_url"] and r["primary_model"]:
        return OpenAICompatExtractor(
            r["primary_url"], r["primary_model"], api_key=api_key,
            max_tokens=r["max_tokens"], timeout_seconds=r["timeout"],
            extra_body=_primary_extra_body(cfg),
        )
    return NoOpExtractor()


def run_sweep_once(service) -> dict:
    """One headless sweep tick: if dreaming is enabled and the backlog+quiescence
    trigger would fire, run a dream with the configured extractor. Session-
    agnostic by construction (it keys on the cursor, not on session lifecycle).
    Compaction, dream-run-journal pruning, and retrieval-log pruning run
    unconditionally BEFORE the dream.enabled gate (issue #178) — every tick
    runs all three regardless of whether a dream fires or dreaming is even
    enabled. Returns ``{"fired": bool, "compacted": int, "runs_pruned": int,
    "retrieval_pruned": int | None, ...}``; never raises into the daemon's
    timer."""
    import time as _t

    cfg = service.config.memory.dream
    # Per-phase durations + one ledger line per tick (2026-09-01): the
    # 2026-08-31 hook-timeout forensics misattributed a stall to the
    # judging tick because only phase COMPLETIONS were logged — with no
    # tick start or duration in the ledger, a completion timestamp
    # invites reading the whole preceding window as that phase.
    t_start = _t.perf_counter()
    timings: dict[str, float] = {}

    def _timed(key, fn):
        t0 = _t.perf_counter()
        out = fn()
        timings[key] = round(_t.perf_counter() - t0, 3)
        return out

    def _done(result):
        timings["total"] = round(_t.perf_counter() - t_start, 3)
        result["timings"] = timings
        logger.info("sweep tick done in %.2fs (%s): %s", timings["total"],
                    result.get("reason") or ("fired" if result.get("fired")
                                             else "quiet"), timings)
        return result

    # Superseded-row compaction (spec 2026-07-14), the v27 dream-run
    # journal, and the v31 retrieval-event log all run BEFORE the
    # dream.enabled check below — none of the three is actually fed only
    # by the automatic backlog-triggered dream trigger that flag gates.
    # memory_fact_set/memory_world_set (compaction's feed) are a separate,
    # always-live write API; a manual `memory_dream` run/deep call and the
    # end-of-session dream (`_fire_and_forget_dream` → `dream_run_auto`,
    # neither of which checks cfg.enabled) both still write dream-run
    # journal rows; and the retrieval log accrues on every memory_search
    # regardless of dream activity. A dream-disabled bank (a first-class,
    # documented knob) still needs all three reapers, or these tables grow
    # unbounded with nothing else to prune them (issue #178, previously
    # true only for the retrieval log because this whole block used to sit
    # after the disabled-return).
    compacted = _timed("compact",
                       lambda: service.compact_superseded().get("total", 0))
    runs_pruned = _timed("prune_runs", service.prune_dream_runs)
    # getattr-guarded for older fakes/tests, like deep_dream_tick below.
    # retrieval_pruned stays None (not 0) when the fake/service predates
    # prune_retrieval_log, so the two "nothing to prune" and "no reaper
    # wired at all" cases stay distinguishable in the sweep result.
    prune_retrieval = getattr(service, "prune_retrieval_log", None)
    retrieval_pruned = _timed(
        "prune_retrieval",
        lambda: prune_retrieval() if prune_retrieval is not None else None)
    if not cfg.enabled:
        return _done({"fired": False, "reason": "disabled",
                      "compacted": compacted, "runs_pruned": runs_pruned,
                      "retrieval_pruned": retrieval_pruned})
    # Need-based deep-dream tick (mechanical Steps A/B only) rides the same
    # timer, independent of the shallow trigger — a quiet bank can still be
    # overdue for consolidation. getattr-guarded for older fakes/tests.
    deep_tick = getattr(service, "deep_dream_tick", None)
    deep = _timed("deep_tick",
                  lambda: deep_tick() if deep_tick is not None else None)
    if deep and deep.get("fired"):
        logger.info("deep-dream tick fired: %s", deep)
    extra = {"deep_tick": deep} if deep is not None else {}
    # Autonomous Step-C judge rides the same timer (2026-08-16 design):
    # shadow-judges a bounded batch of unjudged pending merge proposals,
    # auto-applying only what the configured mode allows. getattr-guarded
    # like the tick; never raises into the sweep.
    judge = getattr(service, "deep_dream_judge", None)
    judged = _timed("judge",
                    lambda: judge() if judge is not None else None)
    if judged and judged.get("judged"):
        logger.info("deep-dream judge: %s", judged)
    if judged is not None:
        extra["deep_judge"] = judged
    # The review-queue judges (2026-09-02): links, junk, store curation
    # and Step-C candidates, each a bounded, mode-gated batch, each
    # getattr-guarded like the merge judge and never raising into the sweep.
    for key, name in (("judge_links", "deep_dream_judge_links"),
                      ("judge_junk", "deep_dream_judge_junk"),
                      ("judge_curation", "deep_dream_judge_curation"),
                      ("judge_candidates", "deep_dream_judge_candidates")):
        fn = getattr(service, name, None)
        res = _timed(key, lambda fn=fn: fn() if fn is not None else None)
        if res and (res.get("judged") or res.get("applied")
                    or res.get("proposed") or res.get("error")):
            logger.info("deep-dream %s: %s", key, res)
        if res is not None:
            extra[f"deep_{key}"] = res
    status = service.dream_status()
    if not status["would_fire"]:
        return _done({"fired": False, "reason": "below_threshold",
                      "backlog": status["backlog"], "compacted": compacted,
                      "runs_pruned": runs_pruned,
                      "retrieval_pruned": retrieval_pruned, **extra})
    result = _timed("dream", service.dream_run_auto)
    logger.info("dream sweep fired: %s", result)
    return _done({"fired": True, "compacted": compacted,
                  "runs_pruned": runs_pruned,
                  "retrieval_pruned": retrieval_pruned, **extra, **result})
