"""Versioned audit and reconsideration for automatic graph decisions.

Only nondestructive terminal actions are eligible: merge/link rejects, junk
keeps, and candidate-pair dismissals. Human and unversioned legacy decisions
remain terminal, while merge or junk accepts are never recorded here.

Each decision has its own meta row so the durable audit does not require
rewriting an ever-growing history value. A small active index supplies bounded,
fair scans. Callers hold the service lock; observed model identity is supplied
by the judge outside that lock.
"""
from __future__ import annotations

from copy import deepcopy
import time


_INDEX_KEY = "automatic_review_decisions_v1"
_RECORD_PREFIX = "automatic_review_decision_v1:"
_VERSION = 1
_ALLOWED = {"merge": "reject", "link": "reject", "junk": "keep"}
_RECENT_CAP = 100
_MISSING = object()


def _index(storage) -> dict:
    value = storage.get_meta(_INDEX_KEY)
    if not isinstance(value, dict) or value.get("version") != _VERSION:
        return {"version": _VERSION, "next_seq": 1,
                "cursors": {}, "active": {}, "recent_ids": []}
    return {"version": _VERSION,
            "next_seq": max(1, int(value.get("next_seq", 1))),
            "cursors": dict(value.get("cursors") or {}),
            "active": dict(value.get("active") or {}),
            "recent_ids": list(value.get("recent_ids") or [])[-_RECENT_CAP:]}


def _record_key(decision_id: str) -> str:
    return _RECORD_PREFIX + decision_id


def decision_record(storage, decision_id: str) -> dict | None:
    value = storage.get_meta(_record_key(decision_id))
    return value if isinstance(value, dict) else None


def has_active_decisions(storage, queue: str) -> bool:
    """Cheap pre-extractor gate for an otherwise empty review queue."""
    return any(row.get("queue") == queue
               for row in _index(storage)["active"].values())


def recent_decisions(storage, limit: int = 20) -> list[dict]:
    """Return a bounded, non-sensitive projection of recent decision audit."""
    count = min(_RECENT_CAP, max(0, int(limit)))
    if not count:
        return []
    rows = []
    for decision_id in reversed(_index(storage)["recent_ids"]):
        record = decision_record(storage, decision_id)
        if not record:
            continue
        rows.append({key: deepcopy(record.get(key)) for key in (
            "queue", "action", "state", "proposal_id", "pair",
            "recorded_at", "ended_at", "end_reason")})
        if len(rows) == count:
            break
    return rows


def _touch_recent(index: dict, decision_id: str) -> None:
    recent = [value for value in index["recent_ids"]
              if value != decision_id]
    recent.append(decision_id)
    index["recent_ids"] = recent[-_RECENT_CAP:]


def _write_new(storage, target: str, marker: dict) -> dict:
    with storage.transaction():
        index = _index(storage)
        previous = index["active"].get(target)
        if isinstance(previous, dict):
            _retire(storage, index, target, previous,
                    state="superseded", reason="new automatic decision")
        seq = index["next_seq"]
        index["next_seq"] = seq + 1
        decision_id = str(seq)
        record = {"version": _VERSION, "decision_id": decision_id,
                  "seq": seq, "target": target, "state": "active",
                  "recorded_at": time.time(), **marker}
        summary = {"decision_id": decision_id, "seq": seq,
                   "queue": record["queue"], "pair": record.get("pair")}
        index["active"][target] = summary
        _touch_recent(index, decision_id)
        storage.set_meta(_record_key(decision_id), record)
        storage.set_meta(_INDEX_KEY, index)
    return {"recorded": True, "decision_id": decision_id}


def _retire(storage, index: dict, target: str, summary: dict, *,
            state: str, reason: str) -> None:
    decision_id = summary["decision_id"]
    marker = decision_record(storage, decision_id) or {}
    marker.update(state=state, ended_at=time.time(), end_reason=reason)
    storage.set_meta(_record_key(decision_id), marker)
    _touch_recent(index, decision_id)
    if index["active"].get(target, {}).get("decision_id") == decision_id:
        index["active"].pop(target, None)


