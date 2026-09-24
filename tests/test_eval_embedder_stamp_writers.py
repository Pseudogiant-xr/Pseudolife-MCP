"""Every eval harness that builds an embedder stamps it into what it writes.

Behavioural tests for the writers whose output feeds a comparison (the
LongMemEval/BEAM rows and summaries, the two offline rebuilds), plus a
static guard over ``evals/``: any entry point that constructs an embedder,
directly or through a helper such as ``ladder_sweep.build_service``, must
call ``embedder_stamp``. The helper itself is tested in
``test_eval_embedder_stamp.py``.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import longmemeval_bench as lmb  # noqa: E402

BF16 = {"backend": "torch", "device": "cpu", "dtype": "bf16"}
FP32 = {"backend": "torch", "device": "cpu", "dtype": "fp32"}


class _Pipe:
    def __init__(self, desc=BF16):
        self._desc = desc

    def describe(self):
        return dict(self._desc)


def _summary(out: Path) -> dict:
    return json.loads(out.with_name(
        out.name.removesuffix(".jsonl") + ".summary.json").read_text(
            encoding="utf-8"))


# ── LongMemEval: extract rows + report summary ────────────────────────────
class _BenchSvc:
    def __init__(self):
        self.config = SimpleNamespace(
            memory=SimpleNamespace(dream=SimpleNamespace()))
        self._embedder = _Pipe(BF16)

    def flush(self):
        pass


def test_lme_extract_rows_record_the_extract_embedder(tmp_path, monkeypatch):
    q = {"question_id": "q1", "question": "which bike?",
         "question_type": "knowledge-update", "answer": "Trek",
         "question_date": "2023/01/01", "haystack_sessions": [[]]}
    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(lmb, "probe", lambda url: True)
    monkeypatch.setattr(lmb, "load_questions", lambda *a, **kw: [q])
    monkeypatch.setattr(lmb.tempfile, "mkdtemp",
                        lambda prefix="": str(tmp_path / "bank"))
    monkeypatch.setattr(lmb, "build_service", lambda tmp: _BenchSvc())
    monkeypatch.setattr(lmb, "_make_extractor", lambda *a, **kw: object())
    monkeypatch.setattr(lmb, "ingest_and_dream",
                        lambda *a: {"turns": 1, "superseded": 0})
    monkeypatch.setattr(lmb, "build_contexts",
                        lambda svc, question, variants=False:
                        {"rag": "r", "cortex": "c", "hybrid": "h"})
    monkeypatch.setattr(lmb, "dump_bank", lambda svc, q, path: [])
    lmb.run_extract("oracle", None, "gemma-e2b", do_answer=False, tag="t")
    rows = lmb.load_rows(lmb.out_file("oracle", "gemma-e2b", "t"))
    assert rows[0]["embedder"] == {"extract": BF16}


def _judged(qid: str, stamp: dict | None) -> dict:
    row = {"question_id": qid, "question": "which bike?", "answer": "Trek",
           "question_type": "knowledge-update",
           "consolidation": {"superseded": 0}}
    for arm in lmb.ARMS:
        row[f"{arm}_correct"] = True
        row[f"{arm}_context_tokens"] = 10
        row[f"{arm}_response"] = "Trek"
    if stamp is not None:
        row["embedder"] = stamp
    return row


def _report(tmp_path, monkeypatch, rows) -> dict:
    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    out = lmb.out_file("oracle", "qwen-27b", "t")
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    lmb.report("oracle", "qwen-27b", "t")
    return _summary(out)


def test_lme_report_carries_the_rows_embedder(tmp_path, monkeypatch):
    stamp = {"extract": FP32, "rebuild_contexts": BF16}
    summary = _report(tmp_path, monkeypatch,
                      [_judged("q1", stamp), _judged("q2", stamp)])
    assert summary["embedder"] == stamp


def test_lme_report_says_when_rows_mix_precisions(tmp_path, monkeypatch):
    summary = _report(tmp_path, monkeypatch,
                      [_judged("q1", {"extract": BF16}),
                       _judged("q2", {"extract": FP32})])
    assert summary["embedder"]["extract"]["mixed"] == [
        {"embedder": BF16, "rows": 1}, {"embedder": FP32, "rows": 1}]


def test_lme_report_of_legacy_rows_has_no_embedder_key(tmp_path, monkeypatch):
    summary = _report(tmp_path, monkeypatch, [_judged("q1", None)])
    assert "embedder" not in summary


# ── rebuild_contexts.py: its own stage, the extract stage kept ────────────
class _RebuildPipe(_Pipe):
    def __init__(self, config=None):
        super().__init__(BF16)

    def encode(self, texts):
        import torch
        return torch.ones(len(texts), 4)

    def encode_query(self, text):
        import torch
        return torch.ones(4)


def test_rebuild_contexts_stamps_its_stage_and_keeps_extracts(tmp_path,
                                                              monkeypatch):
    """The regression gate's precision lives here: stage 1 re-ranks the
    cortex facts with a fresh CPU embedder while the rag block stays the
    source run's. Both provenances must survive on the row."""
    pytest.importorskip("torch")
    import rebuild_contexts
    from context_format import FACTS_HEADER, MEMS_HEADER

    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr("pseudolife_memory.memory.embedding.EmbeddingPipeline",
                        _RebuildPipe)
    src = lmb.out_file("oracle", "e4b-ft", "arm1")
    row = {"question_id": "q1", "question": "which bike?",
           "contexts": {"rag": "r", "cortex": "old",
                        "hybrid": FACTS_HEADER + "old" + MEMS_HEADER + "raw"},
           "embedder": {"extract": FP32}}
    src.write_text(json.dumps(row) + "\n", encoding="utf-8")
    banks = lmb.bank_dir("oracle", "e4b-ft", "arm1")
    banks.mkdir(parents=True)
    with gzip.open(banks / "q1.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"question": "which bike?", "facts": [
            {"entity": "bike", "attribute": "model", "value": "Trek"}]}, fh)
    monkeypatch.setattr(sys, "argv", [
        "rebuild_contexts.py", "--dataset", "oracle", "--extractor", "e4b-ft",
        "--src-tag", "arm1", "--out-tag", "arm1-gate"])
    assert rebuild_contexts.main() == 0
    out = lmb.load_rows(lmb.out_file("oracle", "e4b-ft", "arm1-gate"))
    assert out[0]["embedder"] == {"extract": FP32, "rebuild_contexts": BF16}


