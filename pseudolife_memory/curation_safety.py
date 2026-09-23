"""Fail-closed application of lesson and world duplicate judgments.

The judge runs outside the service lock.  Its evidence is therefore an
immutable snapshot, and an accepted decision is applied only if both current
records still match that snapshot before and after PostgreSQL row locking.
Resident stores are staged and published only after the durable transaction
commits.
"""
from __future__ import annotations

import hashlib
import json
import time
import dataclasses
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Mapping

import numpy as np

from pseudolife_memory.memory.cortex import _norm_key


_COMMON_FIELDS = (
    "entity", "attribute", "value", "polarity", "status", "confidence",
    "origin", "support", "provenance", "asserted_at", "last_confirmed",
    "supersedes_value", "superseded_by_value", "superseded_at", "tx_time",
    "valid_time", "hlc_phys", "hlc_logical", "writer_id", "session_id",
    "version",
)
_LESSON_FIELDS = ("about", "outcome")
_WORLD_FIELDS = (
    "source_url", "source_quote", "retrieved_at", "freshness_class",
    "content_hash", "source_doc_id",
)
_JUDGMENT_BINDINGS_META = "curation_judgment_evidence_v1"
_AUTO_DISMISSALS_META = "curation_auto_dismissals_v1"
_AUTO_REFRESH_CURSOR_META = "curation_auto_dismissal_refresh_cursor_v1"


class CurationReconciliationError(RuntimeError):
    """Durable curation state cannot yet be classified safely."""


class _LockedEvidenceChanged(RuntimeError):
    pass


def _get(record: object, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        aliases = {"entity": "task", "attribute": "aspect", "value": "lesson"}
        if name in record:
            return record[name]
        alias = aliases.get(name)
        return record.get(alias, default) if alias else default
    return getattr(record, name, default)


def _embedding_snapshot(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping) and set(value) >= {"dtype", "shape", "sha256"}:
        return {"dtype": str(value["dtype"]),
                "shape": [int(n) for n in value["shape"]],
                "sha256": str(value["sha256"])}
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    elif hasattr(value, "to_numpy"):
        value = value.to_numpy()
    array = np.ascontiguousarray(value)
    return {
        "dtype": str(array.dtype),
        "shape": [int(n) for n in array.shape],
        "sha256": hashlib.sha256(array.tobytes(order="C")).hexdigest(),
    }


def snapshot_record(store: str, record: object) -> dict[str, Any]:
    """Return the complete logical evidence record in a stable JSON shape.

    PostgreSQL-only surrogate/graph ids are excluded: the judge sees a slot
    record, not its storage placement.  The derived embedding is represented by
    dtype, shape, and digest so a re-embed also invalidates a similarity-based
    judgment without putting a 1024-float vector in the request.
    """
    if store not in ("lesson", "world"):
        raise ValueError("store must be 'lesson' or 'world'")
    entity = str(_get(record, "entity", ""))
    attribute = str(_get(record, "attribute", ""))
    out: dict[str, Any] = {
        "store": store,
        "key": f"{_norm_key(entity)}|{_norm_key(attribute)}",
        "entity_norm": _norm_key(entity),
        "attribute_norm": _norm_key(attribute),
    }
    defaults = {
        "polarity": "+", "status": "current", "confidence": 0.0,
        "support": ["source"] if store == "world" else [],
        "provenance": [], "version": 1,
    }
    for name in _COMMON_FIELDS:
        value = _get(record, name, defaults.get(name))
        if name in ("support", "provenance"):
            value = sorted(str(x) for x in (value or []))
        elif name == "confidence":
            # PostgreSQL REAL round-trips through float32; compare the logical
            # value rather than rejecting 0.9 vs 0.899999976.
            value = round(float(value or 0.0), 6)
        elif name in ("hlc_phys", "hlc_logical", "version", "source_doc_id"):
            value = None if value is None else int(value)
        elif name in ("asserted_at", "last_confirmed", "superseded_at",
                      "tx_time", "valid_time", "retrieved_at"):
            value = None if value is None else float(value)
        out[name] = value
    fields = _LESSON_FIELDS if store == "lesson" else _WORLD_FIELDS
    for name in fields:
        value = _get(record, name)
        if name == "retrieved_at":
            value = float(value or 0.0)
        elif name == "source_doc_id":
            value = None if value is None else int(value)
        out[name] = value
    out["embedding"] = _embedding_snapshot(_get(record, "embedding"))
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, (set, frozenset, tuple, list)):
        values = [_jsonable(v) for v in value]
        return sorted(values, key=lambda v: json.dumps(v, sort_keys=True)) \
            if isinstance(value, (set, frozenset)) else values
    if isinstance(value, np.generic):
        return value.item()
    return value


