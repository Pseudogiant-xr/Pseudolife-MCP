"""Bind pending graph opinions to their evidence and judging policy.

Fingerprints bind pending opinions to the ordinary bounded judge batch.
Versioned automatic terminal decisions are handled by review_decisions;
human and unversioned legacy terminal decisions stay closed.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json


def _fingerprint_default(value):
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    if hasattr(value, "tobytes") and hasattr(value, "shape"):
        return {"shape": list(value.shape), "dtype": str(value.dtype),
                "bytes": hashlib.sha256(value.tobytes()).hexdigest()}
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    return str(value)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=_fingerprint_default).encode()).hexdigest()


def judging_policy(service, extractor, prompt):
    cfg = service.config.memory
    return {"version": 1, "prompt": prompt,
            "deep_dream": dataclasses.asdict(cfg.deep_dream),
            "model": getattr(extractor, "model", None),
            "endpoint": getattr(extractor, "base_url", None),
            "max_tokens": getattr(extractor, "max_tokens", None),
            "extra_body": getattr(extractor, "extra_body", None),
            "thinking": getattr(extractor, "judge_thinking", None),
            "override": cfg.dream.extractor_model_override,
            "effort": cfg.dream.extractor_reasoning_effort}


def observed_model(extractor):
    """Probe launch-default aliases outside the service lock; never guess."""
    base_url = getattr(extractor, "base_url", None)
    if base_url and getattr(extractor, "model", None) in ("extractor", "bench", "judge"):
        from pseudolife_memory.memory.dream import fetch_served_model
        return fetch_served_model(base_url)
    return getattr(extractor, "served_model", None)


def record_response_identity(service, queue, policy_fingerprint, model):
    """Remember a response for later reconsideration, never to authorize replay.

    Caller holds the service lock. A response-only endpoint can reveal a model
    change on another row; this durable hint lets the next bounded tick revisit
    its older decisions even after constructing a new extractor.
    """
    if isinstance(model, str) and model.strip():
        service._storage.set_meta("review_response_identity_v1_" + queue,
                                  {"policy": policy_fingerprint, "model": model})


def last_response_identity(service, queue, policy_fingerprint):
    """Historical observation for invalidation only; caller holds the lock."""
    saved = service._storage.get_meta("review_response_identity_v1_" + queue) or {}
    return saved.get("model") if saved.get("policy") == policy_fingerprint else None


def candidate_inputs(service):
    """Caller holds the lock. One read of everything the candidate scan's
    generation and the candidate evidence fingerprints consume, so a lock
    hold reads the bank once and the fingerprinting can run with the lock
    released (2026-09-23: an idle candidate tick read it three times under
    the lock, embeddings included)."""
    storage = service._storage
    return {"graph": storage.load_graph(),
            "entries": storage.load_entries(),
            "scopes": storage.entity_sources_map(),
            "traces": storage.traces_by_entity_norm(),
            "facts": storage.entity_fact_counts(),
            "fact_texts": storage.current_fact_counts_by_entity_text(),
            "pending_entities": storage.pending_entity_proposals(),
            "lesson_refs": storage.lesson_entity_ids(),
            "pending_links": storage.pending_proposals(),
            "dismissed": storage.dismissed_pairs()}


def candidate_generation(service, *, decision_inputs=True, inputs=None):
    """Observe all inputs to the candidate scan. Pure over ``inputs``
    (from :func:`candidate_inputs`); without them the caller holds the lock
    and they are read here."""
    if inputs is None:
        inputs = candidate_inputs(service)
    evidence = {"graph": inputs["graph"],
                        # Candidate vectors and snippets consume these fields.
                        # Recall counters must not stale an in-flight judgment.
                        "entries": [{key: row.get(key) for key in ("id", "text", "embedding")}
                                    for row in inputs["entries"]],
                        "scopes": inputs["scopes"],
                        "traces": inputs["traces"],
                        "facts": inputs["facts"],
                        "fact_texts": inputs["fact_texts"],
                        "pending_entities": [_evidence(p) for p in inputs["pending_entities"]],
                        "lesson_refs": inputs["lesson_refs"],
                        "config": dataclasses.asdict(service.config.memory.deep_dream)}
    if decision_inputs:
        evidence["pending_links"] = [_evidence(p) for p in inputs["pending_links"]]
        evidence["dismissed"] = sorted(inputs["dismissed"])
    return fingerprint(evidence)


def candidate_current(service, candidate):
    """Action-lock guard; unrelated candidate decisions do not stale a batch."""
    from pseudolife_memory.graph import norm_name
    graph = service._storage.load_graph()
    entities = graph["entities"]
    resolved = []
    for field in ("src", "dst"):
        eid = candidate.get(field + "_id")
        matches = [e for e in entities if (e["id"] == eid if eid is not None
                   else norm_name(candidate[field]) in
                   (e["canonical"], norm_name(e["display"] or "")))]
        if len(matches) != 1:
            return False
        resolved.append(matches[0])
    ids = {e["id"] for e in resolved}
    if len(ids) != 2:
        return False
    if any({p["src_id"], p["dst_id"]} == ids
           for p in service._storage.pending_proposals() + graph["edges"]):
        return False
    return tuple(sorted(e["canonical"] for e in resolved)) not in service._storage.dismissed_pairs()


def _evidence(value):
    if isinstance(value, dict):
        return {k: _evidence(v) for k, v in value.items()
                if k not in ("n", "id") and not k.startswith("judge")}
    if isinstance(value, (list, tuple)):
        return [_evidence(v) for v in value]
    return value


class ReviewJudgments:
    """One judge tick over a review queue. The caller holds the service lock
    around prepare(), refresh(), validate() and the record/apply calls, and
    releases it for sign(): prepare reads the evidence, sign computes the
    packs and fingerprints from it, refresh clears the verdicts they no
    longer match. The queue-sized work thus runs unlocked; validate()
    re-signs only the judged batch, under the lock that also covers its
    writes (2026-09-23: signing the whole ~490-row merge queue under the
    lock held it 1-3 s every tick)."""

    # (storage reads under the lock, pure pack builder) per kind.
    _ENRICH = {"merge": ("_judge_evidence_locked", "_judge_enrich_from"),
               "link": ("_link_evidence_locked", "_enrich_link_proposals_from"),
               "junk": ("_junk_evidence_locked", "_enrich_junk_proposals_from")}
    _PROMPT = {"merge": "_JUDGE_SYSTEM_PROMPT", "link": "_LINK_JUDGE_SYSTEM_PROMPT",
               "junk": "_JUNK_JUDGE_SYSTEM_PROMPT"}
    # Enriched fields derived from the OTHER rows of the list being
    # enriched. The judge never sees them, and signing one ties a row's
    # fingerprint to batch composition: a tick signs a row among the rows
    # prepare() picked, validate() only among the judged batch (before
    # 2026-09-23, among the whole pending queue). The merge ``group`` (the
    # endpoint a row shares with other pending rows) did exactly that, so
    # from 2026-09-22 17:56 the shadow merge judge re-sent the same 8 rows
    # ~125 times a day and recorded nothing. Signed as None rather than
    # dropped: a row that never had a group keeps the fingerprint it was
    # signed with before the fix, so its verdict or automatic decision
    # survives the change and only rows that had one re-sign.
    _CROSS_ROW = {"merge": frozenset({"group"})}

    def __init__(self, service, kind, extractor, pending, current_model=None):
        if kind not in self._ENRICH:
            raise ValueError("unsupported review queue")
        self.service, self.kind, self.extractor = service, kind, extractor
        self.storage = service._storage
        self.key = "review_judgments_v1_" + kind
        self.pending = pending
        self.current_model = current_model
        self.expected = {}
        self.enriched = {}
        self._to_sign, self._evidence = [], None
        self.request_policy = fingerprint(self.policy())

    def observed(self, proposal):
        saved = (self.storage.get_meta(self.key) or {}).get(str(proposal["id"]))
        return saved.get("served_model") if isinstance(saved, dict) else None

    def policy(self):
        from pseudolife_memory.memory import dream
        return judging_policy(self.service, self.extractor,
                              getattr(dream, self._PROMPT[self.kind]))

    def evidence(self, pending):
        """Caller holds the lock: the storage reads that signing ``pending``
        needs."""
        return getattr(self.service, self._ENRICH[self.kind][0])(pending)

    def _sign(self, pending, evidence):
        """Pure: ``(fingerprints, packs)`` by proposal id."""
        enriched = getattr(self.service, self._ENRICH[self.kind][1])(pending, evidence)
        policy = self.policy()
        cross_row = self._CROSS_ROW.get(self.kind, frozenset())
        signatures, packs = {}, {}
        for p, row in zip(pending, enriched):
            key = str(p["id"])
            signatures[key] = fingerprint({"proposal": _evidence(p),
                                           "evidence": _evidence(
                                               {k: None if k in cross_row else v
                                                for k, v in row.items()}),
                                           "policy": policy})
            packs[key] = row
        return signatures, packs

    def signatures(self, pending):
        """Caller holds the lock (the evidence is read here)."""
        if not pending:
            return {}
        return self._sign(pending, self.evidence(pending))[0]

    def prepare(self, limit):
        """Caller holds the lock. Read the evidence for every row this tick
        may sign: the rows carrying a verdict (refresh() checks each one)
        and the first ``limit`` unjudged rows. That covers any batch of at
        most ``limit`` unjudged rows taken in queue order after refresh():
        a row it clears already carried a verdict. The rest of the queue
        is neither signed nor read."""
        unjudged = {id(p) for p in [p for p in self.pending
                                    if not p.get("judge_verdict")][:max(0, int(limit))]}
        self._to_sign = [p for p in self.pending
                         if p.get("judge_verdict") or id(p) in unjudged]
        self._evidence = self.evidence(self._to_sign) if self._to_sign else None

    def sign(self):
        """Lock released: fingerprint the prepared rows and keep their packs
        (the model payload, see rows())."""
        if self._to_sign:
            self.expected, self.enriched = self._sign(self._to_sign, self._evidence)
        else:
            self.expected, self.enriched = {}, {}
        self._evidence = None

    def rows(self, batch):
        """The signed packs for ``batch``, numbered in batch order."""
        return [{**self.enriched[str(p["id"])], "n": i + 1}
                for i, p in enumerate(batch)]

    def refresh(self):
        """Caller holds the lock, after prepare() and sign(). Clears every
        verdict whose evidence, policy or served model no longer matches
        what it was recorded under, and returns the pending rows. A change
        landing between prepare() and here goes unseen until the next
        tick; validate() keeps it from being recorded or applied."""
        memo = self.storage.get_meta(self.key) or {}
        live = {str(p["id"]) for p in self.pending}
        memo = {key: value for key, value in memo.items() if key in live}
        # One meta read per tick, not per verdict row: this runs under the
        # lock over a queue that can be all verdict rows (~490 merges).
        observations = (self.current_model, getattr(self.extractor, "served_model", None),
                        last_response_identity(self.service, self.kind, self.request_policy))
        def matches(p):
            saved = memo.get(str(p["id"]))
            if not isinstance(saved, dict):
                return False
            return (saved.get("fingerprint") == self.expected[str(p["id"])]
                    and all(not model or saved.get("served_model") == model
                            for model in observations))
        stale = [p for p in self.pending if p.get("judge_verdict") and not matches(p)]
        with self.storage.transaction():
            for p in stale:
                if self.kind == "link":
                    self.storage.conn.execute(
                        "UPDATE edge_proposals SET judge_verdict=NULL, "
                        "judge_confidence=NULL, judge_note=NULL, judge_model=NULL, "
                        "judged_at=NULL, judge_relation=NULL "
                        "WHERE id=%s AND status='pending'", (p["id"],))
                else:
                    self.storage.conn.execute(
                        "UPDATE entity_proposals SET judge_verdict=NULL, "
                        "judge_confidence=NULL, judge_note=NULL, judge_model=NULL, "
                        "judged_at=NULL, judge2_verdict=NULL, judge2_confidence=NULL, "
                        "judge2_model=NULL, judged2_at=NULL "
                        "WHERE id=%s AND status='pending'", (p["id"],))
                for key in p:
                    if key.startswith("judge"):
                        p[key] = None
                memo.pop(str(p["id"]), None)
            self.storage.set_meta(self.key, memo)
        return self.pending

    def validate(self, batch, *, response_extractor=None, expected_model=None):
        """Refresh one projection after inference; caller keeps the lock to apply."""
        response = response_extractor or self.extractor
        response_policy = fingerprint(judging_policy(
            self.service, response, self.policy()["prompt"])) if response_extractor else self.request_policy
        record_response_identity(self.service, self.kind + ("_second" if response_extractor else ""),
                                 response_policy, getattr(response, "served_model", None))
        if response_extractor is not None:
            self.second_policy = response_policy
            self.second_model = getattr(response, "served_model", None)
        else:
            self.second_policy = self.second_model = None
        expected_model = self.current_model if response_extractor is None else expected_model
        if expected_model and getattr(response, "served_model", None) != expected_model:
            self.valid, self.batch = set(), {}
            return
        ids = {p["id"] for p in batch}
        pending = (self.storage.pending_proposals() if self.kind == "link"
                   else self.storage.pending_entity_proposals())
        live = [p for p in pending if p["id"] in ids]
        signatures = self.signatures(live)
        self.valid = {p["id"] for p in live
                      if signatures[str(p["id"])] == self.expected.get(str(p["id"]))}
        self.batch = {p["id"]: p for p in live}

    def current(self, proposal, *, first_opinion=False):
        """``first_opinion``: a second vote pairs with the first verdict
        ``proposal`` was read with, so the live row must still carry it.
        The signatures strip every judge* key and cannot see requeue()
        clear it during the second model call."""
        if proposal["id"] not in self.valid:
            return False
        if self.kind == "link":
            row = self.storage.get_proposal(proposal["id"])
        else:
            row = self.storage.get_entity_proposal(proposal["id"])
        if row is None or row.get("status") != "pending":
            return False
        if first_opinion and any(row.get(key) != proposal.get(key)
                                 for key in ("judge_verdict", "judge_confidence")):
            return False
        return True

    def record(self, proposal):
        memo = self.storage.get_meta(self.key) or {}
        memo[str(proposal["id"])] = {
            "fingerprint": self.expected[str(proposal["id"])],
            "served_model": getattr(self.extractor, "served_model", None)}
        self.storage.set_meta(self.key, memo)

    def retry(self, proposal):
        """An operational failure must not strand an otherwise eligible row."""
        memo = self.storage.get_meta(self.key) or {}
        memo.pop(str(proposal["id"]), None)
        self.storage.set_meta(self.key, memo)

    def apply(self, proposal, operation, *args, **kwargs):
        """Apply within the validation lock, without taking that lock twice."""
        from pseudolife_memory.memory.review_decisions import (
            capture_proposal_pair, record_proposal_terminal)
        prior_pair = (capture_proposal_pair(self, proposal)
                      if operation.__name__ in ("graph_reject_proposal", "graph_reject_entity_proposal")
                      else None)
        locked = getattr(self.service, "_" + operation.__name__ + "_locked")
        result = locked(*args, _review_guard=lambda: self.current(proposal), **kwargs)
        if result.get("rejected"):
            record_proposal_terminal(self, proposal,
                                     action="keep" if self.kind == "junk" else "reject",
                                     prior_pair=prior_pair)
        if not (result.get("accepted") or result.get("rejected")):
            self.retry(proposal)
        if result.get("accepted"):
            if self.kind in ("merge", "junk"):
                # A structural edit changes graph-wide differential evidence.
                self.valid.clear()
            else:
                endpoints = {proposal["src_id"], proposal["dst_id"]}
                self.valid.difference_update(
                    pid for pid, row in self.batch.items()
                    if endpoints.intersection((row["src_id"], row["dst_id"])))
        return result


def requeue(service, queue="all", limit=32):
    """Forget bounded pending opinions, never reopen a terminal decision."""
    kinds = ("merge", "link", "junk", "curation", "candidate")
    if queue != "all" and queue not in kinds:
        return {"error": "unknown_queue", "queues": list(kinds)}
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        return {"error": "limit_must_be_between_1_and_100"}
    counts = {}
    left = limit
    with service._lock:
        service._ensure_init()
        storage = service._storage
        if storage is None:
            return {"error": "storage_unavailable"}
        with storage.transaction():
            for kind in kinds if queue == "all" else (queue,):
                if left == 0:
                    break
                if kind == "curation":
                    lessons, world = service._slot_duplicate_listings(
                        service._curation_records("lesson", 0),
                        service._curation_records("world", 0), storage.dismissed_pairs())
                    rows = []
                    for store, candidates in (("lesson", lessons), ("world", world)):
                        saved = storage.curation_judgments(store)
                        for candidate in candidates:
                            pair = tuple(sorted((candidate["a_key"], candidate["b_key"])))
                            if pair in saved and len(rows) < left:
                                rows.append((store, *pair))
                    for row in rows:
                        storage.conn.execute(
                            "DELETE FROM curation_judgments WHERE store=%s "
                            "AND a_key=%s AND b_key=%s", row)
                    count = len(rows)
                elif kind == "candidate":
                    memo = storage.get_meta("deep_candidate_verdicts") or {}
                    pairs = dict(memo.get("pairs") or {})
                    keys = sorted(pairs)[:left]
                    for key in keys:
                        pairs.pop(key)
                    storage.set_meta("deep_candidate_verdicts", {**memo, "pairs": pairs})
                    done = storage.get_meta("deep_candidates_judged") or {}
                    storage.set_meta("deep_candidates_judged", {**done, "complete": False})
                    count = len(keys)
                else:
                    pending = (storage.pending_proposals() if kind == "link"
                               else [p for p in storage.pending_entity_proposals()
                                     if p.get("kind") == kind])
                    selected = [p for p in pending if p.get("judge_verdict")][:left]
                    memo_key = "review_judgments_v1_" + kind
                    memo = storage.get_meta(memo_key) or {}
                    for p in selected:
                        if kind == "link":
                            storage.conn.execute(
                                "UPDATE edge_proposals SET judge_verdict=NULL, "
                                "judge_confidence=NULL, judge_note=NULL, judge_model=NULL, "
                                "judged_at=NULL, judge_relation=NULL "
                                "WHERE id=%s AND status='pending'", (p["id"],))
                        else:
                            storage.conn.execute(
                                "UPDATE entity_proposals SET judge_verdict=NULL, "
                                "judge_confidence=NULL, judge_note=NULL, judge_model=NULL, "
                                "judged_at=NULL, judge2_verdict=NULL, judge2_confidence=NULL, "
                                "judge2_model=NULL, judged2_at=NULL "
                                "WHERE id=%s AND status='pending'", (p["id"],))
                        memo.pop(str(p["id"]), None)
                    storage.set_meta(memo_key, memo)
                    count = len(selected)
                counts[kind] = count
                left -= count
    return {"requeued": sum(counts.values()), "queues": counts, "limit": limit}