def _canonical_pair(storage, queue: str, proposal: dict) -> tuple[str, str] | None:
    graph = storage.load_graph()
    canon = {e["id"]: e["canonical"] for e in graph["entities"]}
    if queue == "junk":
        from pseudolife_memory.service import _junk_keep_key

        value = canon.get(proposal.get("entity_id"))
        key = _junk_keep_key(value) if value else None
        return (key, key) if key else None
    if queue == "merge":
        left = canon.get(proposal.get("entity_id"))
        right = canon.get(proposal.get("into_id"))
    elif queue == "link" and proposal.get("source") == "analyzer":
        left = canon.get(proposal.get("src_id"))
        right = canon.get(proposal.get("dst_id"))
    else:
        return None
    if not left or not right or left == right:
        return None
    return tuple(sorted((left, right)))


def _dismissed_at(storage, pair) -> float | None:
    if not pair:
        return None
    row = storage.conn.execute(
        "SELECT dismissed_at FROM dismissed_pairs "
        "WHERE a_norm=%s AND b_norm=%s", tuple(pair)).fetchone()
    return float(row[0]) if row else None


def capture_proposal_pair(review, proposal: dict) -> dict:
    """Capture pair state immediately before a terminal automatic action."""
    pair = _canonical_pair(review.storage, review.kind, proposal)
    return {"pair": list(pair) if pair else None,
            "dismissed_at": _dismissed_at(review.storage, pair)}


def record_proposal_terminal(review, proposal: dict, *, action: str,
                             prior_pair=_MISSING) -> dict:
    """Record one successful dream-judge nondestructive terminal action."""
    queue = review.kind
    if action in ("merge", "delete", "accept"):
        return {"recorded": False, "reason": "destructive_action"}
    if _ALLOWED.get(queue) != action:
        return {"recorded": False, "reason": "unsupported_action"}
    storage = review.storage
    current = (storage.get_proposal(proposal["id"]) if queue == "link"
               else storage.get_entity_proposal(proposal["id"]))
    if (not current or current.get("status") != "rejected"
            or current.get("decided_by") != "dream-judge"):
        return {"recorded": False, "reason": "not_automatic_terminal"}
    signature = review.expected.get(str(proposal["id"]))
    if not signature:
        return {"recorded": False, "reason": "missing_fingerprint"}
    pair = _canonical_pair(storage, queue, proposal)
    requires_pair = (queue in ("merge", "junk")
                     or (queue == "link"
                         and proposal.get("source") == "analyzer"))
    if requires_pair and prior_pair is _MISSING:
        return {"recorded": False, "reason": "missing_prior_pair_state"}
    if prior_pair is not _MISSING:
        if (not isinstance(prior_pair, dict)
                or prior_pair.get("pair") != (list(pair) if pair else None)):
            return {"recorded": False, "reason": "pair_state_changed"}
        if prior_pair.get("dismissed_at") is not None:
            return {"recorded": False, "reason": "preexisting_pair"}
    pair_at = _dismissed_at(storage, pair)
    if pair and pair_at is None:
        return {"recorded": False, "reason": "pair_not_closed"}
    observed = getattr(review, "observed", None)
    served = observed(proposal) if callable(observed) else None
    served = (served or review.current_model
              or getattr(review.extractor, "served_model", None))
    target = f"{queue}:{proposal['id']}"
    return _write_new(storage, target, {
        "queue": queue, "target_type": "proposal",
        "proposal_id": int(proposal["id"]), "action": action,
        "proposal": deepcopy(proposal), "fingerprint": signature,
        "served_model": served, "decided_by": current.get("decided_by"),
        "second_policy": getattr(review, "second_policy", None),
        "second_model": getattr(review, "second_model", None),
        "decided_at": current.get("decided_at"),
        "terminal_status": current.get("status"),
        "pair": list(pair) if pair else None, "dismissed_at": pair_at})