def evidence_fingerprint(record: Mapping[str, Any]) -> str:
    """Stable SHA-256 identity for one canonical evidence mapping."""
    payload = json.dumps(
        _jsonable(record), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PairEvidence:
    store: str
    a: dict[str, Any]
    b: dict[str, Any]
    a_fingerprint: str
    b_fingerprint: str

    def with_side(self, side: str, record: Mapping[str, Any]) -> "PairEvidence":
        if side not in ("a", "b"):
            raise ValueError("side must be 'a' or 'b'")
        snap = deepcopy(dict(record))
        return replace(
            self, **{side: snap, f"{side}_fingerprint": evidence_fingerprint(snap)})


def capture_pair_evidence(store: str, a: object, b: object) -> PairEvidence:
    """Capture both complete current records before a judge request is sent."""
    if a is None or b is None:
        raise ValueError("both current records are required")
    left, right = snapshot_record(store, a), snapshot_record(store, b)
    if (left["entity_norm"], left["attribute_norm"]) == (
            right["entity_norm"], right["attribute_norm"]):
        raise ValueError("duplicate evidence sides must name different slots")
    return PairEvidence(
        store, left, right, evidence_fingerprint(left), evidence_fingerprint(right))


@dataclass(frozen=True)
class AutoFoldDecision:
    allowed: bool
    reason: str
    keep: str | None = None
    kind: str | None = None


def _reject(reason: str) -> AutoFoldDecision:
    return AutoFoldDecision(False, reason)


def can_auto_fold(pair: PairEvidence, verdict: Mapping[str, Any]) -> AutoFoldDecision:
    """Classify the narrow duplicate classes safe for automatic retirement.

    A judge may select the survivor, but it cannot author replacement text.
    Automatic lesson folds require exact normalized text. Text containment is
    not proof: qualifiers, negation, or a later correction can reverse the
    contained phrase. Category conflicts remain for review. World facts require
    citation equivalence and may only keep the at-least-as-fresh record.
    """
    if verdict.get("verdict", "duplicate") != "duplicate":
        return _reject("verdict_not_duplicate")
    keep = verdict.get("keep")
    if keep not in ("a", "b"):
        return _reject("invalid_keep")
    fold = verdict.get("fold")
    survivor = pair.a if keep == "a" else pair.b
    loser = pair.b if keep == "a" else pair.a
    if fold not in (None, "", survivor["value"]):
        return _reject("invented_fold")
    if survivor.get("status") != "current" or loser.get("status") != "current":
        return _reject("evidence_not_current")

    common = ("polarity", "origin")
    for field in common:
        if survivor.get(field) != loser.get(field):
            return _reject(f"conflicting_{field}")
    if pair.store == "lesson":
        for field in ("outcome", "about"):
            if survivor.get(field) != loser.get(field):
                return _reject(f"conflicting_{field}")
    else:
        for field in (
                "source_url", "source_quote", "freshness_class", "content_hash",
                "source_doc_id", "confidence", "support", "provenance"):
            if survivor.get(field) != loser.get(field):
                return _reject(f"conflicting_{field}")

    # Case can be semantic in code, identifiers, flags, and quoted material.
    # Only edge whitespace is safe to ignore without understanding the text.
    kept = str(survivor.get("value") or "").strip()
    removed = str(loser.get("value") or "").strip()
    if not kept or not removed:
        return _reject("empty_value")
    if kept == removed:
        kind = "exact"
    else:
        return _reject("no_exact_safe_relation")
    if pair.store == "world":
        survivor_age = (
            float(survivor.get("retrieved_at") or 0.0),
            float(survivor.get("last_confirmed") or 0.0),
            float(survivor.get("asserted_at") or 0.0),
        )
        loser_age = (
            float(loser.get("retrieved_at") or 0.0),
            float(loser.get("last_confirmed") or 0.0),
            float(loser.get("asserted_at") or 0.0),
        )
        if survivor_age < loser_age:
            return _reject("stale_world_survivor")
    return AutoFoldDecision(True, kind, keep=keep, kind=kind)


def _pair_id(evidence: PairEvidence) -> str:
    slots = sorted((_slot(evidence.a), _slot(evidence.b)))
    return hashlib.sha256(json.dumps(
        [evidence.store, slots], separators=(",", ":")).encode("utf-8")).hexdigest()


def curation_policy_fingerprint(
        service, extractor, *, observed_model: str | None = None,
) -> str:
    """Fingerprint every input that can change a curation judge's policy."""
    from pseudolife_memory.memory import dream

    cfg = service.config.memory
    return evidence_fingerprint({
        "version": 1,
        "prompt": dream._SLOT_JUDGE_SYSTEM_PROMPT,
        "deep_dream": dataclasses.asdict(cfg.deep_dream),
        "extractor_model": getattr(extractor, "model", None),
        "extractor_url": getattr(extractor, "base_url", None),
        "max_tokens": getattr(extractor, "max_tokens", None),
        "timeout": getattr(extractor, "timeout", None),
        "extra_body": getattr(extractor, "extra_body", None),
        "judge_thinking": getattr(extractor, "judge_thinking", None),
        "observed_model": observed_model,
        "override": cfg.dream.extractor_model_override,
        "effort": cfg.dream.extractor_reasoning_effort,
    })


def curation_observed_model(extractor) -> str | None:
    """Return a positively observed served identity, or ``None``.

    The configured model name remains part of the requested-policy
    fingerprint.  It is not evidence of what an endpoint actually served.
    Network probing is intentionally owned by the caller, outside the service
    lock; :func:`fetch_served_model` supplies its own short-lived cache.
    """
    served = getattr(extractor, "served_model", None)
    if served:
        return str(served)
    base_url = getattr(extractor, "base_url", None)
    requested = getattr(extractor, "model", None)
    if base_url and requested in ("extractor", "bench", "judge"):
        from pseudolife_memory.memory.dream import fetch_served_model
        probed = fetch_served_model(base_url)
        if probed:
            return str(probed)
    return None


def _judgment_signature(evidence: PairEvidence, policy_fingerprint: str) -> str:
    sides = sorted((
        {"slot": _slot(evidence.a), "fingerprint": evidence.a_fingerprint},
        {"slot": _slot(evidence.b), "fingerprint": evidence.b_fingerprint},
    ), key=lambda side: side["slot"])
    return evidence_fingerprint({
        "store": evidence.store, "sides": sides,
        "policy": policy_fingerprint,
    })


def curation_judgment_is_fresh(
        judgment: Mapping[str, Any] | None,
        bindings: Mapping[str, Any], evidence: PairEvidence,
        policy_fingerprint: str, *, now: float, horizon: float,
) -> bool:
    """A time-fresh memo is reusable only for identical evidence and policy."""
    return curation_judgment_state(
        judgment, bindings, evidence, policy_fingerprint,
        now=now, horizon=horizon) != "stale"


def curation_judgment_state(
        judgment: Mapping[str, Any] | None,
        bindings: Mapping[str, Any], evidence: PairEvidence,
        policy_fingerprint: str, *, now: float, horizon: float,
) -> str:
    """Return ``stale``, ``pending``, or a settled action state."""
    if judgment is None:
        return "stale"
    try:
        time_fresh = (float(now) - float(judgment["judged_at"])) < float(horizon)
    except (KeyError, TypeError, ValueError):
        return "stale"
    binding = bindings.get(_pair_id(evidence))
    if isinstance(binding, str):
        signature, action = binding, "settled"
    elif isinstance(binding, Mapping):
        signature = binding.get("signature")
        action = str(binding.get("action") or "settled")
    else:
        return "stale"
    if (not time_fresh
            or signature != _judgment_signature(evidence, policy_fingerprint)):
        return "stale"
    return action


def curation_judgment_bindings(storage) -> dict[str, Any]:
    raw = storage.get_meta(_JUDGMENT_BINDINGS_META) or {}
    return {str(k): v for k, v in raw.items()}


def curation_bound_observed_model(
        bindings: Mapping[str, Any], evidence: PairEvidence,
) -> str | None:
    """Return the response identity saved with an earlier bound opinion."""
    binding = bindings.get(_pair_id(evidence))
    if not isinstance(binding, Mapping):
        return None
    observed = binding.get("observed_model")
    return str(observed) if observed else None


def curation_bound_action(
        bindings: Mapping[str, Any], evidence: PairEvidence,
) -> str | None:
    """Return the saved lifecycle state for an earlier bound opinion."""
    binding = bindings.get(_pair_id(evidence))
    if isinstance(binding, str):
        return "settled"
    if not isinstance(binding, Mapping):
        return None
    return str(binding.get("action") or "settled")


def curation_judgment_saved(service, evidence: PairEvidence) -> bool:
    """Caller holds the service lock. False once ``review_rejudge`` deleted
    the pair's judgment row, so an opinion it forgot is not acted on.

    The row is keyed by the evidence keys, which keep a literal ``|`` that
    the duplicate listing's ``a_key``/``b_key`` fold to ``-``.
    """
    key = tuple(sorted((evidence.a["key"], evidence.b["key"])))
    return key in service._storage.curation_judgments(evidence.store)


def record_bound_curation_judgment(
        service, evidence: PairEvidence, *, verdict: Mapping[str, Any],
        policy_fingerprint: str, model: str | None, at: float,
        observed_model: str | None = None,
        requested_policy_fingerprint: str | None = None,
        action_state: str = "settled",
        locked: bool = False,
) -> bool:
    """Revalidate both rows, then persist the opinion and its binding together."""
    guard = nullcontext() if locked else service._lock
    with guard:
        if _resident_evidence_state(service, evidence) != "current":
            return False
        storage = service._storage
        with slot_curation_transaction(storage):
            if _locked_evidence_state(storage, evidence) != "current":
                return False
            storage.record_curation_judgment(
                evidence.store, evidence.a["key"], evidence.b["key"],
                verdict=str(verdict.get("verdict") or "leave"),
                keep=verdict.get("keep"), fold=verdict.get("fold"),
                confidence=verdict.get("confidence"), note=verdict.get("note"),
                model=model, at=at)
            bindings = curation_judgment_bindings(storage)
            bindings[_pair_id(evidence)] = {
                "signature": _judgment_signature(evidence, policy_fingerprint),
                "action": action_state,
                "observed_model": observed_model,
                "requested_policy": requested_policy_fingerprint,
            }
            storage.set_meta(_JUDGMENT_BINDINGS_META, bindings)
        return True


def _set_action_state(storage, evidence: PairEvidence, state: str) -> None:
    bindings = curation_judgment_bindings(storage)
    binding = bindings.get(_pair_id(evidence))
    if not isinstance(binding, Mapping):
        raise RuntimeError("curation judgment binding is missing")
    bindings[_pair_id(evidence)] = {**binding, "action": state}
    storage.set_meta(_JUDGMENT_BINDINGS_META, bindings)


def settle_curation_judgment_action(
        storage, evidence: PairEvidence, state: str,
) -> None:
    """Mark a non-mutating/manual action outcome durably."""
    with slot_curation_transaction(storage):
        _set_action_state(storage, evidence, state)


def _dismissal_names(evidence: PairEvidence) -> tuple[str, str]:
    return tuple(sorted((
        f"{evidence.store}:{evidence.a['key']}",
        f"{evidence.store}:{evidence.b['key']}",
    )))


def apply_auto_distinct(service, evidence: PairEvidence, *,
                        policy_guard=None, settle_judgment: bool = False,
                        ) -> dict[str, Any]:
    """Dismiss one exact evidence version; changed inputs become visible again."""
    with service._lock:
        service._ensure_init()
        if policy_guard is not None and not policy_guard():
            return {"dismissed": False, "reason": "policy_changed"}
        if _resident_evidence_state(service, evidence) != "current":
            return {"dismissed": False, "reason": "evidence_changed"}
        storage = service._storage
        with slot_curation_transaction(storage):
            if _locked_evidence_state(storage, evidence) != "current":
                return {"dismissed": False, "reason": "evidence_changed"}
            binding = curation_judgment_bindings(storage).get(
                _pair_id(evidence))
            if settle_judgment and (
                    not isinstance(binding, Mapping)
                    or not binding.get("requested_policy")
                    or not binding.get("observed_model")):
                return {"dismissed": False,
                        "reason": "unbound_judgment_policy"}
            bound = binding if isinstance(binding, Mapping) else {}
            a, b = _dismissal_names(evidence)
            new = storage.dismiss_pair(a, b)
            markers = dict(storage.get_meta(_AUTO_DISMISSALS_META) or {})
            # A pre-existing unmarked row is a human verdict.  Do not relabel
            # it as automatic merely because a judge reached the same answer.
            if new or _pair_id(evidence) in markers:
                marker_id = _pair_id(evidence)
                markers[marker_id] = {
                    "store": evidence.store,
                    "a": {k: evidence.a[k] for k in (
                        "key", "entity_norm", "attribute_norm")},
                    "b": {k: evidence.b[k] for k in (
                        "key", "entity_norm", "attribute_norm")},
                    "a_fingerprint": evidence.a_fingerprint,
                    "b_fingerprint": evidence.b_fingerprint,
                    "requested_policy": bound.get("requested_policy"),
                    "observed_model": bound.get("observed_model"),
                }
                storage.set_meta(_AUTO_DISMISSALS_META, markers)
                # The new marker already matches this response. Start the
                # next bounded policy scan after it, so an older marker gets
                # the next slot even when the cap is one.
                storage.set_meta(_AUTO_REFRESH_CURSOR_META, marker_id)
            if settle_judgment:
                _set_action_state(storage, evidence, "applied")
        return {"dismissed": True, "new": new, "store": evidence.store,
                "a_key": evidence.a["key"], "b_key": evidence.b["key"]}


def mark_human_curation_dismissal(
        storage, store: str, a_key: str, b_key: str,
) -> bool:
    """Persist a human dismissal and remove any automatic-version marker."""
    target = sorted((a_key, b_key))
    with slot_curation_transaction(storage):
        new = storage.dismiss_pair(f"{store}:{a_key}", f"{store}:{b_key}")
        markers = dict(storage.get_meta(_AUTO_DISMISSALS_META) or {})
        for marker_id, marker in list(markers.items()):
            if (marker.get("store") == store
                    and sorted(((marker.get("a") or {}).get("key"),
                                (marker.get("b") or {}).get("key"))) == target):
                markers.pop(marker_id, None)
        storage.set_meta(_AUTO_DISMISSALS_META, markers)
    return new


def has_auto_dismissals(storage) -> bool:
    """Cheap marker check used before constructing a curation judge."""
    return bool(storage.get_meta(_AUTO_DISMISSALS_META) or {})


def refresh_auto_dismissals(
        service, *, locked: bool = False,
        requested_policy: str | None = None,
        observed_model: str | None = None,
        response_observed_model: str | None = None,
        check_policy: bool = False,
        limit: int | None = None,
) -> int:
    """Reopen automatic dismissals invalidated by evidence or known policy.

    UI callers omit ``check_policy`` and therefore perform no model lookup.
    Judge callers supply the current requested-policy fingerprint and may
    supply a positively observed model. An unknown current identity never
    proves a stored identity changed.
    """
    guard = nullcontext() if locked else service._lock
    with guard:
        storage = service._storage
        markers = dict(storage.get_meta(_AUTO_DISMISSALS_META) or {})
        if not markers:
            return 0
        current = {
            store: {_slot(snapshot_record(store, rec)): snapshot_record(store, rec)
                    for rec in resident.current_records()}
            for store, resident in (("lesson", service._lessons),
                                    ("world", service._world))
        }
        marker_ids = sorted(markers)
        if check_policy and limit is not None:
            cap = max(1, int(limit))
            cursor = storage.get_meta(_AUTO_REFRESH_CURSOR_META)
            cursor = str(cursor or "")
            after = [marker_id for marker_id in marker_ids if marker_id > cursor]
            before = [marker_id for marker_id in marker_ids if marker_id <= cursor]
            marker_ids = (after + before)[:cap]
        stale = []
        for marker_id in marker_ids:
            marker = markers[marker_id]
            store = marker.get("store")
            if store not in current:
                stale.append((marker_id, marker))
                continue
            a, b = marker.get("a") or {}, marker.get("b") or {}
            live_a = current[store].get(_slot(a)) if a else None
            live_b = current[store].get(_slot(b)) if b else None
            if (live_a is None or live_b is None
                    or evidence_fingerprint(live_a) != marker.get("a_fingerprint")
                    or evidence_fingerprint(live_b) != marker.get("b_fingerprint")):
                stale.append((marker_id, marker))
                continue
            if (check_policy
                    and marker.get("requested_policy") != requested_policy):
                stale.append((marker_id, marker))
                continue
            known_models = [model for model in (
                observed_model, response_observed_model) if model is not None]
            if (check_policy and any(
                    marker.get("observed_model") != model
                    for model in known_models)):
                stale.append((marker_id, marker))
        advance_cursor = check_policy and bool(marker_ids)
        if not stale and not advance_cursor:
            return 0
        with slot_curation_transaction(storage):
            for marker_id, marker in stale:
                a = (marker.get("a") or {}).get("key")
                b = (marker.get("b") or {}).get("key")
                store = marker.get("store")
                if store in ("lesson", "world") and a and b:
                    left, right = sorted((f"{store}:{a}", f"{store}:{b}"))
                    storage.conn.execute(
                        "DELETE FROM dismissed_pairs WHERE a_norm=%s AND b_norm=%s",
                        (left, right))
                markers.pop(marker_id, None)
            storage.set_meta(_AUTO_DISMISSALS_META, markers)
            if advance_cursor:
                storage.set_meta(_AUTO_REFRESH_CURSOR_META, marker_ids[-1])
        return len(stale)


def _resident_records(service, store: str) -> dict[tuple[str, str], object]:
    resident = service._lessons if store == "lesson" else service._world
    return {_slot(snapshot_record(store, rec)): rec
            for rec in resident.current_records()}


def _slot(snapshot: Mapping[str, Any]) -> tuple[str, str]:
    return (str(snapshot["entity_norm"]), str(snapshot["attribute_norm"]))


def _resident_evidence_state(service, evidence: PairEvidence) -> str:
    current = _resident_records(service, evidence.store)
    a, b = current.get(_slot(evidence.a)), current.get(_slot(evidence.b))
    if a is None or b is None:
        return "missing"
    if (evidence_fingerprint(snapshot_record(evidence.store, a))
            != evidence.a_fingerprint
            or evidence_fingerprint(snapshot_record(evidence.store, b))
            != evidence.b_fingerprint):
        return "changed"
    return "current"


@contextmanager
def slot_curation_transaction(storage):
    """Pinned durable boundary used by the isolated curation module."""
    with storage.transaction():
        yield


def _locked_records(
        storage, evidence: PairEvidence,
) -> dict[tuple[str, str], dict[str, Any]]:
    from pseudolife_memory.storage import postgres as pg

    table = "lessons" if evidence.store == "lesson" else "world_facts"
    columns = (("id",) + pg._LESSON_COLS if evidence.store == "lesson"
               else ("id",) + pg._WORLD_FACT_COLS)
    keys = [_slot(evidence.a), _slot(evidence.b)]
    rows = storage.conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} "
        "WHERE status = 'current' AND "
        "((entity_norm = %s AND attribute_norm = %s) OR "
        " (entity_norm = %s AND attribute_norm = %s)) FOR UPDATE",
        (*keys[0], *keys[1]),
    ).fetchall()
    wanted = {_slot(evidence.a), _slot(evidence.b)}
    out = {}
    for values in rows:
        row = dict(zip(columns, values))
        if row.get("status") != "current":
            continue
        snap = snapshot_record(evidence.store, row)
        if _slot(snap) in wanted:
            out[_slot(snap)] = snap
    return out


