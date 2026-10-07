"""Audit recorded supersession pairs without models or a live memory bank.

    python evals/audit_supersessions.py --snapshot snapshot.json --out-dir audit

The output directory must be outside a Git checkout. Existing output files are
never overwritten. Share manual-sample.json with reviewers, withholding the
separate sample-key.json and detector diagnostics until labels are complete.

Exact-text linkage describes surviving snapshot rows, not historical causality.
Replay uses saved vectors/slots and today's real detector on CPU; it neither
re-embeds text nor replays NLI or the original store/consolidation operation.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys

# This utility must never compete with an active GPU workload or fetch a model.
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402

from pseudolife_memory.memory import contradiction as detector  # noqa: E402
from pseudolife_memory.memory.titans_memory import MemoryEntry  # noqa: E402


OUTPUT_FILES = {
    "summary": "summary.json", "pairs": "pairs.json",
    "sample": "manual-sample.json", "sample_key": "sample-key.json",
}


def _vector(value):
    if not isinstance(value, list) or not value:
        raise ValueError("missing_or_empty_embedding")
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) for v in value):
        raise ValueError("invalid_embedding_values")
    vector = torch.tensor(value, dtype=torch.float32, device="cpu")
    norm = torch.linalg.vector_norm(vector)
    if (not torch.isfinite(vector).all() or not torch.isfinite(norm)
            or not norm > 0):
        raise ValueError("nonfinite_or_zero_embedding")
    return vector


def _slots(value):
    if not isinstance(value, list) or any(
            not isinstance(s, (list, tuple)) or len(s) != 4
            or not all(isinstance(v, str) for v in s) for s in value):
        raise ValueError("invalid_slots")
    return [tuple(s) for s in value]


def _explain(new_text, old, new_slots, cosine):
    """Infer qualifying paths from shared helpers; the real detector is decisive.

    These predicates explain, rather than replace, detect_contradictions. A
    disagreement is persisted so a changed detector cannot silently turn this
    explanation into the reported replay verdict. Multiple predicates can qualify;
    their order is the detector's current short-circuit order, not proven history.
    """
    paths = []
    if new_slots and detector._slot_contradiction(new_slots, old.slots):
        paths.append("slot_identity")
    if (cosine >= detector.NEGATION_SIM_THRESHOLD
            and detector._negation_asymmetry(new_text, old.text)):
        paths.append("negation_asymmetry")
    if (cosine >= detector.REPLACEMENT_SIM_THRESHOLD
            and detector._looks_like_replacement(new_text, old.text)):
        paths.append("affirmative_replacement")
    anchor = detector._state_transition_anchor_kind(new_text, old.text)
    if (anchor is not None
            and cosine >= detector._STATE_TRANSITION_FLOOR_BY_KIND[anchor]):
        paths.append(f"state_transition_{anchor}")
    return paths


def _replay(old_row, new_row):
    try:
        old_vector = _vector(old_row.get("embedding"))
        new_vector = _vector(new_row.get("embedding"))
        if old_vector.shape != new_vector.shape:
            raise ValueError("embedding_dimension_mismatch")
        old = MemoryEntry(text=old_row["text"], embedding=old_vector,
                          slots=_slots(old_row.get("slots", [])))
        new_slots = _slots(new_row.get("slots", []))
    except (ValueError, TypeError, RuntimeError, OverflowError) as exc:
        return {"replay_error": str(exc)}

    # A fresh entry clears both the old supersession flag and its cue cache.
    # Use the same float32 matrix operation as the actual single-pair detector.
    cosine = float((F.normalize(old_vector.unsqueeze(0), p=2, dim=1)
                    @ F.normalize(new_vector.unsqueeze(0), p=2, dim=1)
                    .squeeze(0)).item())
    fires = bool(detector.detect_contradictions(
        new_row["text"], new_vector, [old], new_slots=new_slots,
        device="cpu", nli_scorer=None))
    paths = _explain(new_row["text"], old, new_slots, cosine)
    return {
        "current_detector_fires": fires, "cosine": cosine,
        "qualifying_paths": paths,
        "inferred_first_path": paths[0] if paths else None,
        "path_explanation_consistent": fires == bool(paths),
        "replay_error": None,
    }


def _group(old, new):
    sources = {old.get("source"), new.get("source") if new else None}
    if "status" in sources and "digest" in sources:
        return "status_and_digest"
    if "status" in sources:
        return "status"
    if "digest" in sources:
        return "digest"
    return "other"


def _counts(pairs):
    return {
        "recorded_supersessions": len(pairs),
        "resolutions": dict(sorted(Counter(p["resolution"] for p in pairs).items())),
        "replayed_pairs": sum(p["current_detector_fires"] is not None for p in pairs),
        "not_replayed_pairs": sum(p["current_detector_fires"] is None for p in pairs),
        "replay_errors": dict(sorted(Counter(
            p["replay_error"] for p in pairs if p["replay_error"]).items())),
        "detector_fires": sum(p["current_detector_fires"] is True for p in pairs),
        "detector_does_not_fire": sum(p["current_detector_fires"] is False for p in pairs),
        "path_explanation_mismatches": sum(
            p["path_explanation_consistent"] is False for p in pairs),
        "inferred_first_paths": dict(sorted(Counter(
            p["inferred_first_path"] for p in pairs
            if p["inferred_first_path"] is not None).items())),
    }


def audit_snapshot(snapshot, *, sample_per_stratum=8, seed=0):
    """Return summary, lineage diagnostics, blinded texts, and a separate key.

    Each recorded supersession is one audit unit. Missing/ambiguous links remain
    in counts and, when replacement text exists, manual sampling, but have no
    replay verdict. Sampling is stable
    under snapshot row reordering and stratifies by source relation and whether
    status/digest sources are involved; it does not stratify on detector labels.
    """
    if not isinstance(snapshot, dict) or snapshot.get("format_version") != 1:
        raise ValueError("snapshot must have format_version 1")
    rows = snapshot.get("entries")
    if not isinstance(rows, list) or not isinstance(snapshot.get("metadata", {}), dict):
        raise ValueError("snapshot requires entries list and metadata object")
    if sample_per_stratum < 0:
        raise ValueError("sample_per_stratum must be nonnegative")
    by_id, by_text = {}, defaultdict(list)
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("id")) is not int
                or not isinstance(row.get("text"), str)):
            raise ValueError("every entry requires an integer id and string text")
        if row["id"] in by_id:
            raise ValueError(f"duplicate entry id: {row['id']}")
        by_id[row["id"]] = row
        by_text[row["text"]].append(row)

    pairs = []
    for old in sorted(rows, key=lambda r: r["id"]):
        if old.get("superseded_at") is None:
            continue
        replacement = old.get("superseded_by_text")
        candidates = (sorted(by_text.get(replacement, []), key=lambda r: r["id"])
                      if isinstance(replacement, str) and replacement else [])
        resolution = ("missing_replacement_text" if not replacement else
                      "missing_exact_text" if not candidates else
                      "ambiguous_exact_text" if len(candidates) > 1 else
                      "self_reference" if candidates[0]["id"] == old["id"] else
                      "unique_exact_text")
        new = candidates[0] if resolution == "unique_exact_text" else None
        relation = ("unknown" if new is None or not old.get("source")
                    or not new.get("source") else
                    "same_source" if old["source"] == new["source"] else "cross_source")
        group = _group(old, new)
        pair = {
            "entry_id": old["id"], "superseder_id": new["id"] if new else None,
            "candidate_superseder_ids": [r["id"] for r in candidates],
            "resolution": resolution, "source": old.get("source"),
            "superseder_source": new.get("source") if new else None,
            "source_relation": relation, "source_group": group,
            "stratum": f"{relation}/{group}",
            "timestamp": old.get("timestamp"),
            "superseded_at": old["superseded_at"],
            "superseder_timestamp": new.get("timestamp") if new else None,
            "current_detector_fires": None, "qualifying_paths": [],
            "inferred_first_path": None, "path_explanation_consistent": None,
            "replay_error": None,
        }
        if new is not None:
            pair.update(_replay(old, new))
        pairs.append(pair)

    strata = defaultdict(list)
    for pair in pairs:
        strata[pair["stratum"]].append(pair)
    selected = []
    sampling_counts = {}

    def sample_order(pair):
        return hashlib.sha256(f"{seed}:{pair['entry_id']}".encode()).hexdigest()

    for stratum, members in strata.items():
        eligible = [p for p in members if isinstance(
            by_id[p["entry_id"]].get("superseded_by_text"), str)
            and by_id[p["entry_id"]]["superseded_by_text"]]
        chosen = sorted(eligible, key=sample_order)[:sample_per_stratum]
        sampling_counts[stratum] = {
            "eligible": len(eligible), "excluded": len(members) - len(eligible),
            "selected": len(chosen),
        }
        selected.extend(chosen)
    # Interleave strata without disclosing a stratum or detector verdict to raters.
    selected.sort(key=sample_order)
    sample, sample_key = [], []
    for i, pair in enumerate(selected, 1):
        sample_id = f"pair-{i:04d}"
        old = by_id[pair["entry_id"]]
        sample.append({"sample_id": sample_id, "earlier_text": old["text"],
                       "replacement_text": old["superseded_by_text"],
                       "label": None, "notes": ""})
        sample_key.append({"sample_id": sample_id, **pair})

    summary = {
        "format_version": 1, "snapshot_metadata": snapshot.get("metadata", {}),
        "snapshot_entries": len(rows), **_counts(pairs),
        "historical_cause_proven": False, "nli_replayed": False,
        "device": "cpu", "embedding_source": "persisted_snapshot_vectors",
        "slot_source": "persisted_snapshot_slots",
        "detector_sha256": hashlib.sha256(Path(detector.__file__).read_bytes()).hexdigest(),
        "path_attribution": "inferred_from_shared_helpers_checked_against_actual_detector",
        "strata": {k: {**_counts(v), "sampling": sampling_counts[k]}
                   for k, v in sorted(strata.items())},
        "sampling": {"seed": seed, "per_stratum": sample_per_stratum,
                     "eligible": sum(v["eligible"] for v in sampling_counts.values()),
                     "excluded": sum(v["excluded"] for v in sampling_counts.values()),
                     "selected": len(sample), "unit": "recorded_supersession"},
        "limitations": [
            "Exact-text matches identify surviving candidates, not the original writer or cause.",
            "Current non-NLI detector replay is not a historical store/consolidation replay.",
            "Historical vectors and slots are reused; text is not re-embedded or re-extracted.",
            "No detector label establishes whether a replacement was semantically correct.",
            "Missing, ambiguous, self-referential, or invalid-vector pairs have no replay verdict.",
            "Manual labels apply only to pairs with available replacement text; excluded pairs remain unknown.",
            "Missing linkage remains unknown and is never imputed from sampled labels.",
            "Equal allocation across strata is not a prevalence estimate; any label inference must use eligible stratum counts.",
        ],
    }
    return {"summary": summary, "pairs": pairs, "sample": sample, "sample_key": sample_key}


def _output_directory(path):
    directory = Path(path).resolve()
    if any((p / ".git").exists() for p in (directory, *directory.parents)):
        raise ValueError("audit output contains private memory text; choose a directory outside Git")
    if any((directory / name).exists() for name in OUTPUT_FILES.values()):
        raise ValueError("audit output already exists; choose a new directory")
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--sample-per-stratum", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        directory = _output_directory(args.out_dir)
        raw = args.snapshot.read_bytes()
        snapshot = json.loads(raw.decode("utf-8-sig"))
        report = audit_snapshot(snapshot, sample_per_stratum=args.sample_per_stratum,
                                seed=args.seed)
        report["summary"]["snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        for key, name in OUTPUT_FILES.items():
            with (directory / name).open("x", encoding="utf-8") as output:
                json.dump(report[key], output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
    except (OSError, ValueError, TypeError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    print(f"Audited {report['summary']['recorded_supersessions']} recorded supersessions; "
          f"replayed {report['summary']['replayed_pairs']}; sampled {len(report['sample'])}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