# ── rag_lite_rebuild.py: its own stage ────────────────────────────────────
class _RagLiteSvc:
    def __init__(self, turns):
        self._turns = turns
        self._embedder = _Pipe(BF16)

    def store(self, text, source=None):
        pass

    def search(self, query, **kw):
        return {"entries": [{"text": t} for t in self._turns]}

    def flush(self):
        pass


def test_rag_lite_rebuild_stamps_its_stage(tmp_path, monkeypatch):
    import rag_lite_rebuild as rlr

    raw = ["turn one " + "a" * 60, "turn two " + "b" * 60]
    src = tmp_path / "longmemeval-ku-oracle-qwen-27b-src.jsonl"
    row = {"question_id": "q1", "question": "which bike?",
           "contexts": {"rag": "\n\n".join(raw), "cortex": "f",
                        "hybrid": "h"},
           "embedder": {"extract": FP32}}
    src.write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(lmb, "load_questions",
                        lambda *a, **kw: [{"question_id": "q1",
                                           "question": "which bike?"}])
    monkeypatch.setattr(rlr, "question_turns", lambda q: raw)
    monkeypatch.setattr(rlr, "build_service", lambda tmp: _RagLiteSvc(raw))
    assert rlr.main(["--src-tag", "src", "--out-tag", "out",
                     "--rag-lite-top-k", "1"]) == 0
    out = lmb.load_rows(tmp_path / "longmemeval-ku-oracle-qwen-27b-out.jsonl")
    assert out[0]["embedder"] == {"extract": FP32, "rag_lite_rebuild": BF16}