def _locked_evidence_state(storage, evidence: PairEvidence) -> str:
    rows = _locked_records(storage, evidence)
    a, b = rows.get(_slot(evidence.a)), rows.get(_slot(evidence.b))
    if a is None or b is None:
        return "missing"
    if (evidence_fingerprint(a) != evidence.a_fingerprint
            or evidence_fingerprint(b) != evidence.b_fingerprint):
        return "changed"
    return "current"


def _embedding_values(value: Any) -> list | None:
    if value is None:
        return None
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    elif hasattr(value, "to_numpy"):
        value = value.to_numpy()
    return np.asarray(value).tolist()


def _audit_snapshot(store: str, retired: object,
                    original_fingerprint: str, evidence: PairEvidence,
                    keep: str) -> dict[str, Any]:
    snap = snapshot_record(store, retired)
    retired_fingerprint = evidence_fingerprint(snap)
    snap["_curation_evidence_fingerprint"] = original_fingerprint
    snap["_curation_retired_fingerprint"] = retired_fingerprint
    snap["_curation_duplicate"] = {
        "version": 1,
        "keep": keep,
        "a": evidence.a,
        "b": evidence.b,
        "a_fingerprint": evidence.a_fingerprint,
        "b_fingerprint": evidence.b_fingerprint,
        "loser_embedding": _embedding_values(getattr(retired, "embedding", None)),
    }
    snap.pop("embedding", None)
    if store == "lesson":
        snap.update(task=snap.pop("entity"), aspect=snap.pop("attribute"),
                    lesson=snap.pop("value"))
    return snap