def record_candidate_dismissal(service, candidate: dict, *,
                                 decision_fingerprint: str,
                                 policy_fingerprint: str,
                                 generation_fingerprint: str | None = None,
                                 served_model: str | None = None,
                                 evidence_fingerprint: str | None = None) -> dict:
    """Record one automatic candidate dismissal after its pair was closed."""
    from pseudolife_memory.graph import norm_name

    if not served_model:
        return {"recorded": False, "reason": "missing_observed_model"}
    graph = service._storage.load_graph()
    canon = {e["id"]: e["canonical"] for e in graph["entities"]}
    canon_by_norm = {}
    for entity in graph["entities"]:
        canon_by_norm.setdefault(entity["canonical"], entity["canonical"])
        canon_by_norm.setdefault(norm_name(entity.get("display") or ""),
                                 entity["canonical"])

    def resolve(field):
        entity_id = candidate.get(field + "_id")
        if entity_id in canon:
            return canon[entity_id]
        normalized = norm_name(candidate.get(field, ""))
        return canon_by_norm.get(normalized, normalized)

    pair = tuple(sorted((resolve("src"), resolve("dst"))))
    if not pair[0] or not pair[1] or pair[0] == pair[1]:
        return {"recorded": False, "reason": "bad_pair"}
    pair_at = _dismissed_at(service._storage, pair)
    if pair_at is None:
        return {"recorded": False, "reason": "pair_not_closed"}
    if evidence_fingerprint is None:
        evidence_fingerprint = candidate_evidence_fingerprints(
            service, [candidate])[0]
    target = "candidate:" + "|".join(pair)
    return _write_new(service._storage, target, {
        "queue": "candidate", "target_type": "candidate",
        "action": "dismiss", "candidate": deepcopy(candidate),
        "fingerprint": decision_fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "generation_fingerprint": generation_fingerprint,
        "evidence_fingerprint": evidence_fingerprint,
        "served_model": served_model, "pair": list(pair),
        "dismissed_at": pair_at})


def candidate_evidence_fingerprints(service, candidates: list[dict],
                                    inputs: dict | None = None) -> list[str]:
    """Fingerprint pair-local candidate evidence from one bank snapshot.

    Trace IDs take precedence exactly as in ``entity_context_vectors``;
    trace-less endpoints use the same token-subset fallback. Every matching
    entry is included even below the candidate generator's minimum-mention
    threshold so crossing that threshold invalidates the decision. Access and
    other serving-only metadata are deliberately absent.

    Pure over ``inputs`` (from ``review_judgments.candidate_inputs``);
    without them the caller holds the lock and the snapshot is read here.
    No candidates, no read.
    """
    from pseudolife_memory.graph import norm_name
    from pseudolife_memory.memory.graph_review import _token_set
    from pseudolife_memory.memory.review_judgments import (
        candidate_inputs, fingerprint)

    if not candidates:
        return []
    if inputs is None:
        inputs = candidate_inputs(service)
    graph = inputs["graph"]
    entries = inputs["entries"]
    scopes = inputs["scopes"]
    traces = inputs["traces"]
    fact_counts = inputs["facts"]
    pending_edges = inputs["pending_links"]
    pending_entities = inputs["pending_entities"]
    by_id = {e["id"]: e for e in graph["entities"]}
    by_norm = {}
    for entity in graph["entities"]:
        by_norm.setdefault(entity["canonical"], entity)
        by_norm.setdefault(norm_name(entity.get("display") or ""), entity)
    entry_by_id = {entry["id"]: entry for entry in entries}
    entry_tokens = [(entry["id"], _token_set(entry.get("text", "")))
                    for entry in entries]

    def canonical(entity_id):
        entity = by_id.get(entity_id)
        return entity.get("canonical") if entity else {"missing_id": entity_id}

    edge_pending_by_entity = {entity_id: [] for entity_id in by_id}
    for row in pending_edges:
        projection = {
            "id": row.get("id"), "src": canonical(row.get("src_id")),
            "relation": row.get("relation"),
            "dst": canonical(row.get("dst_id")),
            "confidence": row.get("confidence"),
            "similarity": row.get("similarity"),
            "rationale": row.get("rationale"), "source": row.get("source"),
            "created_at": row.get("created_at"), "status": row.get("status")}
        for entity_id in {row.get("src_id"), row.get("dst_id")}:
            if entity_id in edge_pending_by_entity:
                edge_pending_by_entity[entity_id].append(projection)
    entity_pending_by_entity = {entity_id: [] for entity_id in by_id}
    for row in pending_entities:
        projection = {
            "id": row.get("id"), "kind": row.get("kind"),
            "entity": canonical(row.get("entity_id")),
            "into": canonical(row.get("into_id")) if row.get("into_id") else None,
            "score": row.get("score"), "reason": row.get("reason"),
            "created_at": row.get("created_at"), "status": row.get("status")}
        for entity_id in {row.get("entity_id"), row.get("into_id")}:
            if entity_id in entity_pending_by_entity:
                entity_pending_by_entity[entity_id].append(projection)

    def resolve(candidate, field):
        entity_id = candidate.get(field + "_id")
        if entity_id in by_id:
            return by_id[entity_id]
        return by_norm.get(norm_name(candidate.get(field, "")))

    def endpoint(entity):
        if entity is None:
            return {"missing": True}
        trace_ids = list(traces.get(entity["canonical"], []))
        if trace_ids:
            mention_ids = trace_ids
            mention_source = "traces"
        else:
            want = _token_set(entity.get("display") or "")
            mention_ids = ([entry_id for entry_id, tokens in entry_tokens
                            if want and want <= tokens])
            mention_source = "fallback"
        mentions = []
        for entry_id in sorted(set(mention_ids)):
            entry = entry_by_id.get(entry_id)
            if entry is None:
                continue
            mentions.append({"id": entry_id, "text": entry.get("text"),
                             "embedding": entry.get("embedding")})
        incident = [edge for edge in graph["edges"]
                    if entity["id"] in (edge["src_id"], edge["dst_id"])]
        incident.sort(key=lambda edge: (edge.get("id", 0), edge["src_id"],
                                        edge["relation"], edge["dst_id"]))
        return {"entity": entity, "incident_edges": incident,
                "scopes": sorted(scopes.get(entity["id"], [])),
                "trace_ids": sorted(set(trace_ids)),
                "fact_count": fact_counts.get(entity["id"], 0),
                "mention_source": mention_source, "mentions": mentions,
                "pending_edges": edge_pending_by_entity[entity["id"]],
                "pending_entities": entity_pending_by_entity[entity["id"]]}

    return [fingerprint({"src": endpoint(resolve(candidate, "src")),
                         "dst": endpoint(resolve(candidate, "dst"))})
            for candidate in candidates]