# ── guard: every entry point that builds an embedder stamps it ───────────
import ast  # noqa: E402

EVALS = Path(__file__).resolve().parents[1] / "evals"
# Calls that construct an embedder in-process. Reached directly or through
# any evals function that (transitively) makes one, e.g. build_service.
_CONSTRUCTORS = {"EmbeddingPipeline", "MemoryService", "SentenceTransformer"}
_STAMP_MODULE = "embedder_stamp"
# The helpers that WRITE a stamp (or carry one onto a derived dict).
_WRITERS = {"describe", "describe_model", "stamp_row", "record_stage",
            "carry"}


def _eval_modules() -> dict[str, ast.Module]:
    skip = {"results", "data", "models", "prompts"}
    out = {}
    for path in sorted(EVALS.rglob("*.py")):
        rel = path.relative_to(EVALS)
        if skip & set(rel.parts[:-1]) or path.stem == _STAMP_MODULE:
            continue
        out[path.stem] = ast.parse(path.read_text(encoding="utf-8"),
                                   filename=str(path))
    return out


def _module_name(name: str | None) -> str | None:
    if not name:
        return None
    parts = name.split(".")
    return parts[-1] if parts[0] == "evals" or len(parts) == 1 else None


class _Calls(ast.NodeVisitor):
    """Per function (and ``<module>`` for top-level code): the calls made,
    the names bound to a call's result, and what is returned, plus the
    module's import aliases. Functions are keyed by bare name, so two
    same-named defs merge — conservative for a guard."""

    def __init__(self):
        self.stack = ["<module>"]
        self.calls: dict[str, list[ast.expr]] = {"<module>": []}
        self.assigns: dict[str, dict[str, list[ast.expr]]] = {"<module>": {}}
        self.returns: dict[str, list[ast.expr]] = {"<module>": []}
        self.aliases: dict[str, str] = {}               # alias -> module
        self.names: dict[str, tuple[str, str]] = {}     # name -> (mod, attr)

    def _enter(self, node):
        self.calls.setdefault(node.name, [])
        self.assigns.setdefault(node.name, {})
        self.returns.setdefault(node.name, [])
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def visit_Import(self, node):
        for a in node.names:
            mod = _module_name(a.name)
            if mod:
                self.aliases[a.asname or mod] = mod

    def visit_ImportFrom(self, node):
        mod = _module_name(node.module)
        for a in node.names:
            if mod:
                self.names[a.asname or a.name] = (mod, a.name)
            if a.name in _CONSTRUCTORS:
                self.names[a.asname or a.name] = ("<ctor>", a.name)

    def visit_Assign(self, node):
        if isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.assigns[self.stack[-1]].setdefault(
                        target.id, []).append(node.value.func)
        self.generic_visit(node)

    def visit_Return(self, node):
        if node.value is not None:
            self.returns[self.stack[-1]].append(node.value)
        self.generic_visit(node)

    def visit_Call(self, node):
        self.calls[self.stack[-1]].append(node.func)
        self.generic_visit(node)


def _resolve(func: ast.expr, mod: str, info: _Calls,
             defined: set[str]) -> tuple[str, str] | None:
    if isinstance(func, ast.Name):
        if func.id in info.names:
            return info.names[func.id]
        if func.id in _CONSTRUCTORS:
            return ("<ctor>", func.id)
        if func.id in defined:
            return (mod, func.id)
        return None
    if isinstance(func, ast.Attribute):
        if func.attr in _CONSTRUCTORS:
            return ("<ctor>", func.attr)
        if isinstance(func.value, ast.Name) and func.value.id in info.aliases:
            return (info.aliases[func.value.id], func.attr)
    return None