def _stage_lesson(service, evidence: PairEvidence, keep: str, now: float):
    original = service._lessons
    survivor_key = _slot(evidence.a if keep == "a" else evidence.b)
    loser_key = _slot(evidence.b if keep == "a" else evidence.a)
    current = _resident_records(service, "lesson")
    survivor, loser = current[survivor_key], current[loser_key]
    survivor_stage = deepcopy(original)
    # Re-write the selected survivor unchanged in the same transaction. This
    # proves it remained durable without merging metadata that an undo could
    # not subtract later. The loser's full evidence stays in its retired row
    # and FK-free audit snapshot.
    survivor_stage.dirty_slots |= {survivor.key}

    final_stage = deepcopy(survivor_stage)
    retired = final_stage.retire(loser.entity, loser.attribute, now=now)
    if len(retired) != 1:
        raise RuntimeError("safe fold could not retire exactly one loser")
    final_stage.dirty_slots = {loser.key}
    return survivor_stage, final_stage, retired[0]


def _stage_world(service, evidence: PairEvidence, keep: str, now: float):
    survivor_key = _slot(evidence.a if keep == "a" else evidence.b)
    loser_key = _slot(evidence.b if keep == "a" else evidence.a)
    current = _resident_records(service, "world")
    survivor = current[survivor_key]
    loser = current[loser_key]
    survivor_stage = deepcopy(service._world)
    survivor_stage.dirty_slots |= {survivor.key}
    final_stage = deepcopy(survivor_stage)
    retired = final_stage.retire(loser.entity, loser.attribute, now=now)
    if len(retired) != 1:
        raise RuntimeError("safe fold could not retire exactly one loser")
    final_stage.dirty_slots = {loser.key}
    return survivor_stage, final_stage, retired[0]