def _batch(index: dict, queue: str, limit: int) -> list[tuple[str, dict]]:
    rows = [(target, summary) for target, summary in index["active"].items()
            if summary.get("queue") == queue]
    rows.sort(key=lambda item: int(item[1].get("seq", 0)))
    cursor = int(index["cursors"].get(queue, 0))
    ordered = ([row for row in rows if int(row[1]["seq"]) > cursor]
               + [row for row in rows if int(row[1]["seq"]) <= cursor])
    return ordered[:max(1, int(limit))]


def _proposal_projection(review, marker: dict, display: dict[int, str]):
    storage = review.storage
    current = (storage.get_proposal(marker["proposal_id"])
               if marker["queue"] == "link"
               else storage.get_entity_proposal(marker["proposal_id"]))
    if current is None:
        return None, None
    projection = deepcopy(marker["proposal"])
    for key in tuple(projection):
        if (key in current and not key.startswith("judge")
                and key not in ("status", "decided_by", "decided_at")):
            projection[key] = current[key]
    if "status" in projection:
        projection["status"] = "pending"
    if "decided_by" in projection:
        projection["decided_by"] = None
    if "decided_at" in projection:
        projection["decided_at"] = None
    if marker["queue"] == "link":
        projection["src"] = display.get(projection.get("src_id"))
        projection["dst"] = display.get(projection.get("dst_id"))
    else:
        projection["entity"] = display.get(projection.get("entity_id"))
        projection["into"] = display.get(projection.get("into_id"))
    return current, projection


def _owns_terminal(storage, marker: dict, current: dict | None) -> bool:
    if (not current or current.get("status") != marker.get("terminal_status")
            or current.get("decided_by") != marker.get("decided_by")
            or current.get("decided_at") != marker.get("decided_at")):
        return False
    pair = marker.get("pair")
    return not pair or _dismissed_at(storage, pair) == marker.get("dismissed_at")


