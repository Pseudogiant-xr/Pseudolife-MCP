"""evals/embedder_stamp.py — which embedder produced an eval's vectors.

``EmbeddingConfig.cpu_dtype="auto"`` resolves to bf16 on a CPU with native
bf16 and fp32 elsewhere (GitHub runners, most Intel CPUs), so the same
harness embeds at a different precision on a different host. The regression
gate scored identically either way on 2026-09-23, but nothing recorded
which precision a result file was produced at, so two runs could differ
silently. These tests pin the stamp, how stamps merge across rows, and
the rule that an absent stamp reads as "unknown", never as a mismatch.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import embedder_stamp  # noqa: E402

BF16 = {"backend": "torch", "device": "cpu", "dtype": "bf16"}
FP32 = {"backend": "torch", "device": "cpu", "dtype": "fp32"}
ONNX = {"backend": "onnx", "device": "cpu", "dtype": None}


class _Pipeline:
    def __init__(self, desc):
        self._desc = desc

    def describe(self):
        return self._desc


class _Service:
    def __init__(self, embedder):
        self._embedder = embedder


# ── describe ──────────────────────────────────────────────────────────────
def test_describe_reads_a_pipeline_and_returns_a_copy():
    desc = dict(BF16)
    got = embedder_stamp.describe(_Pipeline(desc))
    assert got == BF16
    got["dtype"] = "changed"
    assert desc["dtype"] == "bf16"


def test_describe_reads_a_memory_services_embedder():
    assert embedder_stamp.describe(_Service(_Pipeline(FP32))) == FP32


def test_describe_is_none_when_no_embedder_was_built(capsys):
    """A stamp must never cost a finished run its result file: a service
    whose embedder was never built describes as None, with a note."""
    assert embedder_stamp.describe(_Service(None)) is None
    assert "embedder" in capsys.readouterr().err


def test_describe_is_none_when_describe_raises(capsys):
    class Broken:
        def describe(self):
            raise RuntimeError("boom")

    assert embedder_stamp.describe(Broken()) is None
    assert "boom" in capsys.readouterr().err


def test_describe_reads_the_attribute_the_real_service_holds(tmp_path):
    """The duck-typed ``_embedder`` read is coupled to MemoryService's own
    attribute name; a rename there would silently stamp every run None."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=str(tmp_path))
    assert embedder_stamp.describe(svc) is None          # lazy, not built
    svc._embedder = _Pipeline(BF16)                       # noqa: SLF001
    assert embedder_stamp.describe(svc) == BF16


def test_describe_reads_the_real_pipeline_contract():
    """``EmbeddingPipeline.describe`` is what every stamp copies; pin its
    keys so a change there is a visible decision, not a silent drift."""
    from pseudolife_memory.memory.embedding import EmbeddingPipeline

    pipe = EmbeddingPipeline.__new__(EmbeddingPipeline)
    pipe.backend, pipe._device, pipe.dtype = "torch", "cpu", "bf16"
    assert embedder_stamp.describe(pipe) == BF16


# ── per-stage row stamps ──────────────────────────────────────────────────
def test_stamp_row_records_the_stage_and_keeps_earlier_stages():
    row = {"question_id": "q1"}
    embedder_stamp.stamp_row(row, "extract", _Pipeline(FP32))
    embedder_stamp.stamp_row(row, "rebuild_contexts", _Pipeline(BF16))
    assert row["embedder"] == {"extract": FP32, "rebuild_contexts": BF16}
    # a later pass of the same stage replaces only that stage
    embedder_stamp.stamp_row(row, "rebuild_contexts", _Pipeline(FP32))
    assert row["embedder"] == {"extract": FP32, "rebuild_contexts": FP32}


