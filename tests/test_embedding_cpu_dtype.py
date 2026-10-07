"""CPU embedder precision: bf16 where the CPU has native bf16, fp32 elsewhere.

2026-09-23: the production daemon was cgroup OOM-killed under an ordinary
request burst. The fp32 Qwen3-Embedding-0.6B embedder dominates its
footprint (~2.85 GB steady, 3.8 GB peak RSS while loading, in a throwaway
container from the production image). The checkpoint itself is bfloat16;
loading it directly in bf16 measured ~1.4 GB steady and 537 MB peak RSS
while loading (the weights page in on first use), with retrieval parity
on real bank data (400 entries, 60 queries: bf16 queries against stored
fp32 vectors keep top-8 overlap 0.994 and rank-0 60/60 through this
pipeline; evals/results/embedder-cpu-bf16-probe-20260923.json).
bf16 on a CPU WITHOUT native support is slow, though — the 2026-09-20 CI
diagnostics behind the fp32 cast — so the switch is capability-gated.

Contract pinned here:

* ``embedding.cpu_dtype`` is ``auto`` / ``fp32`` / ``bf16``; anything else
  is a config error, whatever the device;
* ``auto`` (the shipped default) = bf16 only when the CPU reports native
  bf16, fp32 otherwise;
* ``PSEUDOLIFE_EMBEDDING_CPU_DTYPE`` overrides the config value, and the
  suite pins it to fp32 in conftest — daemons the suite spawns inherit it;
* bf16 is LOADED in bf16, never cast after an fp32 load (the cast keeps the
  3.8 GB fp32 load peak) — except under a sentence-transformers too old to
  accept ``model_kwargs``, where load-then-cast is the logged fallback;
* embeddings leave the pipeline as float32 whatever the model computes in;
* GPU and ONNX backends are untouched.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from pseudolife_memory.memory import embedding
from pseudolife_memory.utils.config import EmbeddingConfig

ENV = "PSEUDOLIFE_EMBEDDING_CPU_DTYPE"
_LABEL = {torch.float32: "fp32", torch.bfloat16: "bf16"}


class _Model(torch.nn.Module):
    """A stand-in model: one parameter in the dtype it was 'loaded' in.

    Records every cast so a test can tell a direct bf16 load from an fp32
    load followed by a cast. ``encode`` mimics sentence-transformers 2.2,
    whose numpy conversion (``emb.numpy()``) cannot represent bfloat16.
    """

    def __init__(self, dtype: torch.dtype = torch.float32) -> None:
        super().__init__()
        self.weight = torch.nn.Parameter(torch.ones(4, dtype=dtype))
        self.casts: list[str] = []

    def to(self, *args, **kwargs):
        self.casts.append("to")
        return super().to(*args, **kwargs)

    def float(self):
        self.casts.append("float")
        return super().float()

    def get_sentence_embedding_dimension(self) -> int:
        return 4

    def encode(self, texts, convert_to_tensor=False, **kwargs):
        rows = torch.stack([
            torch.full((4,), float(len(t)), dtype=self.weight.dtype)
            for t in texts
        ])
        if convert_to_tensor:
            return rows
        return np.asarray([row.numpy() for row in rows])


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch SentenceTransformer with a recorder that honours a dtype
    passed through ``model_kwargs``, the way Transformers does."""
    calls: list[dict] = []

    def _load(model_name, device=None, **kwargs):
        model_kwargs = kwargs.get("model_kwargs") or {}
        dtype = (model_kwargs.get("dtype") or model_kwargs.get("torch_dtype")
                 or torch.float32)
        model = _Model(dtype)
        calls.append({"model_name": model_name, "device": device,
                      "kwargs": kwargs, "model": model})
        return model

    monkeypatch.setattr(embedding, "SentenceTransformer", _load)
    monkeypatch.delenv(ENV, raising=False)
    return calls


def _native(monkeypatch: pytest.MonkeyPatch, native: bool) -> None:
    monkeypatch.setattr(embedding, "cpu_has_native_bf16", lambda: native)


def _param_dtype(pipe) -> torch.dtype:
    return next(pipe.model.parameters()).dtype


# ── the knob ───────────────────────────────────────────────────────────────


