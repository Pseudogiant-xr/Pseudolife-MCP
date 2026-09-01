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

v25 rerun support (2026-08-14; spec
docs/superpowers/specs/2026-08-14-flat-band-verdict-preregistration.md):
the retrieval backbone changed after the July runs — BM25 fusion is ON by
default (global candidate pool, cms.py:954-1019), the recency ramp is OFF
by default (cms.py:773), and capacity eviction demotes down the band
chain instead of deleting (cms.py:2072). The mirror gained matching
knobs: ``--bm25 on`` adds the BM25 pool (real ``BM25Index`` /
``normalize_scores``, not a numeric mirror), ``--recency off`` reproduces
the production read path (timestamps never enter ranking, so wall/hist
collapse to one ``off`` pseudo-mode), ``--half-life-base`` picks the
ramp base for steelman arms, and ``--tag-prefix abl25`` keeps every new
artifact clear of the July canonical names. ``replay --band-preset
scaled --scale-total N`` ingests through the 8-band layout at a
proportionally scaled total capacity (eviction-policy edge case: both
arms genuinely evict), and the ``evidence`` subcommand computes the
preregistered judge-free metric — per-question survival of gold-evidence
turns — plus drop-set evidence fractions, with a paired sign-flip
permutation test.
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

from context_format import MEMS_HEADER  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Bench constants mirrored from longmemeval_bench.py (not imported at module
# level — that module pulls in torch/ladder_sweep; replay imports it lazily).
# The served-context headers are NOT mirrored: context_format above is the
# one definition, stdlib-only so it is safe to import here.
RAG_TOP_K = 6
HYBRID_TOP_K = 3
ARMS = ("rag", "cortex", "hybrid")

# ── CMS ranking constants, mirrored with line cites (v29 tree) ────────────
MIN_SCORE = 0.25              # cms.py:688 — relevance keep-threshold
ASSISTANT_SCORE_MULT = 0.85   # cms.py:698
SUPERSEDED_SCORE_MULT = 0.55  # cms.py:710
RECENCY_BOOST_MAX = 0.4       # cms.py:777 — boost = 0.4 * (1 - depth/(n-1))
BASE_HALF_LIFE_S = 3600.0     # config.py:685 recency_base_half_life_s default
                              # (the bench service uses the lib default;
                              # the deployed daemon overrides to 86400 when
                              # absent — service.py:657)

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


def abl_tag(src_tag: str, policy: str, mode: str, prefix: str = "abl") -> str:
    return "-".join(p for p in (src_tag, prefix, policy, mode) if p)


def wabl_tag(src_tag: str, mode: str, prefix: str = "abl") -> str:
    """Write-side ablation tag: flat-INGEST (not just flat ranking).
    ``prefix="abl"`` yields the legacy ``wabl-flat`` family; ``abl25``
    yields ``wabl25-flat`` so v25 artifacts never collide with July's."""
    return "-".join(p for p in (src_tag, f"w{prefix}-flat", mode) if p)


def continuum_total_capacity() -> int:
    from pseudolife_memory.memory.miras.presets import continuum_bands  # noqa: PLC0415
    return sum(b.max_entries for b in continuum_bands())


def scaled_caps(total: int) -> list[int]:
    """Per-band capacities proportional to the continuum preset, each
    rounded and floored at 1. The realised sum (reported by the caller)
    may differ from ``total`` by a few entries because of rounding — the
    flat comparison arm must use the REALISED sum, not ``total``."""
    from pseudolife_memory.memory.miras.presets import continuum_bands  # noqa: PLC0415
    bands = continuum_bands()
    full = sum(b.max_entries for b in bands)
    return [max(1, round(b.max_entries * total / full)) for b in bands]


def write_scaled_config(data_dir: Path, total: int) -> tuple[Path, int]:
    """Write a config.yaml with the 8-band continuum layout at a
    proportionally scaled total capacity. Every non-capacity field
    (update_interval, promotion_*, retention_policy) is copied verbatim
    from the preset, so promotion cadence and per-tier retention behave
    exactly as shipped — only the capacity envelope shrinks enough for
    the corpus to force eviction in BOTH arms (edge case 1).

    ``surprise_threshold`` is pinned to 0.0 for the same reason as
    :func:`write_flat_config` (YAML-loader default differs from the
    dataclass default). Returns (path, realised_total).
    """
    from pseudolife_memory.memory.miras.presets import continuum_bands  # noqa: PLC0415
    caps = scaled_caps(total)
    lines = ["memory:", "  surprise_threshold: 0.0", "  miras:",
             "    preset: custom", "    bands:"]
    for spec, cap in zip(continuum_bands(), caps):
        lines += [
            f"      - name: {spec.name}",
            f"        max_entries: {cap}",
            f"        update_interval: {spec.update_interval}",
            f"        promotion_access_count: {spec.promotion_access_count}",
            f"        promotion_surprise: {spec.promotion_surprise}",
            f"        retention_policy: {spec.retention_policy}",
        ]
    p = Path(data_dir) / "config.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p, sum(caps)


def write_continuum_config(data_dir: Path) -> Path:
    """Pin the 8-band continuum for the continuum replay arm. Required
    since the 2026-08-15 default flip (a bare service is now FLAT);
    ``surprise_threshold`` pinned to 0.0 for the same YAML-loader
    divergence reason as :func:`write_flat_config`, keeping the arm
    byte-identical to the pre-flip continuum arm."""
    p = Path(data_dir) / "config.yaml"
    p.write_text("""memory:
  surprise_threshold: 0.0
  miras:
    preset: continuum
""", encoding="utf-8")
    return p


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
    flat_cap = args.flat_cap or continuum_total_capacity()
    scaled_total = None
    if args.band_preset == "scaled":
        if not args.scale_total:
            sys.exit("--band-preset scaled requires --scale-total")
        scaled_total = sum(scaled_caps(args.scale_total))
    # Dump-dir label: default arms keep their historical names; explicitly
    # capacity-scaled arms carry the realised capacity so they can never
    # be mistaken for (or overwrite) the standard dumps.
    preset_label = args.band_preset
    if args.band_preset == "flat" and flat_cap != continuum_total_capacity():
        preset_label = f"flat{flat_cap}"
    elif args.band_preset == "scaled":
        preset_label = f"scaled{scaled_total}"
    out_dir = band_state_dir(args.dataset, args.extractor, args.src_tag,
                             preset=preset_label)
    out_dir.mkdir(parents=True, exist_ok=True)

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
        elif args.band_preset == "scaled":
            write_scaled_config(tmp, args.scale_total)
        else:
            # Explicit since the 2026-08-15 default flip: a bare service
            # is now the flat layout, which would invalidate this arm.
            write_continuum_config(tmp)
        svc = build_service(tmp)              # fresh, truncated bench DB —
        if args.band_preset != "continuum":
            # A silent fallback to the 8-band preset would invalidate the
            # whole arm — verify the injection actually took, loudly.
            # (_cms is lazy, so check the eagerly-loaded config here and
            # the real band count after ingest below.)
            want_bands = 1 if args.band_preset == "flat" else 8
            n_cfg = len(svc.config.memory.miras.bands)
            if svc.config.memory.miras.preset != "custom" or n_cfg != want_bands:
                sys.exit(f"{args.band_preset}-band injection failed: preset="
                         f"{svc.config.memory.miras.preset!r}, {n_cfg} bands "
                         f"in config (config.yaml not picked up?)")
            if args.band_preset == "scaled":
                got_total = sum(b.max_entries
                                for b in svc.config.memory.miras.bands)
                if got_total != scaled_total:
                    sys.exit(f"scaled-band injection failed: capacity "
                             f"{got_total} != {scaled_total}")
            if svc.config.memory.surprise_threshold != 0.0:
                sys.exit(f"{args.band_preset} arm surprise_threshold drifted "
                         "from 0.0 — config confounder, aborting")
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
        want_live = {"continuum": 8, "flat": 1, "scaled": 8}[args.band_preset]
        if len(cms.bands) != want_live:
            sys.exit(f"{args.band_preset} arm built {len(cms.bands)} live "
                     f"bands (expected {want_live}) — aborting")
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
        q_emb = svc._embedder.encode_query(q["question"])  # noqa: SLF001

        payload = {
            "question_id": qid,
            "band_preset": preset_label,
            "flat_cap": flat_cap if args.band_preset == "flat" else None,
            "scaled_total": scaled_total,
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
                explain: list | None = None, *, recency: str = "on",
                half_life_base: float | None = None,
                bm25: bool = False) -> list[str]:
    """Mirror of ``ContinuumMemorySystem.retrieve`` Pools 1 + 1.5 (+ 1.75
    when ``bm25=True``) for the bench call shape (``svc.search(question,
    top_k=6)``: no filters, min_score default, reranker off, timeline and
    contiguity off, no reference docs; cms.py:640-1085).

    ``policy="continuum"`` reproduces the real 8-band ranking; ``"flat"``
    collapses to one pool with the depth-0 recency parameters. ``mode``
    picks the timestamp regime (see module docstring).

    v25 knobs: ``recency="off"`` mirrors the production default
    (``recency_boost_enabled=False`` since 2026-07-25 — cms.py:773):
    boost=0 under BOTH policies, so ``mode`` becomes irrelevant.
    ``bm25=True`` mirrors the production-default BM25 fusion
    (cms.py:974-1019) using the real ``BM25Index``/``normalize_scores``
    over a global candidate pool — identical under both policies by
    construction, but seen-set dependent. ``half_life_base`` overrides
    the ramp base for the steelman arms (daemon deploys 86400 s).
    """
    import numpy as np  # noqa: PLC0415

    q = np.asarray(dump["query_emb"], dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)   # band.py:169 normalises the query
    now = dump["search_time"] if mode == "wall" else dump["question_ts"]
    ts_key = "ts" if mode == "wall" else "hist_ts"
    base_hl = BASE_HALF_LIFE_S if half_life_base is None else float(half_life_base)

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
        # raw cosine (band.py:157-190), walked shallow-to-deep (cms.py:758).
        start = 0
        for depth, band in enumerate(dump["bands"]):
            m = len(band["entries"])
            idx = list(range(start, start + m))
            start += m
            if not idx:
                continue
            # boost ramp + geometric half-life (cms.py:767-781); the
            # ramp is gated by recency_boost_enabled in production
            # (cms.py:773) — recency="off" mirrors that default.
            if n_bands == 1 or recency == "off":
                boost, half_life = 0.0, float("inf")
            else:
                frac = depth / (n_bands - 1)
                boost = RECENCY_BOOST_MAX * (1.0 - frac)
                half_life = base_hl * (2.0 ** depth)
            cand = sorted(idx, key=lambda j: (-sims[j], j))[:k]
            pipeline(cand, boost, half_life, neural, seen)
    else:  # flat single table
        # One pool, global top-k by cosine. With recency on: a single
        # recency term at the shallowest band's parameters (depth-0 of
        # the ramp) — the July ablation spec's flat arm. With recency
        # off: no timestamp term, which is ALSO what a real n=1 band
        # gets in production (cms.py:773 short-circuits on n == 1).
        cand = sorted(range(len(flat)), key=lambda j: (-sims[j], j))[:k]
        if recency == "off":
            pipeline(cand, 0.0, float("inf"), neural, seen)
        else:
            pipeline(cand, RECENCY_BOOST_MAX, base_hl, neural, seen)

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
        slot_cands.sort(key=lambda x: x[2], reverse=True)  # cms.py:1464 (stable)
        for o, e, score in slot_cands[:k]:
            neural.append((o, e, score))
            seen.add(e["text"])

    # Pool 1.75 — BM25 sparse lexical fusion (cms.py:974-1019). The
    # candidate pool is GLOBAL across bands (cms.py:954-972), so this
    # pool is identical under both policies; the REAL index and
    # normaliser are imported so the numerics can't drift.
    if bm25:
        from types import SimpleNamespace  # noqa: PLC0415

        from pseudolife_memory.memory.bm25 import (  # noqa: PLC0415
            BM25Index, normalize_scores,
        )
        from pseudolife_memory.utils.config import BM25Config  # noqa: PLC0415

        cfg = BM25Config()          # bench service runs the lib defaults
        wrapped = [SimpleNamespace(text=e["text"], _abl_ord=o)
                   for o, _d, e in flat]
        by_text = {e["text"]: (o, e) for o, _d, e in flat}
        idx = BM25Index(wrapped, k1=cfg.k1, b=cfg.b)
        raw_hits = idx.score(dump["question"], top_k=cfg.top_n)
        norm_hits = normalize_scores(raw_hits)
        lookup = {w.text: s for w, s in norm_hits if s >= cfg.min_score}
        # Boost entries already in the pool (cms.py:986-994)…
        neural = [(o, e, sc + cfg.weight * lookup[e["text"]])
                  if e["text"] in lookup else (o, e, sc)
                  for o, e, sc in neural]
        # …then inject BM25-only matches (cms.py:996-1010; the default
        # keep-floor deliberately does not bound injections).
        for w, norm_score in norm_hits:
            if w.text in seen or norm_score < cfg.min_score:
                continue
            o, e = by_text[w.text]
            neural.append((o, e, cfg.weight * norm_score))
            seen.add(w.text)

    neural.sort(key=lambda x: x[2], reverse=True)        # cms.py:1084 (stable)
    top = neural[:k]                                     # cms.py:1085
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
    if args.band_preset == "scaled":
        sys.exit("rebuild over scaled dumps is not wired yet — the "
                 "eviction-policy edge case is analysed offline via the "
                 "`evidence` subcommand (GPU confirmatory only if its "
                 "gate fires; extend rebuild then)")
    served = load_rows(out_file(args.dataset, args.extractor, args.src_tag))
    if not served:
        sys.exit(f"no served rows for src tag {args.src_tag!r}")
    dumps_dir = band_state_dir(args.dataset, args.extractor, args.src_tag,
                               preset=args.band_preset)
    # Flat-INGEST dumps have one band; only the flat ranking policy is
    # meaningful over them (the continuum ramp needs 8 depths).
    policies = POLICIES if args.band_preset == "continuum" else ("flat",)
    # With the recency term off, timestamps never enter ranking — wall
    # and hist are byte-identical, so a single "off" pseudo-mode replaces
    # the 2x2's mode axis. The suffix lets steelman arms (e.g. the 24h
    # half-life base) coexist: hist + suffix "24" -> tag mode "hist24".
    sel_kw = {"recency": args.recency, "bm25": args.bm25 == "on",
              "half_life_base": args.half_life_base}
    if args.recency == "off":
        modes = ("off",)
        mode_ts = {"off": "wall"}       # timestamps unused; any regime works
    else:
        modes = tuple(args.modes)
        mode_ts = {m: m for m in modes}

    def tag_mode(mode: str) -> str:
        return f"{mode}{args.tag_mode_suffix}"

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
    agree_mirror = []      # gate-policy first-mode mirror vs served
    agree_replay = []      # real search on replayed state vs served
    agree_mirror_replay = []   # mirror vs real search (formula fidelity)
    ab_agree = {m: [] for m in modes}   # continuum vs flat overlap, per mode
    selections: dict[tuple[str, str], dict[str, list[str]]] = {
        (p, m): {} for p in policies for m in modes}
    gate_policy = policies[0]   # continuum normally; flat for flat-ingest

    for row, dump in available:
        for policy in policies:
            for mode in modes:
                selections[(policy, mode)][row["question_id"]] = select_topk(
                    dump, policy, mode_ts[mode], **sel_kw)
        served_set = _served_selection(dump, row["contexts"].get("rag", ""))
        mirror = set(selections[(gate_policy, modes[0])][row["question_id"]])
        replay_sel = set(dump.get("live_replay_rag", []))
        denom = max(1, len(served_set))
        agree_mirror.append(len(mirror & served_set) / denom)
        agree_replay.append(len(replay_sel & served_set) / denom)
        agree_mirror_replay.append(
            len(mirror & replay_sel) / max(1, len(replay_sel)))
        if len(policies) == 2:
            for mode in modes:
                a = set(selections[("continuum", mode)][row["question_id"]])
                b = set(selections[("flat", mode)][row["question_id"]])
                ab_agree[mode].append(len(a & b) / max(1, len(a | b)))

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print(f"\n── sanity gate ({len(available)} questions) ──────────────────")
    note = ("" if args.band_preset == "continuum" else
            "  (flat-INGEST state vs continuum-served: divergence expected)")
    print(f"{gate_policy}+{modes[0]} mirror vs SERVED rag : "
          f"{mean(agree_mirror):.3f}{note}")
    print(f"  replayed real search vs served    : {mean(agree_replay):.3f}  "
          f"(replay-state drift: skipped dreams / embedder change)")
    print(f"  mirror vs replayed real search    : {mean(agree_mirror_replay):.3f}  "
          f"(offline formula fidelity — the G0 gate when the mirror flags "
          f"match the live defaults: recency off, bm25 on)")
    if len(policies) == 2:
        for mode in modes:
            print(f"continuum vs flat overlap ({mode:4s})   : "
                  f"{mean(ab_agree[mode]):.3f}  (Jaccard of top-{RAG_TOP_K})")

    if args.dry_run:
        print(f"\n── dry run: first {min(3, len(available))} questions, "
              f"side by side ─────────")
        for row, dump in available[:3]:
            labels = _turn_label(dump)
            print(f"\n{row['question_id']}  {row['question'][:70]}")
            for mode in modes:
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
        for mode in modes:
            tag = (abl_tag(args.src_tag, policy, tag_mode(mode),
                           prefix=args.tag_prefix)
                   if args.band_preset == "continuum"
                   else wabl_tag(args.src_tag, tag_mode(mode),
                                 prefix=args.tag_prefix))
            out_rows = []
            for row, dump in available:
                sel = selections[(policy, mode)][row["question_id"]]
                new = dict(row)
                contexts = dict(row["contexts"])
                contexts["rag"] = "\n\n".join(sel)
                # Hybrid: cortex fact block verbatim from the served row
                # (rebuild_contexts.py precedent), new top-3 raw spliced in.
                facts_block = row["contexts"]["hybrid"].split(
                    MEMS_HEADER, 1)[0]
                contexts["hybrid"] = (facts_block + MEMS_HEADER
                                      + "\n\n".join(sel[:HYBRID_TOP_K]))
                new["contexts"] = contexts
                new["ablation"] = {"policy": policy, "mode": tag_mode(mode),
                                   "source_tag": args.src_tag,
                                   "band_preset": args.band_preset,
                                   "recency": args.recency,
                                   "bm25": args.bm25,
                                   "half_life_base": args.half_life_base}
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
        wpref = f"w{args.tag_prefix}"
        stats_path = RESULTS_DIR / f"longmemeval-ku-{stem}-{wpref}-survival.json"
        stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        print(f"survival stats ({len(cont_dumps)} continuum / "
              f"{len(available)} flat dumps) -> {stats_path.name}")
        if not cont_dumps:
            print("  NOTE: no continuum dumps found — run "
                  "`replay --band-preset continuum` and rebuild again for "
                  "the side-by-side survival comparison")

    ex = args.extractor
    tmodes = [tag_mode(m) for m in modes]
    pfx = args.tag_prefix
    tags = (
        [abl_tag(args.src_tag, p, m, prefix=pfx)
         for p in POLICIES for m in tmodes]
        if args.band_preset == "continuum"
        else [wabl_tag(args.src_tag, m, prefix=pfx) for m in tmodes])
    mode_list = " ".join(tmodes)
    compare_hint = (
        f"""then per mode M in {mode_list}, per arm A in rag hybrid:
  python evals/replicate.py compare --dataset {args.dataset} --extractor {ex} \\
      --tag {abl_tag(args.src_tag, 'continuum', 'M', prefix=pfx)} --b-tag {abl_tag(args.src_tag, 'flat', 'M', prefix=pfx)} --arm A"""
        if args.band_preset == "continuum"
        else f"""then per mode M in {mode_list}, per arm A in rag hybrid:
  # write-side isolation (same flat ranking, different survivor sets):
  python evals/replicate.py compare --dataset {args.dataset} --extractor {ex} \\
      --tag {abl_tag(args.src_tag, 'flat', 'M', prefix=pfx)} --b-tag {wabl_tag(args.src_tag, 'M', prefix=pfx)} --arm A
  # whole-system (as-designed continuum vs flat everything):
  python evals/replicate.py compare --dataset {args.dataset} --extractor {ex} \\
      --tag {abl_tag(args.src_tag, 'continuum', 'M', prefix=pfx)} --b-tag {wabl_tag(args.src_tag, 'M', prefix=pfx)} --arm A""")
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


# ══════════════════════════════════════════════════════════════════════════
# evidence — eviction-policy metric over capacity-scaled dumps (edge case 1)
# ══════════════════════════════════════════════════════════════════════════

def _evidence_texts(q: dict) -> set[str]:
    """Gold-evidence turn texts (ingest form). Primary: per-turn
    ``has_answer`` markers (the needle_survival.py precedent — the
    sharpest evidence unit LongMemEval provides). Fallback when a
    dataset carries no turn markers: every turn of the
    ``answer_session_ids`` sessions."""
    out: set[str] = set()
    for date, session in zip(q["haystack_dates"], q["haystack_sessions"]):
        for turn in session:
            if str(turn.get("has_answer", "False")).lower() != "true":
                continue
            content = (turn.get("content") or "").strip()
            if content:
                out.add(f"[{date}] {turn['role']}: {content}")
    if out:
        return out
    want = set(q.get("answer_session_ids") or [])
    for sid, date, session in zip(q["haystack_session_ids"],
                                  q["haystack_dates"],
                                  q["haystack_sessions"]):
        if sid not in want:
            continue
        for turn in session:
            content = (turn.get("content") or "").strip()
            if content:
                out.add(f"[{date}] {turn['role']}: {content}")
    return out


def _stored_texts(q: dict) -> set[str]:
    out: set[str] = set()
    for date, session in zip(q["haystack_dates"], q["haystack_sessions"]):
        for turn in session:
            content = (turn.get("content") or "").strip()
            if content:
                out.add(f"[{date}] {turn['role']}: {content}")
    return out


def _paired_permutation_p(deltas: list[float], n_perm: int = 10_000,
                          seed: int = 0) -> float:
    """Two-sided sign-flip permutation test on paired deltas (the same
    test family replicate.py uses for accuracy comparisons)."""
    import numpy as np  # noqa: PLC0415
    d = np.asarray(deltas, dtype=np.float64)
    obs = abs(d.mean())
    rng = np.random.RandomState(seed)
    signs = rng.choice((-1.0, 1.0), size=(n_perm, d.size))
    perm = np.abs((signs * d).mean(axis=1))
    return float((perm >= obs - 1e-12).mean())


def cmd_evidence(args) -> int:
    """Compare gold-evidence survival between two capacity-scaled ingest
    arms (edge case 1: eviction POLICY at identical total capacity), plus
    the drop-set evidence fraction (edge case 3: is the band/promotion
    signal informative about what to keep?)."""
    from longmemeval_bench import load_questions  # noqa: PLC0415

    by_id = {q["question_id"]: q for q in load_questions(args.dataset)}
    a_dir = band_state_dir(args.dataset, args.extractor, args.src_tag,
                           preset=args.a_preset)
    b_dir = band_state_dir(args.dataset, args.extractor, args.src_tag,
                           preset=args.b_preset)
    for d in (a_dir, b_dir):
        if not d.is_dir():
            sys.exit(f"missing dump dir {d} — run replay first")

    questions = []
    a_rates, b_rates, a_drop_ev, b_drop_ev = [], [], [], []
    for p in sorted(a_dir.glob("*.json.gz")):
        qid = p.name[: -len(".json.gz")]
        pb = b_dir / p.name
        q = by_id.get(qid)
        if q is None or not pb.exists():
            continue
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            da = json.load(fh)
        with gzip.open(pb, "rt", encoding="utf-8") as fh:
            db = json.load(fh)
        evidence = _evidence_texts(q)
        stored = _stored_texts(q)
        if not evidence:
            continue

        def surv(d: dict) -> set[str]:
            return {e["text"] for band in d["bands"] for e in band["entries"]}

        sa, sb = surv(da), surv(db)
        ra = len(sa & evidence) / len(evidence)
        rb = len(sb & evidence) / len(evidence)
        # Drop-set evidence fraction: of the stored-but-not-surviving
        # texts, how many were gold evidence? Lower = better selection.
        da_drop, db_drop = stored - sa, stored - sb
        fa = len(da_drop & evidence) / max(1, len(da_drop))
        fb = len(db_drop & evidence) / max(1, len(db_drop))
        a_rates.append(ra); b_rates.append(rb)
        a_drop_ev.append(fa); b_drop_ev.append(fb)
        questions.append({
            "question_id": qid, "n_evidence": len(evidence),
            "n_stored_texts": len(stored),
            "a_survivors": len(sa), "b_survivors": len(sb),
            "a_evidence_survival": round(ra, 4),
            "b_evidence_survival": round(rb, 4),
            "a_drop_evidence_frac": round(fa, 4),
            "b_drop_evidence_frac": round(fb, 4),
            "a_per_band_evidence": {
                band["name"]: sum(1 for e in band["entries"]
                                  if e["text"] in evidence)
                for band in da["bands"]},
        })

    if not questions:
        sys.exit("no paired dumps with evidence annotations found")

    def mean(xs):
        return sum(xs) / len(xs)

    deltas = [a - b for a, b in zip(a_rates, b_rates)]
    drop_deltas = [a - b for a, b in zip(a_drop_ev, b_drop_ev)]
    out = {
        "dataset": args.dataset, "extractor": args.extractor,
        "src_tag": args.src_tag,
        "a_preset": args.a_preset, "b_preset": args.b_preset,
        "n_questions": len(questions),
        "a_mean_evidence_survival": round(mean(a_rates), 4),
        "b_mean_evidence_survival": round(mean(b_rates), 4),
        "delta_mean": round(mean(deltas), 4),
        "delta_p_paired_perm_10k_seed0": _paired_permutation_p(deltas),
        "a_mean_drop_evidence_frac": round(mean(a_drop_ev), 4),
        "b_mean_drop_evidence_frac": round(mean(b_drop_ev), 4),
        "drop_delta_mean": round(mean(drop_deltas), 4),
        "drop_delta_p_paired_perm_10k_seed0":
            _paired_permutation_p(drop_deltas),
        "questions": questions,
    }
    stem = "-".join(p for p in (args.dataset, args.extractor,
                                args.src_tag) if p)
    dst = RESULTS_DIR / (f"longmemeval-ku-{stem}-evict-policy-"
                         f"{args.a_preset}-vs-{args.b_preset}.json")
    dst.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[{args.a_preset}] evidence survival {out['a_mean_evidence_survival']:.3f}  "
          f"vs [{args.b_preset}] {out['b_mean_evidence_survival']:.3f}  "
          f"delta {out['delta_mean']:+.3f}  p={out['delta_p_paired_perm_10k_seed0']:.4f}")
    print(f"drop-set evidence frac {out['a_mean_drop_evidence_frac']:.3f} vs "
          f"{out['b_mean_drop_evidence_frac']:.3f}  "
          f"p={out['drop_delta_p_paired_perm_10k_seed0']:.4f}")
    print(f"wrote {dst}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--dataset", default="oracle")
        p.add_argument("--extractor", default="e4b-ft")
        p.add_argument("--src-tag", default="arm1",
                       help="tag of the served source run ('' = untagged)")
        p.add_argument("--band-preset",
                       choices=("continuum", "flat", "scaled"),
                       default="continuum",
                       help="ingest band structure: the stock 8-band "
                            "continuum, ONE flat band (write-side "
                            "ablation — different entries survive), or "
                            "the 8-band layout proportionally scaled to "
                            "--scale-total (eviction-policy edge case)")

    p = sub.add_parser("replay", help="CPU ingest replay -> band-state dumps")
    common(p)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true",
                   help="re-replay questions whose dump already exists")
    p.add_argument("--flat-cap", type=int, default=None,
                   help="flat band capacity (default: the continuum "
                        "preset's total, currently 5250)")
    p.add_argument("--scale-total", type=int, default=None,
                   help="target total capacity for --band-preset scaled "
                        "(per-band caps scaled proportionally; the "
                        "realised sum after rounding is what the flat "
                        "comparison arm must use as --flat-cap)")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("rebuild",
                       help="offline policy rebuild -> tagged JSONLs")
    common(p)
    p.add_argument("--dry-run", action="store_true",
                   help="report agreement + 3-question side-by-side; no writes")
    p.add_argument("--recency", choices=("on", "off"), default="on",
                   help="'on' = the July ramp behaviour; 'off' = the "
                        "production default since 2026-07-25 "
                        "(recency_boost_enabled=False): no timestamp "
                        "term, single 'off' pseudo-mode")
    p.add_argument("--bm25", choices=("on", "off"), default="off",
                   help="'on' mirrors the production-default BM25 fusion "
                        "(global candidate pool). July runs were dense-only")
    p.add_argument("--half-life-base", type=float, default=None,
                   help="recency ramp base seconds (default: lib 3600; "
                        "the deployed daemon uses 86400)")
    p.add_argument("--modes", nargs="+", choices=("wall", "hist"),
                   default=list(MODES),
                   help="timestamp regimes to rebuild (recency=on only)")
    p.add_argument("--tag-prefix", default="abl",
                   help="tag family prefix; use 'abl25' for the v25 rerun "
                        "so July canonical artifacts are never overwritten")
    p.add_argument("--tag-mode-suffix", default="",
                   help="appended to the mode in tags (e.g. '24' -> "
                        "'hist24' for the 86400-base steelman arm)")
    p.set_defaults(fn=cmd_rebuild)

    p = sub.add_parser("evidence",
                       help="gold-evidence survival: eviction-policy "
                            "comparison over two capacity-scaled dumps")
    p.add_argument("--dataset", default="s")
    p.add_argument("--extractor", default="qwen-27b")
    p.add_argument("--src-tag", default="")
    p.add_argument("--a-preset", required=True,
                   help="dump-dir preset label of arm A (e.g. scaled257)")
    p.add_argument("--b-preset", required=True,
                   help="dump-dir preset label of arm B (e.g. flat257)")
    p.set_defaults(fn=cmd_evidence)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