@dataclass(frozen=True)
class PendingCurationRecovery:
    evidence: PairEvidence
    keep: str
    expected_survivor_fingerprint: str
    rollback_store: object | None = None

    @classmethod
    def for_evidence(cls, evidence: PairEvidence, *, keep: str,
                     expected_survivor: Mapping[str, Any],
                     rollback_store: object | None = None):
        return cls(evidence, keep, evidence_fingerprint(expected_survivor),
                   rollback_store)


@dataclass(frozen=True)
class PendingCurationRestore:
    evidence: PairEvidence
    keep: str
    rollback_store: object
    retire_decision_id: int


def _publish_store(service, store: str, resident) -> None:
    if store == "lesson":
        service._lessons = resident
    else:
        service._world = resident


def _hydrate_store(service, store: str):
    if store == "lesson":
        from pseudolife_memory.memory.lessons import LessonStore
        from pseudolife_memory.storage.sync import hydrate_lessons
        resident = LessonStore()
        hydrate_lessons(resident, service._storage)
    else:
        from pseudolife_memory.memory.world_cortex import WorldCortexStore
        from pseudolife_memory.storage.sync import hydrate_world_cortex
        resident = WorldCortexStore()
        hydrate_world_cortex(resident, service._storage)
    return resident


def _recovery_state(service, pending: PendingCurationRecovery, resident) -> str:
    evidence, keep = pending.evidence, pending.keep
    rows = {_slot(snapshot_record(evidence.store, r)): r
            for r in resident.current_records()}
    a, b = rows.get(_slot(evidence.a)), rows.get(_slot(evidence.b))
    before = (a is not None and b is not None
              and evidence_fingerprint(snapshot_record(evidence.store, a))
              == evidence.a_fingerprint
              and evidence_fingerprint(snapshot_record(evidence.store, b))
              == evidence.b_fingerprint)
    if before:
        return "rolled_back"
    survivor_snap = evidence.a if keep == "a" else evidence.b
    loser_snap = evidence.b if keep == "a" else evidence.a
    survivor = rows.get(_slot(survivor_snap))
    loser = rows.get(_slot(loser_snap))
    after = (survivor is not None and loser is None
             and evidence_fingerprint(snapshot_record(evidence.store, survivor))
             == pending.expected_survivor_fingerprint)
    if not after:
        raise CurationReconciliationError(
            "durable curation state is neither before nor after the decision")
    decisions = service._storage.store_decisions(
        evidence.store,
        entity_norm=loser_snap["entity_norm"],
        attribute_norm=loser_snap["attribute_norm"], limit=1)
    if (not decisions or decisions[0].get("action") != "retire"
            or (decisions[0].get("record") or {}).get(
                "_curation_evidence_fingerprint")
            != (evidence.b_fingerprint if keep == "a"
                else evidence.a_fingerprint)):
        raise CurationReconciliationError(
            "durable curation retirement is missing its matching audit")
    return "committed"


