"""Record which embedder produced an eval's vectors, and compare records.

``EmbeddingConfig.cpu_dtype="auto"`` loads the torch embedder in bf16 on a
CPU with native bf16 and in fp32 everywhere else (GitHub runners, most Intel
CPUs), and ``PSEUDOLIFE_EMBEDDING_CPU_DTYPE`` overrides it per process. The
same harness therefore embeds at a different precision on a different host.
The regression gate scored every arm identically in bf16 and fp32
(2026-09-23), but a result file that does not say which precision produced
it lets two runs differ silently, so every harness that builds an embedder
stamps ``EmbeddingPipeline.describe()`` ({backend, device, dtype}) into what
it writes.

Two shapes, both under the ``embedder`` key:

* a single-run result file carries the description itself;
* a LongMemEval/BEAM row carries one description PER STAGE that embedded
  its contexts: ``{"extract": {...}, "rebuild_contexts": {...}}``. A
  rebuilt row's contexts come from two embedders (``rebuild_contexts.py``
  re-ranks the cortex facts and copies the rag block verbatim), so one
  description could not say which arm a precision applies to.

A missing stamp, a ``None`` description and the literal ``"unknown"`` all
read as unknown, and unknown is never a mismatch: every artifact written
before stamping existed carries none.

Stdlib only. ``replicate.py`` imports this and must stay torch-free.
"""
from __future__ import annotations

import json
import sys

KEY = "embedder"
UNKNOWN = "unknown"
# The stage that first builds a row's contexts. A later stage a row did not
# pass through (a rebuild) left that part of its contexts as the base stage
# built it, so for comparison the missing stage inherits the base's
# precision. A missing BASE stage is unknown.
_BASE_STAGES = ("extract", "serve")
# Recorded, never compared: rag_lite_rebuild refuses to write unless its
# re-derived ranking is byte-identical to the judged control's, so its
# precision provably moved nothing.
_UNCOMPARED_STAGES = frozenset({"rag_lite_rebuild"})


def describe(source) -> dict | None:
    """The resolved description of ``source``'s embedder, or None.

    ``source`` is an ``EmbeddingPipeline`` or anything holding one as
    ``_embedder`` (a ``MemoryService``). None when no embedder was built or
    describing it failed: a stamp must never cost a finished run its result
    file, so the failure is reported on stderr instead of raised.
    """
    embedder = source if hasattr(source, "describe") else getattr(
        source, "_embedder", None)
    if embedder is None or not hasattr(embedder, "describe"):
        print("embedder stamp: no embedder was built; recording none",
              file=sys.stderr)
        return None
    try:
        return dict(embedder.describe())
    except Exception as exc:  # noqa: BLE001 — never fail the result write
        print(f"embedder stamp: describe() failed ({exc}); recording none",
              file=sys.stderr)
        return None


_TORCH_DTYPE_LABELS = {"torch.float32": "fp32", "torch.bfloat16": "bf16",
                       "torch.float16": "fp16"}


def describe_model(model, device: str, backend: str = "torch") -> dict:
    """``{backend, device, dtype}`` for a model loaded WITHOUT
    ``EmbeddingPipeline`` (embedder_recall.py's bake-off arms): no
    cpu_dtype applies there, so the dtype is read back from the parameters
    the way ``EmbeddingPipeline.describe()`` reads it — one dtype names
    it, several read ``"mixed"``, none (a non-torch model) reads None.
    """
    try:
        found = {str(p.dtype) for p in model.parameters()}
    except (AttributeError, TypeError):
        found = set()
    if len(found) == 1:
        (name,) = found
        dtype = _TORCH_DTYPE_LABELS.get(name, name.removeprefix("torch."))
    else:
        dtype = "mixed" if found else None
    return {"backend": backend, "device": device, "dtype": dtype}


def carry(src: dict, dst: dict) -> dict:
    """Copy ``src``'s stamp onto ``dst``, a dict derived from it by picking
    named fields (which would otherwise drop the stamp)."""
    if KEY in src:
        dst[KEY] = src[KEY]
    return dst


def record_stage(row: dict, stage: str, desc: dict | None) -> dict:
    """Record an already-read description as ``stage``'s, keeping other
    stages (for a caller whose embedder is gone by the time it writes)."""
    stages = row.get(KEY)
    # A copy, never the row's own dict: rows derived by ``dict(row)`` share
    # it, and stamping one must not stamp its siblings.
    stages = dict(stages) if isinstance(stages, dict) else {}
    stages[stage] = desc
    row[KEY] = stages
    return row


def stamp_row(row: dict, stage: str, source) -> dict:
    """Record ``stage``'s embedder on ``row``, keeping other stages."""
    return record_stage(row, stage, describe(source))


def _canonical(desc) -> str:
    return json.dumps(desc, sort_keys=True)