def test_stamping_a_shallow_copy_leaves_the_source_row_alone():
    """band_ablation derives several rows per source row with dict(row);
    they share the nested stamp dict, so stamping must not mutate it."""
    source = {"embedder": {"extract": FP32}}
    derived = dict(source)
    embedder_stamp.record_stage(derived, "band_ablation", BF16)
    assert source["embedder"] == {"extract": FP32}
    assert derived["embedder"] == {"extract": FP32, "band_ablation": BF16}


def test_stamp_row_records_none_when_nothing_describes(capsys):
    row = {}
    embedder_stamp.stamp_row(row, "extract", _Service(None))
    assert row["embedder"] == {"extract": None}


# ── merging rows into a summary ───────────────────────────────────────────
def test_merge_rows_is_empty_for_legacy_rows():
    assert embedder_stamp.merge_rows([{"question_id": "q"}] * 3) == {}


def test_merge_rows_collapses_agreeing_rows_to_one_description():
    rows = [{"embedder": {"extract": FP32, "rebuild_contexts": BF16}}
            for _ in range(3)]
    assert embedder_stamp.merge_rows(rows) == {
        "extract": FP32, "rebuild_contexts": BF16}


def test_merge_rows_counts_each_variant_when_rows_disagree():
    """A run resumed on another host (or across the stamping change) mixes
    precisions within one file; the summary must say so with counts, not
    report whichever row came first."""
    rows = ([{"embedder": {"extract": BF16}}] * 2
            + [{"embedder": {"extract": FP32}}]
            + [{"question_id": "legacy"}])
    merged = embedder_stamp.merge_rows(rows)
    assert merged == {"extract": {"mixed": [
        {"embedder": BF16, "rows": 2},
        {"embedder": FP32, "rows": 1},
        {"embedder": None, "rows": 1},
    ]}}


def test_merge_rows_counts_a_none_stamp_and_a_missing_one_as_one_unknown():
    rows = [{"embedder": {"extract": BF16}}, {"embedder": {"extract": None}},
            {"question_id": "legacy"}]
    assert embedder_stamp.merge_rows(rows) == {"extract": {"mixed": [
        {"embedder": BF16, "rows": 1},
        {"embedder": None, "rows": 2},
    ]}}


# ── labels ────────────────────────────────────────────────────────────────
def test_labels_read_absent_and_null_as_unknown():
    assert embedder_stamp.labels(None) == {}
    assert embedder_stamp.labels("unknown") == {}
    assert embedder_stamp.labels({}) == {}
    assert embedder_stamp.labels({"extract": None}) == {}


def test_labels_name_the_dtype_and_fall_back_to_the_backend_for_onnx():
    assert embedder_stamp.labels({"extract": FP32, "rebuild_contexts": BF16}
                                 ) == {"extract": {"fp32"},
                                       "rebuild_contexts": {"bf16"}}
    # ONNX reports no resident dtype: its precision is the artifact's.
    assert embedder_stamp.labels({"extract": ONNX}) == {"extract": {"onnx"}}


def test_labels_of_a_mixed_stage_are_its_known_variants():
    merged = embedder_stamp.merge_rows(
        [{"embedder": {"extract": BF16}}, {"embedder": {"extract": FP32}},
         {}])
    assert embedder_stamp.labels(merged) == {"extract": {"bf16", "fp32"}}


# ── warnings ──────────────────────────────────────────────────────────────
def test_no_warning_when_both_sides_agree():
    same = {"rebuild_contexts": BF16}
    assert embedder_stamp.precision_warnings(same, dict(same)) == []


def test_warns_when_a_stage_differs_between_the_two_runs():
    warnings = embedder_stamp.precision_warnings(
        {"rebuild_contexts": BF16}, {"rebuild_contexts": FP32},
        a_label="run", b_label="baseline")
    assert len(warnings) == 1
    w = warnings[0]
    assert "rebuild_contexts" in w and "bf16" in w and "fp32" in w
    assert "run" in w and "baseline" in w