def test_shipped_default_is_auto() -> None:
    assert EmbeddingConfig().cpu_dtype == "auto"


def test_the_daemon_defaults_layer_leaves_cpu_dtype_alone() -> None:
    """``_apply_mcp_defaults`` rewrites several embedding defaults for the
    daemon (batch size, backend). The precision default is the dataclass's
    own: no hidden daemon override, and an operator's value survives."""
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.utils.config import AppConfig

    cfg = AppConfig()
    MemoryService._apply_mcp_defaults(cfg)
    assert cfg.embedding.cpu_dtype == "auto"

    cfg = AppConfig()
    cfg.embedding.cpu_dtype = "fp32"
    MemoryService._apply_mcp_defaults(
        cfg, user_keys=frozenset({"embedding.cpu_dtype"}))
    assert cfg.embedding.cpu_dtype == "fp32"


def test_cpu_dtype_round_trips_through_config_yaml(tmp_path) -> None:
    from pseudolife_memory.utils.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("embedding:\n  cpu_dtype: fp32\n", encoding="utf-8")
    assert load_config(path).embedding.cpu_dtype == "fp32"


def test_a_console_write_keeps_an_operator_cpu_dtype(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The precision is deliberately not a Console knob (the Console never
    edits embedder construction). A Console write merges into config.yaml,
    so it must leave an operator's hand-set precision in place."""
    from pseudolife_memory.utils.config import AppConfig, load_config
    from pseudolife_memory.web.config_io import write_config

    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("embedding:\n  cpu_dtype: fp32\n", encoding="utf-8")
    write_config(SimpleNamespace(config=AppConfig(), data_dir=tmp_path),
                 {"memory.top_k": 9})
    reloaded = load_config(path)
    assert reloaded.embedding.cpu_dtype == "fp32"
    assert reloaded.memory.top_k == 9


# ── the capability gate ────────────────────────────────────────────────────


@pytest.mark.parametrize("native", [True, False])
def test_auto_uses_bf16_only_on_a_cpu_with_native_bf16(
    loads, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    _native(monkeypatch, native)
    pipe = embedding.EmbeddingPipeline(
        EmbeddingConfig(device="cpu", cpu_dtype="auto"))
    expected = torch.bfloat16 if native else torch.float32
    assert _param_dtype(pipe) == expected
    assert pipe.dtype == _LABEL[expected]


@pytest.mark.parametrize("requested,native,expected", [
    ("fp32", True, torch.float32),
    ("fp32", False, torch.float32),
    ("bf16", True, torch.bfloat16),
    ("bf16", False, torch.bfloat16),
])
def test_an_explicit_precision_overrides_detection(
    loads, monkeypatch: pytest.MonkeyPatch,
    requested: str, native: bool, expected: torch.dtype,
) -> None:
    _native(monkeypatch, native)
    pipe = embedding.EmbeddingPipeline(
        EmbeddingConfig(device="cpu", cpu_dtype=requested))
    assert _param_dtype(pipe) == expected


def test_forcing_bf16_on_a_cpu_without_native_support_warns(
    loads, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _native(monkeypatch, False)
    with caplog.at_level(logging.WARNING, logger=embedding.logger.name):
        embedding.EmbeddingPipeline(
            EmbeddingConfig(device="cpu", cpu_dtype="bf16"))
    assert any("native bf16" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("env,configured,expected", [
    ("fp32", "bf16", torch.float32),
    ("bf16", "fp32", torch.bfloat16),
    (" BF16 ", "fp32", torch.bfloat16),
    ("", "bf16", torch.bfloat16),
])
def test_the_env_override_beats_the_config_value(
    loads, monkeypatch: pytest.MonkeyPatch,
    env: str, configured: str, expected: torch.dtype,
) -> None:
    _native(monkeypatch, True)
    monkeypatch.setenv(ENV, env)
    pipe = embedding.EmbeddingPipeline(
        EmbeddingConfig(device="cpu", cpu_dtype=configured))
    assert _param_dtype(pipe) == expected


@pytest.mark.parametrize("device,configured,env", [
    ("cpu", "fp16", None),
    ("cpu", "float32", None),
    ("cpu", "auto", "half"),
    ("cuda", "bfloat16", None),
])
def test_an_unknown_precision_is_a_config_error(
    loads, monkeypatch: pytest.MonkeyPatch,
    device: str, configured: str, env: str | None,
) -> None:
    """Refused loudly on every device: a typo must not wait for the day the
    daemon lands on a CPU host to surface."""
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    if env is not None:
        monkeypatch.setenv(ENV, env)
    with pytest.raises(ValueError, match="cpu_dtype"):
        embedding.EmbeddingPipeline(
            EmbeddingConfig(device=device, cpu_dtype=configured))


def test_the_suite_runs_the_embedder_in_fp32() -> None:
    """conftest pins fp32 for every pipeline the suite builds, and every
    daemon it spawns inherits the variable — so a runner that reports
    native bf16 cannot flip the suite's numerics. GitHub's runners report
    none today; this keeps it that way on any host."""
    assert os.environ.get(ENV) == "fp32"


# ── how bf16 is loaded ─────────────────────────────────────────────────────


def test_bf16_is_loaded_directly_not_cast_after_an_fp32_load(
    loads, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _native(monkeypatch, True)
    embedding.EmbeddingPipeline(EmbeddingConfig(device="cpu", cpu_dtype="auto"))
    (call,) = loads
    assert call["kwargs"]["model_kwargs"] == {
        embedding._dtype_kwarg_name(): torch.bfloat16}
    assert call["model"].casts == [], (
        "a cast after load keeps the fp32 load peak (3.8 GB measured) the "
        "direct bf16 load exists to avoid")


def test_fp32_keeps_the_plain_load_and_its_cast(
    loads, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fp32 path is the pre-existing one: no model_kwargs (the
    sentence-transformers floor predates them), then ``.float()`` for
    Transformers 5, which defaults to the checkpoint's bf16."""
    _native(monkeypatch, False)
    embedding.EmbeddingPipeline(EmbeddingConfig(device="cpu", cpu_dtype="auto"))
    (call,) = loads
    assert "model_kwargs" not in call["kwargs"]
    assert call["model"].casts == ["float"]


@pytest.mark.parametrize("version,name", [
    ("4.40.0", "torch_dtype"),
    ("4.55.4", "torch_dtype"),
    ("4.56.0", "dtype"),
    ("4.57.6", "dtype"),
    ("5.0.0", "dtype"),
    ("5.2.0.dev0", "dtype"),
    ("not-a-version", "dtype"),
])
def test_the_dtype_kwarg_follows_the_transformers_rename(
    version: str, name: str,
) -> None:
    """Transformers 4.56 renamed ``torch_dtype`` to ``dtype`` (PR #39782);
    older releases only know ``torch_dtype``."""
    assert embedding._dtype_kwarg_name(version) == name


def test_an_old_sentence_transformers_falls_back_to_load_then_cast(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    class _OldST(_Model):
        # sentence-transformers 2.2's constructor: no model_kwargs, no **kw.
        def __init__(self, model_name_or_path=None, modules=None,
                     device=None, cache_folder=None):
            super().__init__(torch.float32)

    monkeypatch.setattr(embedding, "SentenceTransformer", _OldST)
    monkeypatch.delenv(ENV, raising=False)
    _native(monkeypatch, True)
    with caplog.at_level(logging.WARNING, logger=embedding.logger.name):
        pipe = embedding.EmbeddingPipeline(
            EmbeddingConfig(device="cpu", cpu_dtype="auto"))
    assert _param_dtype(pipe) == torch.bfloat16
    assert pipe.dtype == "bf16"
    assert any("model_kwargs" in r.getMessage() for r in caplog.records)
    # And it still serves: sentence-transformers 2.2 cannot hand back bf16
    # as numpy, so the pipeline must not ask it to.
    assert pipe.encode(["x"]).dtype == torch.float32


def test_a_loader_that_ignores_the_dtype_kwarg_is_caught_and_cast(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A Transformers that silently drops the kwarg would leave the model in
    fp32 while the log claimed bf16. The resident dtype is verified."""
    monkeypatch.setattr(
        embedding, "SentenceTransformer",
        lambda model_name, device=None, **kwargs: _Model(torch.float32))
    monkeypatch.delenv(ENV, raising=False)
    _native(monkeypatch, True)
    with caplog.at_level(logging.WARNING, logger=embedding.logger.name):
        pipe = embedding.EmbeddingPipeline(
            EmbeddingConfig(device="cpu", cpu_dtype="auto"))
    assert _param_dtype(pipe) == torch.bfloat16
    assert any("cast" in r.getMessage() for r in caplog.records), (
        "a silently ignored dtype kwarg must be logged")


def test_a_partly_bf16_model_is_finished_by_a_cast(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """sentence-transformers 3.0.x passed the dtype to the Transformer module
    only; a downstream Dense layer stayed fp32 and every encode then failed
    with a dtype mismatch. The first parameter alone reads bf16 there, so
    the check has to cover all of them."""

    class _Mixed(_Model):
        def __init__(self):
            super().__init__(torch.bfloat16)
            self.dense = torch.nn.Parameter(torch.ones(4, dtype=torch.float32))

    monkeypatch.setattr(embedding, "SentenceTransformer",
                        lambda model_name, device=None, **kwargs: _Mixed())
    monkeypatch.delenv(ENV, raising=False)
    _native(monkeypatch, True)
    with caplog.at_level(logging.WARNING, logger=embedding.logger.name):
        pipe = embedding.EmbeddingPipeline(
            EmbeddingConfig(device="cpu", cpu_dtype="auto"))
    assert {p.dtype for p in pipe.model.parameters()} == {torch.bfloat16}
    assert pipe.dtype == "bf16"
    assert any("cast" in r.getMessage() for r in caplog.records)


# ── what leaves the pipeline ───────────────────────────────────────────────


def test_cached_rows_are_ordinary_tensors(
    loads, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sentence-transformers 5 encodes under torch.inference_mode(); rows
    kept in the LRU must not be inference tensors (an in-place op on one
    raises outside inference mode). The numpy path this replaced never
    produced them."""
    _native(monkeypatch, True)

    class _InferenceModel(_Model):
        def encode(self, texts, convert_to_tensor=False, **kwargs):
            with torch.inference_mode():
                return torch.ones(len(texts), 4, dtype=torch.float32)

    monkeypatch.setattr(embedding, "SentenceTransformer",
                        lambda model_name, device=None, **kwargs: _InferenceModel())
    pipe = embedding.EmbeddingPipeline(
        EmbeddingConfig(device="cpu", cpu_dtype="fp32", cache_size=8))
    pipe.encode(["alpha"])
    (cached,) = pipe._cache.values()
    assert not cached.is_inference()


@pytest.mark.parametrize("cache_size", [0, 16])
def test_bf16_embeddings_leave_the_pipeline_as_float32(
    loads, monkeypatch: pytest.MonkeyPatch, cache_size: int,
) -> None:
    """Stored vectors and every cosine stay fp32."""
    _native(monkeypatch, True)
    pipe = embedding.EmbeddingPipeline(EmbeddingConfig(
        device="cpu", cpu_dtype="auto", cache_size=cache_size))
    assert pipe.dtype == "bf16"
    batch = pipe.encode(["alpha", "be"])
    assert batch.dtype == torch.float32 and batch.shape == (2, 4)
    assert pipe.encode_single("alpha").dtype == torch.float32
    assert pipe.encode_query("q").dtype == torch.float32


def test_the_backend_log_line_and_describe_name_the_resolved_dtype(
    loads, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """"Verify live" greps this line and reads /health's copy of describe()."""
    _native(monkeypatch, True)
    with caplog.at_level(logging.INFO, logger=embedding.logger.name):
        pipe = embedding.EmbeddingPipeline(EmbeddingConfig(device="cpu"))
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("Embedding backend:"))
    assert "dtype=bf16" in line
    assert pipe.describe() == {
        "backend": "torch", "device": "cpu", "dtype": "bf16"}


# ── untouched backends ─────────────────────────────────────────────────────


def test_a_gpu_embedder_ignores_the_cpu_precision(
    loads, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    _native(monkeypatch, True)
    embedding.EmbeddingPipeline(EmbeddingConfig(device="cuda", cpu_dtype="bf16"))
    (call,) = loads
    assert "model_kwargs" not in call["kwargs"]
    assert call["model"].casts == []


def test_the_onnx_backend_ignores_the_cpu_precision(
    loads, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _native(monkeypatch, True)
    monkeypatch.setattr(embedding, "_native_windows", lambda: False)
    monkeypatch.setattr(embedding, "_resolve_onnx_source",
                        lambda *args: "/verified")
    pipe = embedding.EmbeddingPipeline(
        EmbeddingConfig(device="cpu", backend="onnx", cpu_dtype="bf16"))
    assert pipe.backend == "onnx"
    (call,) = loads
    assert call["kwargs"]["model_kwargs"] == {
        "file_name": "onnx/model.onnx", "export": False}
    assert call["model"].casts == []


# ── native-bf16 detection ──────────────────────────────────────────────────


@pytest.mark.parametrize("verdict", [True, False])
def test_detection_trusts_torchs_cpu_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path, verdict: bool,
) -> None:
    info = tmp_path / "cpuinfo"
    info.write_text("flags\t: avx512_bf16\n" if not verdict else "flags\t: fpu\n",
                    encoding="utf-8")
    monkeypatch.setattr(torch.cpu, "_is_avx512_bf16_supported",
                        lambda: verdict, raising=False)
    assert embedding.cpu_has_native_bf16(cpuinfo_path=str(info)) is verdict


@pytest.mark.parametrize("line,expected", [
    ("flags\t\t: fpu sse2 avx2 avx512f avx512_bf16 vaes", True),
    ("flags\t\t: fpu avx512f amx_bf16 amx_tile", True),
    ("flags\t\t: fpu sse2 avx2 avx512f avx512bw avx512_vnni", False),
    # aarch64 advertises bf16 too, but CPU bf16 speed there is unmeasured:
    # auto stays fp32 off x86 (set bf16 explicitly to opt in).
    ("Features\t: fp asimd sve bf16 i8mm", False),
])
def test_detection_reads_cpuinfo_flags_without_the_torch_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path, line: str, expected: bool,
) -> None:
    monkeypatch.delattr(torch.cpu, "_is_avx512_bf16_supported", raising=False)
    info = tmp_path / "cpuinfo"
    info.write_text(f"processor\t: 0\n{line}\n\n", encoding="utf-8")
    assert embedding.cpu_has_native_bf16(cpuinfo_path=str(info)) is expected


def test_detection_falls_through_a_failing_probe_to_cpuinfo(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    def _broken():
        raise RuntimeError("cpuinfo init failed")

    monkeypatch.setattr(torch.cpu, "_is_avx512_bf16_supported", _broken,
                        raising=False)
    info = tmp_path / "cpuinfo"
    info.write_text("flags\t: avx512_bf16\n", encoding="utf-8")
    assert embedding.cpu_has_native_bf16(cpuinfo_path=str(info)) is True


def test_detection_says_no_when_nothing_can_tell(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    monkeypatch.delattr(torch.cpu, "_is_avx512_bf16_supported", raising=False)
    assert embedding.cpu_has_native_bf16(
        cpuinfo_path=str(tmp_path / "absent")) is False


# ── the real stack ─────────────────────────────────────────────────────────


@pytest.mark.real_model
def test_the_installed_stack_loads_bf16_directly_and_serves_float32(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Pins the kwarg name against the INSTALLED Transformers: a wrong name
    would load fp32 and take the logged cast path. MiniLM keeps it cheap."""
    monkeypatch.delenv(ENV, raising=False)
    common = dict(device="cpu", model_name="all-MiniLM-L6-v2", cache_size=0)
    with caplog.at_level(logging.WARNING, logger=embedding.logger.name):
        bf16 = embedding.EmbeddingPipeline(
            EmbeddingConfig(cpu_dtype="bf16", **common))
    fp32 = embedding.EmbeddingPipeline(EmbeddingConfig(cpu_dtype="fp32", **common))
    assert _param_dtype(bf16) == torch.bfloat16
    assert _param_dtype(fp32) == torch.float32
    assert not [r for r in caplog.records if "cast" in r.getMessage()]
    texts = ["the daemon was OOM-killed under load",
             "bf16 halves the embedder's resident weights"]
    a, b = bf16.encode(texts), fp32.encode(texts)
    assert a.dtype == torch.float32
    assert float((a * b).sum(1).min()) > 0.99