def _analyse() -> dict:
    """The construction map of evals/.

    * ``factories`` — functions that RETURN what a constructor or another
      factory built (the build_service helpers); they hand the embedder on
      and stamp nothing themselves.
    * ``sites`` — every other function that calls a constructor or a
      factory: the place the embedder is used, so the place it is stamped.
    * ``constructs`` — modules reaching a constructor through any chain of
      calls (warm_cache_probe -> ls.run_rung -> build_service).
    * ``writes`` — functions calling a stamp-WRITING helper; merge_rows,
      labels and the warning helpers read stamps and do not count.
    """
    modules = _eval_modules()
    infos, edges = {}, {}
    for mod, tree in modules.items():
        info = _Calls()
        info.visit(tree)
        infos[mod] = info
        defined = set(info.calls) - {"<module>"}
        for fn, funcs in info.calls.items():
            edges[(mod, fn)] = {t for f in funcs
                                if (t := _resolve(f, mod, info, defined))}

    factories: set[tuple[str, str]] = set()

    def builds(target) -> bool:
        return target[0] == "<ctor>" or target in factories

    changed = True
    while changed:
        changed = False
        for mod, info in infos.items():
            defined = set(info.calls) - {"<module>"}
            for fn, returned in info.returns.items():
                if (mod, fn) in factories:
                    continue
                for value in returned:
                    funcs = ([value.func] if isinstance(value, ast.Call)
                             else info.assigns[fn].get(value.id, [])
                             if isinstance(value, ast.Name) else [])
                    if any((t := _resolve(f, mod, info, defined))
                           and builds(t) for f in funcs):
                        factories.add((mod, fn))
                        changed = True
                        break

    building = {node for node, targets in edges.items()
                if any(t[0] == "<ctor>" for t in targets)}
    changed = True
    while changed:
        changed = False
        for node, targets in edges.items():
            if node not in building and targets & building:
                building.add(node)
                changed = True
    writes = {node for node, targets in edges.items()
              if any(t[0] == _STAMP_MODULE and t[1] in _WRITERS
                     for t in targets)}
    sites = {node for node, targets in edges.items()
             if node not in factories and any(builds(t) for t in targets)}
    return {"factories": factories, "sites": sites, "writes": writes,
            "constructs": {mod for mod, _ in building},
            "stamps": {mod for mod, _ in writes}}


def test_the_guard_sees_every_known_construction_path():
    """A guard that silently flags nothing passes everything. Pin one
    module per construction route: EmbeddingPipeline, MemoryService,
    SentenceTransformer, build_service imported by name, ls.build_service
    through a module alias, and a helper two hops away (ls.run_rung)."""
    found = _analyse()
    assert {"rebuild_contexts", "retrieval_replay", "embedder_recall",
            "longmemeval_bench", "quarantine_gate", "warm_cache_probe",
            } <= found["constructs"]
    # the ones that only read rows back are not flagged
    assert not {"replicate", "leak_check", "lme_rejudge", "context_format",
                } & found["constructs"]
    # factories are recognised through a chain (seed_bench's helper returns
    # what ladder_sweep.build_service built), and are not sites themselves
    assert {("ladder_sweep", "build_service"),
            ("seed_bench", "_seed_bench_service"),
            ("live_replay_flat_ab", "_build")} <= found["factories"]
    assert {("longmemeval_bench", "run_extract"), ("beam_adapter", "run"),
            ("seed_bench", "run")} <= found["sites"]


def test_every_construction_site_writes_the_stamp():
    """Where an embedder is used, it is stamped: every function that calls
    a constructor or a build_service-style factory must call a stamp-writing
    helper itself. A module-wide check would let report()'s merge_rows
    stand in for a deleted row stamp in the run loop (2026-09-23 review)."""
    found = _analyse()
    missing = sorted(found["sites"] - found["writes"])
    assert not missing, (
        f"these functions build or use an embedder but write no stamp: "
        f"{missing}")