def _latest_slot_decision(service, evidence: PairEvidence, side: str):
    snap = evidence.a if side == "a" else evidence.b
    rows = service._storage.store_decisions(
        evidence.store, entity_norm=snap["entity_norm"],
        attribute_norm=snap["attribute_norm"], limit=1)
    return rows[0] if rows else None


def _restore_recovery_state(
        service, pending: PendingCurationRestore, resident,
) -> str:
    evidence = pending.evidence
    rows = {_slot(snapshot_record(evidence.store, r)): r
            for r in resident.current_records()}
    keep = pending.keep
    survivor_snap = evidence.a if keep == "a" else evidence.b
    loser_snap = evidence.b if keep == "a" else evidence.a
    survivor = rows.get(_slot(survivor_snap))
    loser = rows.get(_slot(loser_snap))
    survivor_matches = (
        survivor is not None
        and evidence_fingerprint(snapshot_record(evidence.store, survivor))
        == evidence_fingerprint(survivor_snap))
    if not survivor_matches:
        raise CurationReconciliationError(
            "durable curation restore changed the survivor")
    latest = _latest_slot_decision(
        service, evidence, "b" if keep == "a" else "a")
    if loser is None:
        if (latest and latest.get("id") == pending.retire_decision_id
                and latest.get("action") == "retire"):
            return "rolled_back"
    elif (evidence_fingerprint(snapshot_record(evidence.store, loser))
          == evidence_fingerprint(loser_snap)):
        record = (latest or {}).get("record") or {}
        if (latest and latest.get("action") == "restore"
                and record.get("_curation_undo_of")
                == pending.retire_decision_id):
            return "committed"
    raise CurationReconciliationError(
        "durable curation restore is neither before nor after the decision")


def _recover_slot_curation_locked(service) -> str | None:
    pending = getattr(service, "_slot_curation_recovery", None)
    if pending is None:
        return None
    try:
        resident = _hydrate_store(service, pending.evidence.store)
        if isinstance(pending, PendingCurationRestore):
            state = _restore_recovery_state(service, pending, resident)
        else:
            state = _recovery_state(service, pending, resident)
    except CurationReconciliationError:
        raise
    except Exception as exc:
        raise CurationReconciliationError(
            f"slot curation commit reconciliation required: {exc}") from exc
    if state == "rolled_back" and pending.rollback_store is not None:
        resident = pending.rollback_store
    _publish_store(service, pending.evidence.store, resident)
    service._slot_curation_recovery = None
    return state


def _pair_from_retire_audit(store: str, decision: Mapping[str, Any]):
    record = decision.get("record") or {}
    curation = record.get("_curation_duplicate")
    if not isinstance(curation, Mapping):
        return None
    try:
        evidence = PairEvidence(
            store, dict(curation["a"]), dict(curation["b"]),
            str(curation["a_fingerprint"]), str(curation["b_fingerprint"]))
        keep = str(curation["keep"])
    except (KeyError, TypeError, ValueError):
        return None
    if keep not in ("a", "b"):
        return None
    # PairEvidence is frozen and intentionally contains evidence only. Return
    # direction separately rather than smuggling mutable action state into it.
    return evidence, keep, curation


def _record_from_snapshot(store: str, snap: Mapping[str, Any], embedding):
    import torch

    vector = (torch.as_tensor(embedding, dtype=torch.float32)
              if embedding is not None else None)
    common = dict(
        entity=snap["entity"], attribute=snap["attribute"], value=snap["value"],
        polarity=snap.get("polarity") or "+",
        confidence=float(snap.get("confidence") or 0.0), status="current",
        asserted_at=float(snap.get("asserted_at") or 0.0),
        last_confirmed=float(snap.get("last_confirmed") or 0.0),
        supersedes_value=snap.get("supersedes_value"),
        superseded_by_value=snap.get("superseded_by_value"),
        superseded_at=snap.get("superseded_at"), embedding=vector,
        tx_time=snap.get("tx_time"), valid_time=snap.get("valid_time"),
        hlc_phys=snap.get("hlc_phys"), hlc_logical=snap.get("hlc_logical"),
        writer_id=snap.get("writer_id"), session_id=snap.get("session_id"),
        version=int(snap.get("version") or 1),
    )
    if store == "lesson":
        from pseudolife_memory.memory.lessons import LessonRecord
        return LessonRecord(
            **common, about=snap.get("about"),
            outcome=snap.get("outcome") or "success",
            origin=snap.get("origin"),
            support=set(snap.get("support") or []),
            provenance=set(snap.get("provenance") or []))
    from pseudolife_memory.memory.world_cortex import WorldRecord
    return WorldRecord(
        **common, source_url=snap.get("source_url") or "",
        source_quote=snap.get("source_quote") or "",
        freshness_class=snap.get("freshness_class") or "volatile",
        retrieved_at=float(snap.get("retrieved_at") or 0.0),
        content_hash=snap.get("content_hash"),
        source_doc_id=snap.get("source_doc_id"))