def merge_rows(rows: list[dict]) -> dict:
    """Stage -> the one description every row agrees on.

    When the rows disagree (a run resumed on another host, or across the
    change that added stamping), the stage becomes
    ``{"mixed": [{"embedder": desc, "rows": n}, ...]}`` in first-seen
    order, with the rows that carry no stamp for that stage counted as
    ``None``. Empty when no row carries a stamp, so a legacy artifact's
    summary keeps its shape.
    """
    counts: dict[str, dict[str, list]] = {}
    for row in rows:
        stages = row.get(KEY)
        if not isinstance(stages, dict):
            continue
        for stage, desc in stages.items():
            variants = counts.setdefault(stage, {})
            variants.setdefault(_canonical(desc), [desc, 0])[1] += 1
    merged: dict = {}
    for stage, variants in counts.items():
        unstamped = len(rows) - sum(n for _, n in variants.values())
        if unstamped:
            # A row with no stamp for this stage and one stamped None are
            # the same unknown; count them as one variant.
            variants.setdefault(_canonical(None), [None, 0])[1] += unstamped
        if len(variants) == 1:
            merged[stage] = next(iter(variants.values()))[0]
            continue
        merged[stage] = {"mixed": [{"embedder": desc, "rows": n}
                                   for desc, n in variants.values()]}
    return merged


def _label(desc) -> str | None:
    """A description's precision label. ONNX reports no resident dtype (its
    precision is the artifact's), so it is labelled by its backend and
    compares unequal to every torch dtype."""
    if not isinstance(desc, dict):
        return None
    return desc.get("dtype") or desc.get("backend") or None


def _is_description(stamp: dict) -> bool:
    return "backend" in stamp and not isinstance(stamp["backend"], dict)


def _stages(stamp) -> dict:
    """Stage -> recorded description; a single-run artifact's flat
    description reads as one stage, ``run``."""
    if not isinstance(stamp, dict):
        return {}
    return {"run": stamp} if _is_description(stamp) else stamp


def labels(stamp) -> dict[str, set[str]]:
    """Stage -> the known precision labels in a stamp.

    Unknown contributes nothing: an absent stamp, ``"unknown"``, a None
    description, or a stage every variant of which is None.
    """
    out: dict[str, set[str]] = {}
    for stage, desc in _stages(stamp).items():
        if isinstance(desc, dict) and "mixed" in desc:
            found = {_label(v.get("embedder")) for v in desc["mixed"]}
        else:
            found = {_label(desc)}
        found.discard(None)
        if found:
            out[stage] = found
    return out


def _fmt(found: set[str]) -> str:
    return "+".join(sorted(found))


def precision_warnings(a, b, a_label: str = "a",
                       b_label: str = "b") -> list[str]:
    """Why two runs' embedder precisions do not match, if they do not.

    Per side: a warning when one run mixes precisions within a stage.
    Per stage either side recorded: a warning when the two sides' known
    labels differ. A side that never RAN a later stage (a source run
    against its rebuild) is compared through its base stage, which built
    that part of its contexts. A stage that ran but was recorded as None
    stays unknown and never inherits, and neither does a missing base
    stage: unknown is never a mismatch.
    """
    la, lb = labels(a), labels(b)
    ran_a, ran_b = set(_stages(a)), set(_stages(b))
    out = []
    for side, found_by_stage in ((a_label, la), (b_label, lb)):
        for stage, found in sorted(found_by_stage.items()):
            if len(found) > 1:
                out.append(
                    f"embedder {stage}: {side} mixes precisions "
                    f"({_fmt(found)}) — its rows were embedded at "
                    f"different precisions")

    def effective(found_by_stage: dict, ran: set,
                  stage: str) -> tuple[set, str]:
        if stage in found_by_stage:
            return found_by_stage[stage], ""
        if stage in ran or stage in _BASE_STAGES:
            return set(), ""                  # recorded unknown / no base
        base = [s for s in _BASE_STAGES if s in found_by_stage]
        inherited = set().union(*(found_by_stage[s] for s in base))
        return inherited, (f" (its {'/'.join(base)} stage)" if base else "")

    for stage in sorted((set(la) | set(lb)) - _UNCOMPARED_STAGES):
        ea, note_a = effective(la, ran_a, stage)
        eb, note_b = effective(lb, ran_b, stage)
        if ea and eb and ea != eb:
            out.append(
                f"embedder {stage}: {a_label} {_fmt(ea)}{note_a} vs "
                f"{b_label} {_fmt(eb)}{note_b} — the runs embedded at "
                f"different precisions, so a delta between them may be "
                f"the embedder, not the change under test")
    return out


def summary_line(*sides: tuple[str, object]) -> str:
    """One line naming each ``(label, stamp)`` side's recorded precision,
    or that it is unknown, so a silent comparison is not mistaken for a
    checked one."""
    parts = []
    for side, stamp in sides:
        found = labels(stamp)
        if found:
            parts.append(f"{side} " + ", ".join(
                f"{stage}={_fmt(found[stage]) if stage in found else UNKNOWN}"
                for stage in sorted(_stages(stamp))))
        else:
            parts.append(f"{side} {UNKNOWN}")
    return "embedder precision: " + "; ".join(parts)
