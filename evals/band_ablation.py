"""8-band continuum vs single-table ablation — offline context rebuild.

Answers the 2026-07-17 architecture-critique question: does the multi-band
continuum (band-depth-modulated recency half-life in ranking) actually beat
ONE cosine table with a plain timestamp-recency term, on LongMemEval-KU?

The arm1 bank dumps (``banks/oracle-e4b-ft-arm1``) carry only cortex facts —
raw turns, band membership and timestamps were never dumped — so this module
first REPLAYS ingest CPU-only (``replay``: store every haystack turn exactly
as ``longmemeval_bench.ingest_and_dream`` does, but with dreaming SKIPPED —
no extractor call, embedder on CPU) and serialises the full band state per
question. ``rebuild`` then re-ranks the rag/hybrid raw-turn selection offline
from that serialised state under two policies × two timestamp modes and emits
four tagged JSONLs ready for the GPU answer phase (``replicate.py run``).

Policies
--------
* ``continuum`` — mirrors the CMS's actual Pool-1 ranking (cms.py; exact
  lines cited inline in :func:`select_topk`): per-band top-k cosine
  candidates, recency boost ramp ``0.4*(1-depth/(n-1))`` with geometric
  half-life ``3600*2**depth`` seconds, source/supersession multipliers,
  keep-gate at relevance>=0.25, plus the slot-token channel (Pool 1.5).
* ``flat`` — identical pipeline over ONE flat pool: global top-k cosine
  candidates, a single exponential recency term using the shallowest band's
  parameters (boost=0.4, half-life=``recency_base_half_life_s``=3600 s —
  config.py:588), same multipliers/gate/slot channel. The only difference
  from ``continuum`` is the band structure itself.

Timestamp modes
---------------
* ``wall`` — the served regime: entry timestamps are the replay's real
  store times and "now" is the replay's post-ingest search time. Ingest
  takes seconds, so age~0 and recency~1 everywhere: this mode isolates the
  boost-coefficient/pooling difference, NOT the half-life continuum.
* ``hist`` — the counterfactual that makes the recency lever real: each
  turn is stamped with its haystack session date (+60 s per turn to keep
  in-session order) and "now" is the question date. Ages span days-months,
  so the per-band half-life schedule actually differentiates.

Sanity gate: ``continuum``+``wall`` re-selection is compared against the
ORIGINALLY SERVED ``contexts.rag`` per question (agreement rate reported).
Divergence has two separable sources, both reported: replay-state drift
(the original run dreamed between sessions; we skip it — measured by the
REAL ``svc.search`` re-run recorded at replay time vs served) and mirror
drift (our offline formula vs the real code — measured mirror vs replay).

Usage (repo root, venv python; replay needs the bench Postgres at :5433):

    python evals/band_ablation.py replay              # CPU, ~seconds/question
    python evals/band_ablation.py rebuild --dry-run   # 3-question preview
    python evals/band_ablation.py rebuild             # write the 4 JSONLs

Then (GPU window): answer-phase the four ``arm1-abl-*`` tags — see the
commands printed at the end of ``rebuild``.

Write-side ablation (2026-07-24): the ranking ablation above holds the
INGEST fixed — both arms rank the same already-banded survivors. The
``--band-preset flat`` variant re-runs ingest through ONE flat band at the
continuum's total capacity (5,250), so eviction/promotion never partitions
by tier and different entries survive. Only meaningful on the ``s``
full-haystack dataset (~493 turns/question vs the 200-cap ``working``
entry band — the ``oracle`` corpus stores ~23 turns/question and never
evicts, making the write side a no-op there):

    python evals/band_ablation.py replay  --dataset s --extractor qwen-27b \\
        --src-tag "" --band-preset continuum      # baseline dumps
    python evals/band_ablation.py replay  --dataset s --extractor qwen-27b \\
        --src-tag "" --band-preset flat           # flat-ingest dumps
    python evals/band_ablation.py rebuild --dataset s --extractor qwen-27b \\
        --src-tag ""                              # abl-* tags (4)
    python evals/band_ablation.py rebuild --dataset s --extractor qwen-27b \\
        --src-tag "" --band-preset flat           # wabl-flat-* tags (2)
                                                  # + survival-stats JSON

``wabl-flat-M`` vs ``abl-flat-M`` isolates the write side (same flat
ranking, different survivor sets); vs ``abl-continuum-M`` is the
whole-system comparison.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")                # CPU only
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Bench constants mirrored from longmemeval_bench.py (not imported at module
# level — that module pulls in torch/ladder_sweep; replay imports it lazily).
RAG_TOP_K = 6
HYBRID_TOP_K = 3
_HYBRID_SPLIT = "\n\nRelevant memories:\n"
ARMS = ("rag", "cortex", "hybrid")

# ── CMS ranking constants, mirrored with line cites ───────────────────────
MIN_SCORE = 0.25              # cms.py:583 — relevance keep-threshold
ASSISTANT_SCORE_MULT = 0.85   # cms.py:586
SUPERSEDED_SCORE_MULT = 0.55  # cms.py:600
RECENCY_BOOST_MAX = 0.4       # cms.py:661 — boost = 0.4 * (1 - depth/(n-1))
BASE_HALF_LIFE_S = 3600.0     # config.py:588 recency_base_half_life_s default
                              # (the bench service uses the lib default)

POLICIES = ("continuum", "flat")
MODES = ("wall", "hist")


def out_file(dataset: str, extractor: str, tag: str) -> Path:
    stem = "-".join(p for p in (dataset, extractor, tag) if p)
    return RESULTS_DIR / f"longmemeval-ku-{stem}.jsonl"


def band_state_dir(dataset: str, extractor: str, src_tag: str,
                   preset: str = "continuum") -> Path:
    stem = "-".join(p for p in (dataset, extractor, src_tag) if p)
    suffix = "" if preset == "continuum" else f"-{preset}"
    return RESULTS_DIR / "banks" / f"{stem}-ablbands{suffix}"


def abl_tag(src_tag: str, policy: str, mode: str) -> str:
    return "-".join(p for p in (src_tag, "abl", policy, mode) if p)


def wabl_tag(src_tag: str, mode: str) -> str:
    """Write-side ablation tag: flat-INGEST (not just flat ranking)."""
    return "-".join(p for p in (src_tag, "wabl-flat", mode) if p)


def continuum_total_capacity() -> int:
    from pseudolife_memory.memory.miras.presets import continuum_bands  # noqa: PLC0415
    return sum(b.max_entries for b in continuum_bands())


def write_flat_config(data_dir: Path, cap: int) -> Path:
    """Write a config.yaml that MemoryService will pick up, replacing the
    8-band continuum with ONE flat band of ``cap`` entries. Promotion can
    never fire; retention matches the fast tiers' ``balanced`` policy.

    ``surprise_threshold`` is pinned to 0.0 because the YAML loader's
    default (0.3) differs from the dataclass default (0.0) the continuum
    arm gets — pinning keeps the two arms' configs identical outside
    ``memory.miras`` (tests/test_band_ablation_flat.py proves it).
    """
    p = Path(data_dir) / "config.yaml"
    p.write_text(f"""memory:
  surprise_threshold: 0.0
  miras:
    preset: custom
    bands:
      - name: flat
        max_entries: {cap}
        update_interval: 1000000000
        promotion_access_count: 1000000000
        promotion_surprise: 1.1
        retention_policy: balanced