def test_absent_stamps_are_unknown_not_a_mismatch():
    """The rule the whole feature rests on: every artifact written before
    this change carries no stamp, and must compare silently."""
    stamped = {"rebuild_contexts": BF16}
    for legacy in (None, {}, "unknown"):
        assert embedder_stamp.precision_warnings(stamped, legacy) == []
        assert embedder_stamp.precision_warnings(legacy, stamped) == []
    assert embedder_stamp.precision_warnings(None, None) == []
    # a stage only one side recorded is unknown on the other
    assert embedder_stamp.precision_warnings(
        {"extract": FP32, "rebuild_contexts": BF16},
        {"rebuild_contexts": BF16}) == []


def test_warns_when_one_run_mixes_precisions_within_a_stage():
    mixed = embedder_stamp.merge_rows(
        [{"embedder": {"extract": BF16}}, {"embedder": {"extract": FP32}}])
    warnings = embedder_stamp.precision_warnings(mixed, None,
                                                 a_label="side-A")
    assert len(warnings) == 1
    assert "side-A" in warnings[0] and "mixes" in warnings[0]


def test_a_rebuilt_run_warns_against_its_own_source():
    """The knob-tuning workflow: extract `diag` in fp32, rebuild it into
    `diag-knobs` on a bf16 host, compare the two. The source has no
    rebuild_contexts stage because its cortex arm was ranked by its extract
    embedder, which is known — so the stage inherits it and differs."""
    rebuilt = {"extract": FP32, "rebuild_contexts": BF16}
    source = {"extract": FP32}
    for a, b in ((rebuilt, source), (source, rebuilt)):
        warnings = embedder_stamp.precision_warnings(a, b)
        assert len(warnings) == 1
        assert "rebuild_contexts" in warnings[0]
        assert "bf16" in warnings[0] and "fp32" in warnings[0]
    # a rebuild of an unstamped source vs a fresh fp32 extract: the
    # rebuilt cortex ranking (bf16) vs the extract's own (fp32)
    assert len(embedder_stamp.precision_warnings(
        {"rebuild_contexts": BF16}, {"extract": FP32})) == 1


def test_an_unknown_base_stage_is_inherited_as_unknown_not_a_mismatch():
    # A's extract predates stamping; its rebuild agrees with B's.
    assert embedder_stamp.precision_warnings(
        {"rebuild_contexts": BF16},
        {"extract": FP32, "rebuild_contexts": BF16}) == []
    # both rebuilt at the same precision from the same fp32 source
    assert embedder_stamp.precision_warnings(
        {"extract": FP32, "rebuild_contexts": BF16},
        {"extract": FP32, "rebuild_contexts": BF16}) == []


def test_a_stage_recorded_as_unknown_never_inherits_the_base():
    """Only a MISSING stage inherits from extract. A stage that ran but was
    recorded as None (describe() failed soft, or band_ablation rebuilt a
    pre-stamp dump) is unknown, and unknown is never a mismatch. Repro from
    the Codex review of PR #340 (2026-09-23)."""
    a = {"extract": BF16, "rebuild_contexts": None}
    b = {"extract": BF16, "rebuild_contexts": FP32}
    assert embedder_stamp.precision_warnings(a, b, "A", "B") == []
    assert embedder_stamp.precision_warnings(b, a, "B", "A") == []
    # against a side that never ran the stage, too: only extract differs
    warnings = embedder_stamp.precision_warnings(a, {"extract": FP32})
    assert len(warnings) == 1 and "embedder extract:" in warnings[0]
    # a merged stage whose every row was None is the same unknown
    merged = embedder_stamp.merge_rows(
        [{"embedder": {"extract": BF16, "band_ablation": None}}] * 2)
    assert embedder_stamp.precision_warnings(
        merged, {"extract": BF16, "band_ablation": FP32}) == []


def test_summary_line_names_a_stage_recorded_as_unknown():
    line = embedder_stamp.summary_line(
        ("run", {"extract": BF16, "rebuild_contexts": None}))
    assert "extract=bf16" in line and "rebuild_contexts=unknown" in line