def _reopen_proposal(storage, marker: dict) -> bool:
    table = "edge_proposals" if marker["queue"] == "link" else "entity_proposals"
    resets = ["status='pending'", "decided_by=NULL", "decided_at=NULL",
              "judge_verdict=NULL", "judge_confidence=NULL", "judge_note=NULL",
              "judge_model=NULL", "judged_at=NULL"]
    if table == "edge_proposals":
        resets.append("judge_relation=NULL")
    else:
        resets.extend(("judge2_verdict=NULL", "judge2_confidence=NULL",
                       "judge2_model=NULL", "judged2_at=NULL"))
    with storage.transaction():
        current = (storage.get_proposal(marker["proposal_id"])
                   if marker["queue"] == "link"
                   else storage.get_entity_proposal(marker["proposal_id"]))
        if not _owns_terminal(storage, marker, current):
            return False
        cur = storage.conn.execute(
            f"UPDATE {table} SET {', '.join(resets)} "
            f"WHERE id=%s AND status=%s AND decided_by=%s AND decided_at=%s",
            (marker["proposal_id"], marker["terminal_status"],
             marker["decided_by"], marker["decided_at"]))
        if cur.rowcount != 1:
            return False
        pair = marker.get("pair")
        if pair:
            removed = storage.conn.execute(
                "DELETE FROM dismissed_pairs WHERE a_norm=%s AND b_norm=%s "
                "AND dismissed_at=%s", (*pair, marker["dismissed_at"]))
            if removed.rowcount != 1:
                raise RuntimeError("automatic dismissal ownership changed")
    return True


def refresh_proposal_terminals(review, *, limit: int) -> dict[str, int]:
    """Reconsider one fair, bounded batch for ``review.kind``.

    ``review.signatures`` enriches the entire batch in one snapshot. Markers
    whose terminal row or pair ownership changed are retired as human/external
    state and never applied over.
    """
    storage = review.storage
    index = _index(storage)
    selected = _batch(index, review.kind, limit)
    if not selected:
        return {"considered": 0, "reopened": 0, "superseded": 0,
                "remaining": 0}

    candidates = []
    superseded = 0
    graph = storage.load_graph()
    display = {e["id"]: e["display"] for e in graph["entities"]}
    for target, summary in selected:
        marker = decision_record(storage, summary["decision_id"])
        if not marker or marker.get("queue") != review.kind:
            with storage.transaction():
                live_index = _index(storage)
                live_summary = live_index["active"].get(target)
                if live_summary and live_summary.get("decision_id") == summary["decision_id"]:
                    _retire(storage, live_index, target, live_summary,
                            state="externally_superseded",
                            reason="automatic decision record missing or invalid")
                    storage.set_meta(_INDEX_KEY, live_index)
                    superseded += 1
            continue
        current, projection = _proposal_projection(review, marker, display)
        if not _owns_terminal(storage, marker, current):
            with storage.transaction():
                live_index = _index(storage)
                live_summary = live_index["active"].get(target)
                if live_summary and live_summary.get("decision_id") == summary["decision_id"]:
                    _retire(storage, live_index, target, live_summary,
                            state="externally_superseded",
                            reason="terminal row or dismissal ownership changed")
                    storage.set_meta(_INDEX_KEY, live_index)
                    superseded += 1
            continue
        candidates.append((target, summary, marker, projection))

    signatures = review.signatures([row[3] for row in candidates]) \
        if candidates else {}
    from pseudolife_memory.memory.review_judgments import last_response_identity
    observations = (review.current_model, getattr(review.extractor, "served_model", None),
                    last_response_identity(review.service, review.kind,
                                           getattr(review, "request_policy", "")))
    reopened = 0
    for target, summary, marker, projection in candidates:
        changed = signatures.get(str(projection["id"])) != marker.get("fingerprint")
        if any(model and marker.get("served_model") != model for model in observations):
            changed = True
        if marker.get("second_policy"):
            second_model = last_response_identity(
                review.service, review.kind + "_second", marker["second_policy"])
            if second_model and second_model != marker.get("second_model"):
                changed = True
        if not changed:
            continue
        with storage.transaction():
            live_index = _index(storage)
            live_summary = live_index["active"].get(target)
            if (not live_summary
                    or live_summary.get("decision_id") != summary["decision_id"]):
                continue
            if not _reopen_proposal(storage, marker):
                continue
            _retire(storage, live_index, target, live_summary,
                    state="reopened",
                    reason="evidence, policy, or observed model changed")
            storage.set_meta(_INDEX_KEY, live_index)
            reopened += 1

    with storage.transaction():
        live_index = _index(storage)
        live_index["cursors"][review.kind] = int(selected[-1][1]["seq"])
        storage.set_meta(_INDEX_KEY, live_index)
        remaining = sum(1 for row in live_index["active"].values()
                        if row.get("queue") == review.kind)
    return {"considered": len(selected), "reopened": reopened,
            "superseded": superseded, "remaining": remaining}