""", encoding="utf-8")
    return p


def survival_stats(cont_dumps: list[dict], flat_dumps: list[dict]) -> dict:
    """The write-side headline numbers: how much each ingest arm kept.

    Both lists hold replay payload dicts; either may be empty (stats for
    the missing side come back None rather than fabricated zeros).
    """
    def survivors(d: dict) -> int:
        return sum(len(b["entries"]) for b in d["bands"])

    flat_by_id = {d["question_id"]: d for d in flat_dumps}
    questions = []
    for d in cont_dumps:
        f = flat_by_id.get(d["question_id"])
        questions.append({
            "question_id": d["question_id"],
            "turns_stored": d["turns_stored"],
            "continuum_survivors": survivors(d),
            "continuum_per_band": {b["name"]: len(b["entries"])
                                   for b in d["bands"]},
            "flat_survivors": survivors(f) if f else None,
        })

    def loss(dumps: list[dict]) -> float | None:
        stored = sum(d["turns_stored"] for d in dumps)
        if not stored:
            return None
        kept = sum(survivors(d) for d in dumps)
        return 1.0 - kept / stored

    return {
        "n_questions": len(questions),
        "continuum_loss_rate": loss(cont_dumps),
        "flat_loss_rate": loss(flat_dumps),
        "questions": questions,
    }


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def rewrite_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


# ══════════════════════════════════════════════════════════════════════════
# replay — CPU-only ingest, band-state serialisation
# ══════════════════════════════════════════════════════════════════════════

def cmd_replay(args) -> int:
    """Re-ingest each question's haystack turns through the REAL service
    (same ``build_service`` + store sequence as the bench) with dreaming
    skipped, then serialise the complete band state."""
    import tempfile

    from longmemeval_bench import (  # noqa: PLC0415 — heavy (torch)
        _parse_date, load_questions,
    )
    from ladder_sweep import build_service  # noqa: PLC0415

    served = load_rows(out_file(args.dataset, args.extractor, args.src_tag))
    if not served:
        sys.exit(f"no served rows for src tag {args.src_tag!r} — nothing to replay")
    by_id = {q["question_id"]: q for q in load_questions(args.dataset)}
    out_dir = band_state_dir(args.dataset, args.extractor, args.src_tag,
                             preset=args.band_preset)
    out_dir.mkdir(parents=True, exist_ok=True)
    flat_cap = args.flat_cap or continuum_total_capacity()

    rows = served[: args.limit] if args.limit else served
    t_all = time.perf_counter()
    for i, row in enumerate(rows):
        qid = row["question_id"]
        dst = out_dir / f"{qid}.json.gz"
        if dst.exists() and not args.force:
            print(f"[{i + 1}/{len(rows)}] {qid}  exists, skipped", flush=True)
            continue
        q = by_id.get(qid)
        if q is None:
            print(f"[{i + 1}/{len(rows)}] {qid}  NOT IN DATASET — skipped",
                  flush=True)
            continue
        t0 = time.perf_counter()
        tmp = Path(tempfile.mkdtemp(prefix="abl_"))
        if args.band_preset == "flat":
            # MemoryService reads <data_dir>/config.yaml at construction —
            # writing it first is the supported custom-preset injection path.
            write_flat_config(tmp, flat_cap)
        svc = build_service(tmp)              # fresh, truncated bench DB —
        if args.band_preset == "flat":
            # A silent fallback to the 8-band preset would invalidate the
            # whole arm — verify the injection actually took, loudly.
            # (_cms is lazy, so check the eagerly-loaded config here and
            # the real band count after ingest below.)
            n_cfg = len(svc.config.memory.miras.bands)
            if svc.config.memory.miras.preset != "custom" or n_cfg != 1:
                sys.exit(f"flat-band injection failed: preset="
                         f"{svc.config.memory.miras.preset!r}, {n_cfg} bands "
                         f"in config (config.yaml not picked up?)")
            if svc.config.memory.surprise_threshold != 0.0:
                sys.exit("flat arm surprise_threshold drifted from 0.0 — "
                         "config confounder, aborting")
        # same knobs as longmemeval_bench.run_extract (dream config is inert
        # here — we never dream — but kept identical for faithfulness):
        svc.config.memory.dream.extract_relations = False
        svc.config.memory.dream.known_facts_window = int(row.get("window", 0))

        # Ingest exactly as ingest_and_dream does (longmemeval_bench.py:242-251)
        # minus the dream loop. Historical timestamps: session date + 60 s per
        # turn (keeps in-session order); first occurrence wins for duplicate
        # turn texts — matching retrieval's text-level dedup.
        sessions = sorted(
            zip(q["haystack_dates"], q["haystack_sessions"]),
            key=lambda pair: _parse_date(pair[0]))
        hist_ts: dict[str, float] = {}
        turns = 0
        for date, session in sessions:
            base_ts = _parse_date(date).timestamp()
            for t_idx, turn in enumerate(session):
                content = (turn.get("content") or "").strip()
                if not content:
                    continue
                text = f"[{date}] {turn['role']}: {content}"
                hist_ts.setdefault(text, base_ts + 60.0 * t_idx)
                svc.store(text, source="bench")
                turns += 1

        # Serialise band state BEFORE any retrieval (retrieval bumps
        # access counts; the original run's context build also ranked over
        # the pre-search state).
        cms = svc._cms  # noqa: SLF001 — bench-style introspection
        assert cms is not None
        if args.band_preset == "flat" and len(cms.bands) != 1:
            sys.exit(f"flat arm built {len(cms.bands)} live bands — aborting")
        bands_out = []
        for depth, band in enumerate(cms.bands):
            bands_out.append({
                "name": band.name,
                "depth": depth,
                "entries": [
                    {
                        "text": e.text,
                        "ts": float(e.timestamp),
                        "hist_ts": float(hist_ts.get(e.text, e.timestamp)),
                        "source": e.source,
                        "superseded_at": e.superseded_at,
                        "slots": [list(s) for s in (e.slots or [])],
                        "emb": [round(float(x), 7) for x in e.embedding.tolist()],
                    }
                    for e in band.entries
                ],
            })

        # Fidelity probe: the REAL retrieval over the replayed state. Divergence
        # of THIS from the served contexts.rag measures replay-state drift
        # (skipped dreams etc.), independent of the offline mirror.
        search_time = time.time()
        live = svc.search(q["question"], top_k=RAG_TOP_K).get("entries", [])
        q_emb = svc._embedder.encode_single(q["question"])  # noqa: SLF001

        payload = {
            "question_id": qid,
            "band_preset": args.band_preset,
            "flat_cap": flat_cap if args.band_preset == "flat" else None,
            "question": q["question"],
            "question_date": q["question_date"],
            "question_ts": _parse_date(q["question_date"]).timestamp(),
            "search_time": search_time,
            "turns_stored": turns,
            "query_emb": [round(float(x), 7) for x in q_emb.tolist()],
            "bands": bands_out,
            "live_replay_rag": [e.get("text", "") for e in live],
        }
        with gzip.open(dst, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        try:
            svc.flush()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
        n_entries = sum(len(b["entries"]) for b in bands_out)
        occupied = sum(1 for b in bands_out if b["entries"])
        print(f"[{i + 1}/{len(rows)}] {qid}  {turns} turns -> {n_entries} "
              f"entries in {occupied} bands "
              f"({time.perf_counter() - t0:.1f}s)", flush=True)
    print(f"replay done: {len(rows)} questions in "
          f"{time.perf_counter() - t_all:.1f}s -> {out_dir}")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# rebuild — offline policy ranking (numpy over the serialised state)
# ══════════════════════════════════════════════════════════════════════════

def _content_tokens(text: str):
    # Import the REAL tokeniser so the slot-channel mirror can't drift
    # (cms.py:101-107; stop-word set cms.py:78-97).
    from pseudolife_memory.memory.cms import _content_tokens as real  # noqa: PLC0415
    return real(text)


def _slot_tokens(slots: list[list[str]]):
    # Mirror of _entry_slot_tokens (cms.py:109-117): content tokens over the
    # (entity, value) pairs; attribute skipped.
    tokens: set[str] = set()
    for s in slots:
        ent, _attr, val = s[0], s[1], s[2]
        tokens |= _content_tokens(f"{ent} {val}")
    return tokens


def select_topk(dump: dict, policy: str, mode: str, k: int = RAG_TOP_K,
                explain: list | None = None) -> list[str]:
    """Mirror of ``ContinuumMemorySystem.retrieve`` Pools 1 + 1.5 for the
    bench call shape (``svc.search(question, top_k=6)``: no filters,
    min_score default, recency on, BM25 off — config default, config.py —
    reranker off, no reference docs; cms.py:536-910).

    ``policy="continuum"`` reproduces the real 8-band ranking; ``"flat"``
    collapses to one pool with the depth-0 recency parameters. ``mode``
    picks the timestamp regime (see module docstring).
    """
    import numpy as np  # noqa: PLC0415

    q = np.asarray(dump["query_emb"], dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)   # band.py:169 normalises the query
    now = dump["search_time"] if mode == "wall" else dump["question_ts"]
    ts_key = "ts" if mode == "wall" else "hist_ts"

    # Flatten in band-then-insertion order — the ordinal that drives every
    # deterministic tie-break downstream (cms.py:1080-1094).
    flat: list[tuple[int, int, dict]] = []       # (ordinal, depth, entry)
    ordinal = 0
    for band in dump["bands"]:
        for e in band["entries"]:
            flat.append((ordinal, band["depth"], e))
            ordinal += 1
    if not flat:
        return []
    embs = np.asarray([e["emb"] for _, _, e in flat], dtype=np.float32)
    norms = np.linalg.norm(embs, axis=1)
    norms[norms == 0.0] = 1.0
    sims = (embs / norms[:, None]) @ q           # band.py:164-170 cosine

    n_bands = len(dump["bands"])

    def recency_weight(ts: float, half_life: float) -> float:
        # _recency_weight (cms.py:1788-1791): 2^(-age/half_life), age >= 0.
        age = max(now - ts, 0.0)
        return 2.0 ** (-age / half_life)

    def pipeline(cand_idx: list[int], boost: float, half_life: float,
                 neural: list, seen: set) -> None:
        # Candidate walk of cms.py:675-758 (filters that can't fire in the
        # bench shape — sources/episodes/tags/logical-turn — omitted;
        # hide_superseded is False so _keep always passes, cms.py:616-621).
        for j in cand_idx:
            e = flat[j][2]
            if e["text"] in seen:                       # cms.py:693-695
                continue
            score = float(sims[j])
            src_mult = (ASSISTANT_SCORE_MULT             # cms.py:602-603,717
                        if e["source"] == "assistant" else 1.0)
            sup_mult = (SUPERSEDED_SCORE_MULT            # cms.py:720-722
                        if e["superseded_at"] is not None else 1.0)
            if boost > 0.0:                              # cms.py:731-738
                rec = recency_weight(e[ts_key], half_life)
                relevance = score * (1.0 + boost * rec)
            else:
                relevance = score
            adjusted = relevance * src_mult * sup_mult   # cms.py:745
            if relevance >= MIN_SCORE:                   # cms.py:751-754
                neural.append((flat[j][0], e, adjusted))
                seen.add(e["text"])

    neural: list[tuple[int, dict, float]] = []
    seen: set[str] = set()

    if policy == "continuum":
        # Per-band candidate pools: each band contributes its own top-k by
        # raw cosine (band.py:157-190), walked shallow-to-deep (cms.py:646).
        start = 0
        for depth, band in enumerate(dump["bands"]):
            m = len(band["entries"])
            idx = list(range(start, start + m))
            start += m
            if not idx:
                continue
            # boost ramp + geometric half-life (cms.py:656-665)
            if n_bands == 1:
                boost, half_life = 0.0, float("inf")
            else:
                frac = depth / (n_bands - 1)
                boost = RECENCY_BOOST_MAX * (1.0 - frac)
                half_life = BASE_HALF_LIFE_S * (2.0 ** depth)
            cand = sorted(idx, key=lambda j: (-sims[j], j))[:k]
            pipeline(cand, boost, half_life, neural, seen)
    else:  # flat single table
        # One pool, global top-k by cosine; single recency term at the
        # shallowest band's parameters (depth-0 of the ramp: boost=0.4,
        # half-life=BASE_HALF_LIFE_S). This is what cms.py:646-665 degrades
        # to with n=1 band, except n=1 would set boost=0 (cms.py:657-658) —
        # the ablation spec keeps the plain-timestamp-recency term.
        cand = sorted(range(len(flat)), key=lambda j: (-sims[j], j))[:k]
        pipeline(cand, RECENCY_BOOST_MAX, BASE_HALF_LIFE_S, neural, seen)

    # Pool 1.5 — slot-token channel (cms.py:780-795 + 1109-1198). Identical
    # under both policies/modes (timestamp-free), but seen-set dependent.
    tokens = _content_tokens(dump["question"])
    if tokens:
        slot_cands: list[tuple[int, dict, float]] = []
        for o, _depth, e in flat:                        # ordinal order,
            if e["text"] in seen:                        # cms.py:1158-1161
                continue
            if not e["slots"]:
                continue
            st = _slot_tokens(e["slots"])
            overlap = tokens & st
            if not overlap:
                continue
            confidence = len(overlap) / max(len(st), 1)  # cms.py:1183
            score = float(min(0.95, 0.55 + 0.35 * confidence))  # cms.py:1184
            if e["superseded_at"] is not None:           # cms.py:1185-1186
                score *= 0.55
            slot_cands.append((o, e, score))
        slot_cands.sort(key=lambda x: x[2], reverse=True)  # cms.py:1197 (stable)
        for o, e, score in slot_cands[:k]:               # cms.py:1198,791-793
            neural.append((o, e, score))
            seen.add(e["text"])

    neural.sort(key=lambda x: x[2], reverse=True)        # cms.py:909 (stable)
    top = neural[:k]                                     # cms.py:910
    if explain is not None:
        explain.extend((o, e["text"], round(s, 4)) for o, e, s in top)
    return [e["text"] for _, e, _ in top]


def _served_selection(dump: dict, served_rag: str) -> set[str]:
    """Reconstruct the originally-served rag selection as a set of entry
    texts. Turn texts may themselves contain blank lines, so splitting the
    joined context on ``\\n\\n`` is ambiguous — substring containment
    against the known entry universe is exact for verbatim-joined texts."""
    if not served_rag:
        return set()
    universe = {e["text"] for band in dump["bands"] for e in band["entries"]}
    return {t for t in universe if t in served_rag}


def _turn_label(dump: dict) -> dict[str, str]:
    """Stable short ids per entry text: b<depth>e<idx> plus the turn's
    [date] role prefix, for the dry-run side-by-side."""
    labels: dict[str, str] = {}
    for band in dump["bands"]:
        for i, e in enumerate(band["entries"]):
            head = e["text"].split("] ", 1)
            prefix = (head[0] + "]") if len(head) == 2 else e["text"][:24]
            role = head[1].split(":", 1)[0] if len(head) == 2 else "?"
            labels.setdefault(
                e["text"], f"b{band['depth']}e{i:02d} {prefix} {role}")
    return labels


def cmd_rebuild(args) -> int:
    served = load_rows(out_file(args.dataset, args.extractor, args.src_tag))
    if not served:
        sys.exit(f"no served rows for src tag {args.src_tag!r}")
    dumps_dir = band_state_dir(args.dataset, args.extractor, args.src_tag,
                               preset=args.band_preset)
    # Flat-INGEST dumps have one band; only the flat ranking policy is
    # meaningful over them (the continuum ramp needs 8 depths).
    policies = POLICIES if args.band_preset == "continuum" else ("flat",)

    available: list[tuple[dict, dict]] = []
    missing: list[str] = []
    for row in served:
        p = dumps_dir / f"{row['question_id']}.json.gz"
        if not p.exists():
            missing.append(row["question_id"])
            continue
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            available.append((row, json.load(fh)))
    if missing:
        print(f"WARNING: {len(missing)} questions lack band-state dumps "
              f"(run `replay` first): {', '.join(missing[:5])}"
              f"{'…' if len(missing) > 5 else ''}")
    if not available:
        sys.exit("no band-state dumps found — run `replay` first")

    # ── sanity gate + selections over everything available ────────────────
    agree_mirror = []      # gate-policy+wall mirror vs served (the gate)
    agree_replay = []      # real search on replayed state vs served
    agree_mirror_replay = []   # mirror vs real search (formula fidelity)
    ab_agree = {m: [] for m in MODES}   # continuum vs flat overlap, per mode
    selections: dict[tuple[str, str], dict[str, list[str]]] = {
        (p, m): {} for p in policies for m in MODES}
    gate_policy = policies[0]   # continuum normally; flat for flat-ingest

    for row, dump in available:
        for policy in policies:
            for mode in MODES:
                selections[(policy, mode)][row["question_id"]] = select_topk(
                    dump, policy, mode)
        served_set = _served_selection(dump, row["contexts"].get("rag", ""))
        mirror = set(selections[(gate_policy, "wall")][row["question_id"]])
        replay_sel = set(dump.get("live_replay_rag", []))
        denom = max(1, len(served_set))
        agree_mirror.append(len(mirror & served_set) / denom)
        agree_replay.append(len(replay_sel & served_set) / denom)
        agree_mirror_replay.append(
            len(mirror & replay_sel) / max(1, len(replay_sel)))
        if len(policies) == 2:
            for mode in MODES:
                a = set(selections[("continuum", mode)][row["question_id"]])
                b = set(selections[("flat", mode)][row["question_id"]])
                ab_agree[mode].append(len(a & b) / max(1, len(a | b)))

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print(f"\n── sanity gate ({len(available)} questions) ──────────────────")
    note = ("" if args.band_preset == "continuum" else
            "  (flat-INGEST state vs continuum-served: divergence expected)")
    print(f"{gate_policy}+wall mirror vs SERVED rag : "
          f"{mean(agree_mirror):.3f}{note}")
    print(f"  replayed real search vs served    : {mean(agree_replay):.3f}  "
          f"(replay-state drift: skipped dreams)")
    print(f"  mirror vs replayed real search    : {mean(agree_mirror_replay):.3f}  "
          f"(offline formula fidelity)")
    if len(policies) == 2:
        for mode in MODES:
            print(f"continuum vs flat overlap ({mode:4s})   : "
                  f"{mean(ab_agree[mode]):.3f}  (Jaccard of top-{RAG_TOP_K})")

    if args.dry_run:
        print(f"\n── dry run: first {min(3, len(available))} questions, "
              f"side by side ─────────")
        for row, dump in available[:3]:
            labels = _turn_label(dump)
            print(f"\n{row['question_id']}  {row['question'][:70]}")
            for mode in MODES:
                a = selections[(policies[0], mode)][row["question_id"]]
                b = (selections[("flat", mode)][row["question_id"]]
                     if len(policies) == 2 else [])
                print(f"  [{mode}] {policies[0]:<42}| "
                      f"{'flat' if len(policies) == 2 else ''}")
                for i in range(max(len(a), len(b))):
                    la = labels.get(a[i], "-")[:40] if i < len(a) else ""
                    lb = labels.get(b[i], "-")[:40] if i < len(b) else ""
                    mark = " " if (i < len(a) and i < len(b)
                                   and a[i] == b[i]) else "*"
                    print(f"  {mark} {la:<42}| {lb}")
        print("\ndry run only — no files written")
        return 0

    # ── write the tagged JSONLs ───────────────────────────────────────────
    for policy in policies:
        for mode in MODES:
            tag = (abl_tag(args.src_tag, policy, mode)
                   if args.band_preset == "continuum"
                   else wabl_tag(args.src_tag, mode))
            out_rows = []
            for row, dump in available:
                sel = selections[(policy, mode)][row["question_id"]]
                new = dict(row)
                contexts = dict(row["contexts"])
                contexts["rag"] = "\n\n".join(sel)
                # Hybrid: cortex fact block verbatim from the served row
                # (rebuild_contexts.py precedent), new top-3 raw spliced in.
                facts_block = row["contexts"]["hybrid"].split(
                    _HYBRID_SPLIT, 1)[0]
                contexts["hybrid"] = (facts_block + _HYBRID_SPLIT
                                      + "\n\n".join(sel[:HYBRID_TOP_K]))
                new["contexts"] = contexts
                new["ablation"] = {"policy": policy, "mode": mode,
                                   "source_tag": args.src_tag,
                                   "band_preset": args.band_preset}
                for arm in ARMS:      # strip verdicts -> answer phase re-runs
                    for field in ("response", "correct", "context_tokens"):
                        new.pop(f"{arm}_{field}", None)
                out_rows.append(new)
            dst = out_file(args.dataset, args.extractor, tag)
            rewrite_rows(dst, out_rows)
            print(f"wrote {len(out_rows)} rows -> {dst.name}")

    # ── survival-stats artifact (write-side headline; both ingest arms) ───
    if args.band_preset == "flat":
        cont_dir = band_state_dir(args.dataset, args.extractor, args.src_tag)
        cont_dumps = []
        if cont_dir.is_dir():
            for row, _ in available:
                p = cont_dir / f"{row['question_id']}.json.gz"
                if p.exists():
                    with gzip.open(p, "rt", encoding="utf-8") as fh:
                        cont_dumps.append(json.load(fh))
        stats = survival_stats(cont_dumps, [d for _, d in available])
        stem = "-".join(p for p in (args.dataset, args.extractor,
                                    args.src_tag) if p)
        stats_path = RESULTS_DIR / f"longmemeval-ku-{stem}-wabl-survival.json"
        stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        print(f"survival stats ({len(cont_dumps)} continuum / "
              f"{len(available)} flat dumps) -> {stats_path.name}")
        if not cont_dumps:
            print("  NOTE: no continuum dumps found — run "
                  "`replay --band-preset continuum` and rebuild again for "
                  "the side-by-side survival comparison")

    ex = args.extractor
    tags = (
        [abl_tag(args.src_tag, p, m) for p in POLICIES for m in MODES]
        if args.band_preset == "continuum"
        else [wabl_tag(args.src_tag, m) for m in MODES])
    compare_hint = (
        f"""then per mode M in wall hist, per arm A in rag hybrid:
  python evals/replicate.py compare --dataset {args.dataset} --extractor {ex} \\
      --tag {abl_tag(args.src_tag, 'continuum', 'M')} --b-tag {abl_tag(args.src_tag, 'flat', 'M')} --arm A"""
        if args.band_preset == "continuum"
        else f"""then per mode M in wall hist, per arm A in rag hybrid:
  # write-side isolation (same flat ranking, different survivor sets):
  python evals/replicate.py compare --dataset {args.dataset} --extractor {ex} \\
      --tag {abl_tag(args.src_tag, 'flat', 'M')} --b-tag {wabl_tag(args.src_tag, 'M')} --arm A
  # whole-system (as-designed continuum vs flat everything):
  python evals/replicate.py compare --dataset {args.dataset} --extractor {ex} \\
      --tag {abl_tag(args.src_tag, 'continuum', 'M')} --b-tag {wabl_tag(args.src_tag, 'M')} --arm A""")
    print(f"""