def _stage_restore(service, evidence: PairEvidence, keep: str, curation):
    store = evidence.store
    resident = service._lessons if store == "lesson" else service._world
    staged = deepcopy(resident)
    loser_snap = evidence.b if keep == "a" else evidence.a
    loser = next((r for r in staged.records
                  if r.key == _slot(loser_snap) and r.status == "retired"), None)
    source = "retired_record"
    restored = staged.restore(loser_snap["entity"], loser_snap["attribute"])
    if restored:
        record = restored[0]
        if evidence_fingerprint(snapshot_record(store, record)) != \
                evidence_fingerprint(loser_snap):
            raise CurationReconciliationError(
                "retired curation record no longer matches its pre-image")
    else:
        if loser is not None:
            raise CurationReconciliationError("curation loser could not be restored")
        record = _record_from_snapshot(
            store, loser_snap, curation.get("loser_embedding"))
        staged.records.append(record)
        staged._current[record.key] = len(staged.records) - 1
        staged.dirty_slots.add(record.key)
        source = "audit_snapshot"
    return staged, record, source


def _targeted_all_rows(storage, evidence: PairEvidence):
    from pseudolife_memory.storage import postgres as pg

    table = "lessons" if evidence.store == "lesson" else "world_facts"
    columns = (("id",) + pg._LESSON_COLS if evidence.store == "lesson"
               else ("id",) + pg._WORLD_FACT_COLS)
    keys = [_slot(evidence.a), _slot(evidence.b)]
    values = storage.conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE "
        "((entity_norm = %s AND attribute_norm = %s) OR "
        " (entity_norm = %s AND attribute_norm = %s)) FOR UPDATE",
        (*keys[0], *keys[1])).fetchall()
    return [dict(zip(columns, row)) for row in values]


def _validate_restore_locked(
        storage, evidence: PairEvidence, keep: str,
        retire_decision: Mapping[str, Any],
) -> bool:
    rows = _targeted_all_rows(storage, evidence)
    snapshots = [snapshot_record(evidence.store, row) for row in rows]
    survivor_snap = evidence.a if keep == "a" else evidence.b
    loser_snap = evidence.b if keep == "a" else evidence.a
    survivor = next((row for row in snapshots
                     if _slot(row) == _slot(survivor_snap)
                     and row["status"] == "current"), None)
    loser_current = next((row for row in snapshots
                          if _slot(row) == _slot(loser_snap)
                          and row["status"] == "current"), None)
    if (survivor is None or loser_current is not None
            or evidence_fingerprint(survivor)
            != evidence_fingerprint(survivor_snap)):
        return False
    expected_retired = (retire_decision.get("record") or {}).get(
        "_curation_retired_fingerprint")
    retired = [row for row in snapshots
               if _slot(row) == _slot(loser_snap) and row["status"] == "retired"]
    if retired and expected_retired not in {
            evidence_fingerprint(row) for row in retired}:
        return False
    locked = storage.conn.execute(
        "SELECT action, record FROM store_decisions WHERE id=%s FOR UPDATE",
        (retire_decision["id"],)).fetchone()
    return bool(locked and locked[0] == "retire"
                and (locked[1] or {}).get("_curation_duplicate"))


def _restore_decision_snapshot(store: str, record: object,
                               retire_decision_id: int) -> dict[str, Any]:
    snap = snapshot_record(store, record)
    snap["_curation_undo_of"] = int(retire_decision_id)
    snap.pop("embedding", None)
    if store == "lesson":
        snap.update(task=snap.pop("entity"), aspect=snap.pop("attribute"),
                    lesson=snap.pop("value"))
    return snap


def restore_curated_duplicate(
        service, store: str, entity: str, attribute: str | None, *,
        decided_by: str, locked: bool = False,
) -> dict[str, Any] | None:
    """Atomically undo an automatic duplicate retirement when one is targeted."""
    guard = nullcontext() if locked else service._lock
    with guard:
        requested_attribute = attribute
        ne = _norm_key(entity)
        decisions = service._storage.retired_slots(store, entity_norm=ne)
        if attribute is None:
            curated = [(row, _pair_from_retire_audit(store, row))
                       for row in decisions]
            curated = [(row, parsed) for row, parsed in curated
                       if parsed is not None]
            if not curated:
                return None
            # A one-slot whole-entity request is unambiguous and can use the
            # exact same atomic path.  Mixed/multi-slot requests fail closed:
            # executing only the curated subset would make a whole-entity
            # restore silently partial, while restoring all rows through the
            # legacy split RAM/audit/save path would lose atomicity.
            if len(decisions) != 1 or len(curated) != 1:
                return {"restored": 0,
                        "reason": "curation_restore_requires_attribute"}
            decision, parsed = curated[0]
            na = str(decision["attribute_norm"])
        else:
            na = _norm_key(attribute)
            decision = next((row for row in decisions
                             if row["attribute_norm"] == na), None)
            parsed = _pair_from_retire_audit(store, decision or {})
        if parsed is None:
            return None
        evidence, keep, curation = parsed
        loser_snap = evidence.b if keep == "a" else evidence.a
        if _slot(loser_snap) != (ne, na):
            return {"restored": 0, "reason": "curation_target_mismatch"}
        current = _resident_records(service, store)
        survivor_snap = evidence.a if keep == "a" else evidence.b
        survivor = current.get(_slot(survivor_snap))
        if (survivor is None
                or evidence_fingerprint(snapshot_record(store, survivor))
                != evidence_fingerprint(survivor_snap)
                or _slot(loser_snap) in current):
            return {"restored": 0, "reason": "curation_evidence_changed"}
        rollback_store = deepcopy(
            service._lessons if store == "lesson" else service._world)
        staged, restored, source = _stage_restore(
            service, evidence, keep, curation)
        stamp = time.time()
        pending = PendingCurationRestore(
            evidence, keep, rollback_store, int(decision["id"]))
        try:
            with slot_curation_transaction(service._storage):
                if not _validate_restore_locked(
                        service._storage, evidence, keep, decision):
                    return {"restored": 0, "reason": "curation_evidence_changed"}
                if store == "lesson":
                    from pseudolife_memory.storage.sync import sync_lesson_slots
                    sync_lesson_slots(staged, service._storage)
                else:
                    from pseudolife_memory.storage.sync import sync_world_slots
                    sync_world_slots(staged, service._storage)
                service._storage.record_store_decision(
                    store, ne, na, "restore", decided_by=decided_by, reason=None,
                    record=_restore_decision_snapshot(
                        store, restored, int(decision["id"])), now=stamp)
        except Exception as exc:
            service._slot_curation_recovery = pending
            try:
                state = _recover_slot_curation_locked(service)
            except CurationReconciliationError:
                raise
            if state != "committed":
                raise CurationReconciliationError(
                    f"curation restore rolled back: {exc}") from exc
            source = "reconciled"
        else:
            staged.dirty_slots.clear()
            _publish_store(service, store, staged)
            service._slot_curation_recovery = None
        if store == "lesson":
            from pseudolife_memory.service import _lesson_record_to_dict
            entry = _lesson_record_to_dict(restored)
            label = {"task": entity, "aspect": requested_attribute}
        else:
            from pseudolife_memory.service import _world_record_to_dict
            entry = _world_record_to_dict(
                restored, stale_policy=service._stale_policy)
            label = {"entity": entity, "attribute": requested_attribute}
        return {"restored": 1, "source": source, **label, "entries": [entry]}