def refresh_candidate_dismissals(service, *, policy_fingerprint: str,
                                   generation_fingerprint: str | None = None,
                                   served_model: str | None = None,
                                   last_response_model: str | None = None,
                                   limit: int) -> dict[str, int]:
    """Reconsider a bounded candidate-dismissal batch from one generation."""
    storage = service._storage
    index = _index(storage)
    selected = _batch(index, "candidate", limit)
    if not served_model and not last_response_model:
        remaining = sum(1 for row in index["active"].values()
                        if row.get("queue") == "candidate")
        return {"considered": 0, "reopened": 0, "superseded": 0,
                "remaining": remaining}
    markers = [(target, summary,
                decision_record(storage, summary["decision_id"]) or {})
               for target, summary in selected]
    evidence = candidate_evidence_fingerprints(
        service, [marker.get("candidate") or {}
                  for _target, _summary, marker in markers])
    reopened = superseded = 0
    for (target, summary, marker), current_evidence in zip(markers, evidence):
        pair = marker.get("pair")
        if not pair or _dismissed_at(storage, pair) != marker.get("dismissed_at"):
            with storage.transaction():
                live_index = _index(storage)
                live_summary = live_index["active"].get(target)
                if live_summary and live_summary.get("decision_id") == summary["decision_id"]:
                    _retire(storage, live_index, target, live_summary,
                            state="externally_superseded",
                            reason="candidate dismissal ownership changed")
                    storage.set_meta(_INDEX_KEY, live_index)
                    superseded += 1
            continue
        changed = (marker.get("policy_fingerprint") != policy_fingerprint
                   or marker.get("evidence_fingerprint") != current_evidence
                   or any(model and marker.get("served_model") != model
                          for model in (served_model, last_response_model)))
        if not changed:
            continue
        with storage.transaction():
            live_index = _index(storage)
            live_summary = live_index["active"].get(target)
            if (not live_summary
                    or live_summary.get("decision_id") != summary["decision_id"]
                    or _dismissed_at(storage, pair) != marker.get("dismissed_at")):
                continue
            removed = storage.conn.execute(
                "DELETE FROM dismissed_pairs WHERE a_norm=%s AND b_norm=%s "
                "AND dismissed_at=%s", (*pair, marker["dismissed_at"]))
            if removed.rowcount != 1:
                continue
            _retire(storage, live_index, target, live_summary,
                    state="reopened",
                    reason="evidence, policy, or observed model changed")
            storage.set_meta(_INDEX_KEY, live_index)
            reopened += 1
    with storage.transaction():
        live_index = _index(storage)
        if selected:
            live_index["cursors"]["candidate"] = int(selected[-1][1]["seq"])
        storage.set_meta(_INDEX_KEY, live_index)
        remaining = sum(1 for row in live_index["active"].values()
                        if row.get("queue") == "candidate")
    return {"considered": len(selected), "reopened": reopened,
            "superseded": superseded, "remaining": remaining}


def confirm_human_pair(storage, a_norm: str, b_norm: str) -> dict[str, int]:
    """Retire automatic ownership when a human confirms one graph pair."""
    pair = list(sorted((a_norm, b_norm)))
    superseded = 0
    with storage.transaction():
        index = _index(storage)
        for target, summary in list(index["active"].items()):
            if summary.get("pair") != pair:
                continue
            _retire(storage, index, target, summary,
                    state="human_confirmed", reason="human confirmed pair")
            superseded += 1
        storage.set_meta(_INDEX_KEY, index)
    return {"superseded": superseded}


def confirm_human_proposal(storage, queue: str,
                           proposal_id: int) -> dict[str, int]:
    """Retire automatic ownership when a human confirms a terminal row."""
    target = f"{queue}:{int(proposal_id)}"
    with storage.transaction():
        index = _index(storage)
        summary = index["active"].get(target)
        if not summary:
            return {"superseded": 0}
        _retire(storage, index, target, summary,
                state="human_confirmed", reason="human confirmed terminal")
        storage.set_meta(_INDEX_KEY, index)
    return {"superseded": 1}