── GPU window (answer phase; needs the Qwen endpoint at :1234) ──────────
for each TAG in {' '.join(tags)}:
  python evals/longmemeval_bench.py --dataset {args.dataset} --extractor {ex} --tag TAG --phase answer
  python evals/replicate.py spawn --dataset {args.dataset} --extractor {ex} --tag TAG -n 4
  python evals/replicate.py run   --dataset {args.dataset} --extractor {ex} --tag TAG
  python evals/replicate.py agg   --dataset {args.dataset} --extractor {ex} --tag TAG
{compare_hint}
""")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--dataset", default="oracle")
        p.add_argument("--extractor", default="e4b-ft")
        p.add_argument("--src-tag", default="arm1",
                       help="tag of the served source run ('' = untagged)")
        p.add_argument("--band-preset", choices=("continuum", "flat"),
                       default="continuum",
                       help="ingest band structure: the stock 8-band "
                            "continuum, or ONE flat band (write-side "
                            "ablation — different entries survive)")

    p = sub.add_parser("replay", help="CPU ingest replay -> band-state dumps")
    common(p)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true",
                   help="re-replay questions whose dump already exists")
    p.add_argument("--flat-cap", type=int, default=None,
                   help="flat band capacity (default: the continuum "
                        "preset's total, currently 5250)")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("rebuild",
                       help="offline policy rebuild -> tagged JSONLs")
    common(p)
    p.add_argument("--dry-run", action="store_true",
                   help="report agreement + 3-question side-by-side; no writes")
    p.set_defaults(fn=cmd_rebuild)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