def recover_slot_curation(service, *, locked: bool = False) -> str | None:
    """Resolve an uncertain commit before an ordinary store operation.

    Pass ``locked=True`` only when the caller already owns the service's
    non-reentrant lock (for example from ``MemoryService._ensure_init``).
    """
    if locked:
        return _recover_slot_curation_locked(service)
    with service._lock:
        return _recover_slot_curation_locked(service)


def apply_slot_duplicate(service, evidence: PairEvidence, *,
                         verdict: Mapping[str, Any] | None = None,
                         keep: str | None = None, reason: str | None = None,
                         now: float | None = None, policy_guard=None,
                         settle_judgment: bool = False) -> dict[str, Any]:
    """Apply one safe duplicate decision atomically, or return why it refused."""
    verdict = dict(verdict or {"verdict": "duplicate", "keep": keep,
                               "fold": None})
    decision = can_auto_fold(evidence, verdict)
    if not decision.allowed:
        return {"applied": False, "reason": decision.reason}
    assert decision.keep is not None
    with service._lock:
        service._ensure_init()
        if policy_guard is not None and not policy_guard():
            return {"applied": False, "reason": "policy_changed"}
        try:
            _recover_slot_curation_locked(service)
        except CurationReconciliationError:
            return {"applied": False, "reason": "reconciliation_required"}
        state = _resident_evidence_state(service, evidence)
        if state != "current":
            return {"applied": False, "reason": f"evidence_{state}"}

        stamp = max(
            float(now if now is not None else time.time()),
            float(evidence.a.get("last_confirmed") or 0.0),
            float(evidence.b.get("last_confirmed") or 0.0),
        )
        try:
            rollback_store = deepcopy(
                service._lessons if evidence.store == "lesson" else service._world)
            if evidence.store == "lesson":
                survivor_stage, final_stage, retired = _stage_lesson(
                    service, evidence, decision.keep, stamp)
            else:
                survivor_stage, final_stage, retired = _stage_world(
                    service, evidence, decision.keep, stamp)
        except Exception:
            return {"applied": False, "reason": "staging_error"}

        expected = (evidence.a if decision.keep == "a" else evidence.b)
        if survivor_stage is not None:
            survivor_key = _slot(expected)
            expected_rec = {_slot(snapshot_record(evidence.store, r)): r
                            for r in survivor_stage.current_records()}[survivor_key]
            expected = snapshot_record(evidence.store, expected_rec)
        pending = PendingCurationRecovery.for_evidence(
            evidence, keep=decision.keep, expected_survivor=expected,
            rollback_store=rollback_store)

        try:
            with slot_curation_transaction(service._storage):
                locked = _locked_evidence_state(service._storage, evidence)
                if locked != "current":
                    raise _LockedEvidenceChanged(locked)
                if evidence.store == "lesson":
                    from pseudolife_memory.storage.sync import sync_lesson_slots
                    sync_lesson_slots(survivor_stage, service._storage)
                    sync_lesson_slots(final_stage, service._storage)
                else:
                    from pseudolife_memory.storage.sync import sync_world_slots
                    sync_world_slots(survivor_stage, service._storage)
                    sync_world_slots(final_stage, service._storage)
                loser_fp = (evidence.b_fingerprint if decision.keep == "a"
                            else evidence.a_fingerprint)
                ne, na = retired.key
                service._storage.record_store_decision(
                    evidence.store, ne, na, "retire",
                    decided_by="dream-judge", reason=reason,
                    record=_audit_snapshot(
                        evidence.store, retired, loser_fp, evidence,
                        decision.keep),
                    now=stamp)
                if settle_judgment:
                    _set_action_state(service._storage, evidence, "applied")
        except _LockedEvidenceChanged as exc:
            try:
                _publish_store(service, evidence.store,
                               _hydrate_store(service, evidence.store))
            except Exception:
                pass
            return {"applied": False, "reason": f"evidence_{exc}"}
        except Exception:
            service._slot_curation_recovery = pending
            try:
                recovered = _recover_slot_curation_locked(service)
            except CurationReconciliationError:
                return {"applied": False, "reason": "reconciliation_required"}
            if recovered == "committed":
                return {"applied": True, "kind": decision.kind,
                        "reconciled": "committed"}
            return {"applied": False, "reason": "persistence_error",
                    "reconciled": "rolled_back"}

        final_stage.dirty_slots.clear()
        _publish_store(service, evidence.store, final_stage)
        service._slot_curation_recovery = None
        return {"applied": True, "kind": decision.kind}