def test_every_eval_that_builds_an_embedder_stamps_it():
    """cpu_dtype "auto" is bf16 on a native-bf16 CPU and fp32 elsewhere,
    so an unstamped result file cannot say which precision produced it.
    Any evals module that reaches a constructor — also through a runner
    that stamps its own result, which the caller can still drop (as
    warm_cache_probe's field-picking did) — must call a stamp-writing
    helper (into its result file; stdout for the two stdout-only probes)."""
    found = _analyse()
    missing = sorted(found["constructs"] - found["stamps"])
    assert not missing, (
        f"these evals build an embedder but write no stamp: {missing}")


# ── the other comparison-feeding writers ──────────────────────────────────
def _beam_rows(stamp):
    rows = [{"chat_id": "1", "type": "abstention", "index": i,
             "rag_score": 1.0, "rag_score_intfaithful": 1.0}
            for i in range(2)]
    if stamp is not None:
        for r in rows:
            r["embedder"] = stamp
    return rows


@pytest.mark.parametrize("stamp", [{"extract": BF16}, None])
def test_beam_report_carries_the_rows_embedder(tmp_path, monkeypatch, stamp):
    import beam_adapter

    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    out = tmp_path / "beam-100K-qwen-27b-t.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in _beam_rows(stamp)),
                   encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "t")
    summary = _summary(out)
    if stamp is None:
        assert "embedder" not in summary          # legacy shape kept
    else:
        assert summary["embedder"] == stamp


def test_beam_reader_sweep_answer_rows_keep_the_serve_stamp():
    """process_row picks named fields off the serve row; the stamp of the
    embedder that ranked raw_entries must survive into the judged row."""
    import beam_reader_sweep as brs

    serve_row = {"chat_id": "1", "tier": "100K", "type": "abstention",
                 "index": 0, "question": "q?", "difficulty": "easy",
                 "rubric": ["item"], "serve_top_k": 2,
                 "raw_entries": ["a", "b"], "embedder": {"serve": BF16}}
    out = brs.process_row(
        serve_row, (1,), "system", lambda system, prompt: "answer",
        "judge <question> <rubric_item> <llm_response>",
        lambda system, user, **kw: '{"score": 1.0}')
    assert out["embedder"] == {"serve": BF16}


def test_lme_v2_smoke_report_carries_the_rows_embedder(tmp_path,
                                                       monkeypatch):
    import lme_v2_smoke as smoke

    rows = []
    for qid in ("q1", "q2"):
        row = {"question_id": qid, "consolidation": {"superseded": 0},
               "embedder": {"extract": FP32}}
        for arm in smoke.ARMS:
            row[f"{arm}_correct"] = True
            row[f"{arm}_context_tokens"] = 5
        rows.append(row)
    out = tmp_path / "lme-v2-smoke.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    summary_file = tmp_path / "lme-v2-smoke.summary.json"
    monkeypatch.setattr(smoke, "OUT_FILE", out)
    monkeypatch.setattr(smoke, "SUMMARY_FILE", summary_file)
    smoke.report()
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    assert summary["embedder"] == {"extract": FP32}


def test_recall_fanout_combine_warns_when_the_arms_precisions_differ(
        tmp_path, monkeypatch, capsys):
    """The before/after arms can run on different checkouts and hosts."""
    import recall_fanout_bench as rfb

    monkeypatch.setattr(rfb, "combine", lambda b, a: {
        "n_questions": 0, "deltas": {}, "structural_identity": {},
        "targets_lost": [], "targets_gained": []})
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps({"embedder": FP32}), encoding="utf-8")
    after.write_text(json.dumps({"embedder": BF16}), encoding="utf-8")
    out = tmp_path / "combined.json"
    assert rfb.main(["--combine", str(before), str(after),
                     "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["embedder"] == {"before": FP32, "after": BF16}
    assert len(report["embedder_warnings"]) == 1
    assert "WARNING" in capsys.readouterr().err
    # an arm file written before stamping is unknown, not a mismatch
    before.write_text(json.dumps({}), encoding="utf-8")
    assert rfb.main(["--combine", str(before), str(after),
                     "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["embedder"]["before"] == "unknown"
    assert report["embedder_warnings"] == []