def test_a_crossed_pair_warns_on_each_stage_it_differs_on():
    a = {"extract": FP32, "rebuild_contexts": BF16}
    b = {"extract": FP32, "band_ablation": BF16}
    stages = " ".join(embedder_stamp.precision_warnings(a, b))
    assert "rebuild_contexts" in stages and "band_ablation" in stages


def test_rag_lite_rebuild_never_triggers_a_warning():
    """rag_lite_rebuild refuses to write unless its re-derived ranking is
    byte-identical to the judged control, so its precision provably did
    not move anything; it is recorded, not compared."""
    assert embedder_stamp.precision_warnings(
        {"extract": FP32, "rag_lite_rebuild": BF16}, {"extract": FP32}) == []


def test_labels_read_a_flat_description_as_one_stage():
    """Single-run artifacts carry the description itself; reading its keys
    as stage names would make every flat stamp silently unknown."""
    assert embedder_stamp.labels(BF16) == {"run": {"bf16"}}
    assert embedder_stamp.labels(ONNX) == {"run": {"onnx"}}
    assert len(embedder_stamp.precision_warnings(BF16, FP32)) == 1


def test_summary_line_names_unknown_sides():
    line = embedder_stamp.summary_line(("run", {"rebuild_contexts": BF16}),
                                       ("baseline", None))
    assert "rebuild_contexts=bf16" in line and "baseline unknown" in line
    assert embedder_stamp.summary_line(("run", None)) == (
        "embedder precision: run unknown")


def test_module_stays_stdlib_only():
    """replicate.py imports this and must not pull torch (its own lazy-
    import test runs a clean interpreter)."""
    evals_dir = str(Path(__file__).resolve().parents[1] / "evals")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "import embedder_stamp; "
            "hit = sorted({'torch', 'pseudolife_memory'} & "
            "{m.split('.')[0] for m in sys.modules}); "
            "sys.exit(('heavy imports: ' + ', '.join(hit)) if hit else 0)")
    subprocess.run([sys.executable, "-c", code, evals_dir], check=True)


# ── helpers for harnesses that derive rows or bypass EmbeddingPipeline ────
def test_carry_copies_the_stamp_onto_a_derived_dict():
    """warm_cache_probe and beam_reader_sweep build their output rows by
    picking named fields, which drops the stamp unless it is carried."""
    src = {"embedder": {"serve": BF16}, "other": 1}
    assert embedder_stamp.carry(src, {"picked": 1}) == {
        "picked": 1, "embedder": {"serve": BF16}}
    assert embedder_stamp.carry({"other": 1}, {"picked": 1}) == {"picked": 1}


def test_describe_model_reads_the_dtype_back_from_the_parameters():
    """embedder_recall.py loads bake-off arms with a bare
    SentenceTransformer, bypassing cpu_dtype, so its precision is whatever
    the installed stack defaults to — read it back, don't assume it."""
    from types import SimpleNamespace

    def model(*dtypes):
        return SimpleNamespace(
            parameters=lambda: [SimpleNamespace(dtype=d) for d in dtypes])

    assert embedder_stamp.describe_model(
        model("torch.bfloat16", "torch.bfloat16"), device="cpu") == BF16
    assert embedder_stamp.describe_model(
        model("torch.float32", "torch.bfloat16"), device="cuda:0") == {
            "backend": "torch", "device": "cuda:0", "dtype": "mixed"}
    assert embedder_stamp.describe_model(object(), device="cpu",
                                         backend="llama.cpp") == {
        "backend": "llama.cpp", "device": "cpu", "dtype": None}


def test_describe_model_labels_a_real_torch_module():
    torch = __import__("pytest").importorskip("torch")
    layer = torch.nn.Linear(2, 2).to(torch.bfloat16)
    assert embedder_stamp.describe_model(layer, device="cpu") == BF16
    assert embedder_stamp.describe_model(torch.nn.Linear(2, 2),
                                         device="cpu") == FP32
