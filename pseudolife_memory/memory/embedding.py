"""Embedding pipeline for converting text to dense vectors."""

from __future__ import annotations

import inspect
import logging
import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path, PurePosixPath


import torch
import numpy as np
from sentence_transformers import SentenceTransformer

from pseudolife_memory.onnx_artifacts import (
    has_nested_transformer_module,
    onnx_layout_available,
)
from pseudolife_memory.utils.config import EmbeddingConfig

logger = logging.getLogger(__name__)

# Overrides EmbeddingConfig.cpu_dtype when set to a non-empty value. An env
# var as well as a config key because the test suite must pin fp32 in the
# daemons it spawns, not only in-process, and because an operator can then
# roll a deployed daemon back to fp32 without a rebuild or an edit inside
# the state volume.
CPU_DTYPE_ENV = "PSEUDOLIFE_EMBEDDING_CPU_DTYPE"
_CPU_DTYPES = {"fp32": torch.float32, "bf16": torch.bfloat16}
_DTYPE_LABELS = {
    torch.float32: "fp32", torch.bfloat16: "bf16", torch.float16: "fp16",
}
_NATIVE_BF16_FLAGS = frozenset({"avx512_bf16", "amx_bf16"})


def _native_windows() -> bool:
    return sys.platform == "win32"


def cpu_has_native_bf16(cpuinfo_path: str = "/proc/cpuinfo") -> bool:
    """True when this CPU executes bf16 natively (x86 AVX512_BF16/AMX_BF16).

    torch's cpuinfo-backed probe answers on every OS that has it; Linux's
    ``/proc/cpuinfo`` flags stand in for a torch too old to carry it.
    Anything else answers no, keeping ``cpu_dtype="auto"`` on fp32. That
    includes aarch64, which advertises bf16 too, because CPU bf16 speed
    there has never been measured for this embedder.
    """
    probe = getattr(getattr(torch, "cpu", None), "_is_avx512_bf16_supported", None)
    if probe is not None:
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 — fall through to /proc/cpuinfo
            logger.debug("torch's bf16 CPU probe failed", exc_info=True)
    try:
        with open(cpuinfo_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if (line.startswith("flags")
                        and _NATIVE_BF16_FLAGS & set(line.partition(":")[2].split())):
                    return True
    except OSError:
        pass
    return False


def _requested_cpu_dtype(config: EmbeddingConfig) -> str:
    """The configured CPU precision, env override first; validated so a
    typo fails at construction on every device, not only on a CPU host."""
    env = os.environ.get(CPU_DTYPE_ENV, "").strip().lower()
    raw = env or str(getattr(config, "cpu_dtype", "auto")).strip().lower()
    if raw != "auto" and raw not in _CPU_DTYPES:
        source = CPU_DTYPE_ENV if env else "embedding.cpu_dtype"
        raise ValueError(
            f"embedding cpu_dtype must be 'auto', 'fp32' or 'bf16', got "
            f"{raw!r} (from {source})",
        )
    return raw


def _resolve_cpu_dtype(requested: str) -> torch.dtype:
    if requested == "auto":
        return torch.bfloat16 if cpu_has_native_bf16() else torch.float32
    if requested == "bf16" and not cpu_has_native_bf16():
        logger.warning(
            "embedding cpu_dtype=bf16 was requested on a CPU without native "
            "bf16 support: encodes will be slow ('auto' picks fp32 here).",
        )
    return _CPU_DTYPES[requested]


def _dtype_kwarg_name(version: str | None = None) -> str:
    """``from_pretrained``'s dtype keyword. Transformers 4.56 renamed
    ``torch_dtype`` to ``dtype`` (PR #39782); older releases know only
    ``torch_dtype``. An unparseable version is taken to be current."""
    if version is None:
        try:
            import transformers  # noqa: PLC0415

            version = transformers.__version__
        except Exception:  # noqa: BLE001
            return "dtype"
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return "dtype"
    return "dtype" if (major, minor) >= (4, 56) else "torch_dtype"


def _accepts_model_kwargs(factory) -> bool:
    """Whether this SentenceTransformer takes ``model_kwargs``; the
    declared sentence-transformers 2.2 floor predates it."""
    try:
        params = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.name == "model_kwargs" or p.kind is inspect.Parameter.VAR_KEYWORD
        for p in params
    )


def _param_dtypes(model) -> set[torch.dtype]:
    """Every dtype among a torch model's parameters; empty for ONNX or a
    stub. All of them, not the first: sentence-transformers 3.0.x applied a
    load dtype to the Transformer module only, leaving a Dense head fp32."""
    try:
        return {p.dtype for p in model.parameters()}
    except (AttributeError, TypeError):
        return set()


def _cached_hf_file(
    repo_id: str,
    filename: str,
    local_files_only: bool = True,
) -> str:
    """Return one configured file from a locally cached Hub snapshot."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415

    return hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_files_only=local_files_only,
    )


def _onnx_file_parts(file_name: str) -> tuple[str, ...]:
    """Validate a model-relative ONNX path and return its components."""
    normalized = file_name.replace("\\", "/")
    raw_parts = normalized.split("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(not part or part in {".", ".."} or ":" in part for part in raw_parts)
        or path.suffix != ".onnx"
    ):
        raise ValueError(
            "embedding.onnx_file_name must be a relative .onnx path "
            "inside the configured model",
        )
    return path.parts


def _resolve_onnx_source(model_name: str, file_name: str) -> str | None:
    """Resolve a verified existing ONNX artifact to its local model root.

    Both SentenceTransformers and Optimum infer that a missing artifact should
    be exported, on every platform. The preflight verifies the configured
    artifact at every recognized Transformer module's effective path before
    either library sees the request.

    Hub models are resolved through ``hf_hub_download(local_files_only=True)``
    even when networking is enabled. It probes the configured artifact or the
    minimal ``modules.json`` / ``config.json`` metadata needed to find effective
    module paths, then returns their revision-specific cached snapshot. It does
    not list a mutable remote head or download every ONNX variant.
    """
    parts = _onnx_file_parts(file_name)
    local_root = Path(model_name).expanduser()
    if local_root.is_dir():
        return str(local_root) if onnx_layout_available(local_root, file_name) else None

    # Mirror sentence-transformers' short-id resolution: bare names live
    # under the sentence-transformers/ org.
    candidates = (
        [model_name]
        if "/" in model_name
        else [f"sentence-transformers/{model_name}", model_name]
    )
    lookups = ("/".join(parts), "modules.json", "config.json")
    for repo_id in candidates:
        for lookup in lookups:
            try:
                artifact = Path(_cached_hf_file(
                    repo_id,
                    lookup,
                    local_files_only=True,
                ))
            except Exception:  # noqa: BLE001 — absent from this local cache key
                continue
            lookup_parts = PurePosixPath(lookup).parts
            if tuple(artifact.parts[-len(lookup_parts):]) != lookup_parts:
                continue
            snapshot_root = artifact
            for _ in lookup_parts:
                snapshot_root = snapshot_root.parent
            if onnx_layout_available(
                snapshot_root,
                file_name,
                allow_hub_blob_links=True,
            ):
                return str(snapshot_root)
    return None


def _configured_onnx_source(config: EmbeddingConfig) -> tuple[str, str | None]:
    """Normalize the configured ONNX artifact name and resolve its local root.

    Shared by the loader and the MCP default overlay's auto-select probe, so
    the two can never check different paths. Returns the normalized file name
    and the resolved root, or ``None`` when the artifact is absent. Raises
    ``ValueError`` for an ``onnx_file_name`` outside the model.
    """
    file_name = "/".join(_onnx_file_parts(
        getattr(config, "onnx_file_name", "onnx/model.onnx"),
    ))
    return file_name, _resolve_onnx_source(config.model_name, file_name)


def _native_windows_nested_layout(onnx_source: str) -> bool:
    """True when the loader must refuse ONNX for this resolved model root.

    The pinned Optimum stack matches a POSIX subfolder pattern against
    OS-native path strings, so on native Windows it never detects the
    artifact of a Transformer module loaded from a nested subfolder and
    re-enables export. Shared by the loader's fallback and the MCP default
    overlay's auto-select, so the two gates cannot drift.
    """
    return _native_windows() and has_nested_transformer_module(onnx_source)


class EmbeddingPipeline:
    """Encodes text into dense vector embeddings using sentence-transformers.

    Two perf levers (both config-driven, both fail-soft):

    * ``backend = "onnx"`` uses sentence-transformers' native ONNX backend.
      The historical MiniLM fp32 artifact measured ~3x faster single-text CPU
      encoding with cosine 1.00000 against torch; other configured artifacts
      carry no equivalence claim. The backend is load-only: its configured
      artifact must already exist in a local model or cached Hub snapshot. It
      falls back to torch when the artifact is missing, and when a nested
      module layout would be mis-detected on native Windows.
    * ``cache_size > 0`` keeps an LRU of ``(text, normalize)`` →
      embedding. The service embeds the same strings repeatedly within
      and across requests (query text for search + slot ops, dedup keys,
      warmup probes); repeats skip the model forward entirely.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        # Auto-fallback to CPU if CUDA requested but not available
        device = config.device
        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        self._device = device

        requested = getattr(config, "backend", "torch")
        if requested not in ("torch", "onnx"):
            raise ValueError(
                f"embedding backend must be 'torch' or 'onnx', got {requested!r}",
            )
        cpu_dtype = _requested_cpu_dtype(config)
        self.model = None
        self.backend = "torch"
        if requested == "onnx":
            try:
                file_name, onnx_source = _configured_onnx_source(config)
                if onnx_source is None:
                    raise FileNotFoundError(
                        f"configured ONNX artifact {file_name!r} is not "
                        "available in the local model or Hub cache",
                    )
                if _native_windows_nested_layout(onnx_source):
                    # Narrower than a platform gate: only a nested module
                    # subfolder goes undetected on native Windows and
                    # re-enables export. A flat layout resolves on both
                    # platforms and keeps the accelerator.
                    logger.warning(
                        "ONNX embedding backend is disabled on native Windows "
                        "for this model's nested module layout because the "
                        "pinned Optimum stack mis-detects a nested "
                        "subfolder's existing ONNX artifact and enables "
                        "export; falling back to torch before ONNX "
                        "construction.",
                    )
                else:
                    self.model = SentenceTransformer(
                        onnx_source,
                        device=device,
                        backend="onnx",
                        model_kwargs={
                            "file_name": file_name, "export": False,
                        },
                    )
                    self.backend = "onnx"
            except Exception as exc:  # noqa: BLE001 — optional accelerator
                logger.warning(
                    "ONNX embedding backend failed to load (%s) — falling "
                    "back to torch.",
                    exc,
                )
        if self.model is None:
            target = (_resolve_cpu_dtype(cpu_dtype)
                      if torch.device(device).type == "cpu" else None)
            if target is torch.bfloat16 and _accepts_model_kwargs(SentenceTransformer):
                # Load straight into bf16. Casting after an fp32 load keeps
                # the fp32 load peak: peak RSS while loading the Qwen default
                # measured 3,808 MB that way vs 537 MB direct (2026-09-23;
                # bf16 weights then page in on first use, ~1.4 GB steady).
                dtype_kwarg = _dtype_kwarg_name()
                self.model = SentenceTransformer(
                    config.model_name,
                    device=device,
                    model_kwargs={dtype_kwarg: torch.bfloat16},
                )
                loaded = _param_dtypes(self.model)
                if loaded and loaded != {torch.bfloat16}:
                    logger.warning(
                        "The model loader left parameters in %s despite "
                        "%s=bfloat16; casting them to bf16 after load (the "
                        "fp32 load peak is avoided only for what loaded in "
                        "bf16).", sorted(map(str, loaded)), dtype_kwarg,
                    )
                    self.model.to(torch.bfloat16)
            elif target is torch.bfloat16:
                logger.warning(
                    "This sentence-transformers predates model_kwargs: "
                    "loading fp32 and casting to bf16 (the fp32 load peak "
                    "is not avoided).",
                )
                self.model = SentenceTransformer(config.model_name, device=device)
                self.model.to(torch.bfloat16)
            else:
                self.model = SentenceTransformer(
                    config.model_name,
                    device=device,
                )
                if target is torch.float32:
                    # Preserve the FP32 CPU inference used by the validated
                    # 4.x stack. Transformers 5 defaults to the checkpoint
                    # dtype; the 2026-09-20 full-suite CI diagnostics and
                    # subsequent Qwen probe exposed slow BF16 on CPUs lacking
                    # native support. Module.float() also supports the
                    # sentence-transformers 2.x floor, unlike newer
                    # constructor model_kwargs. GPU and ONNX retain their
                    # backend's precision.
                    self.model.float()
        # The resident precision, read back rather than assumed. None for
        # ONNX, whose precision is the configured artifact's.
        resident = _param_dtypes(self.model)
        self.dtype: str | None = (
            _DTYPE_LABELS.get(next(iter(resident))) if len(resident) == 1
            else "mixed" if resident else None
        )
        # Cap the tokenizer's max sequence length. Applies to both backends:
        # SentenceTransformer.max_seq_length delegates to the underlying
        # Transformer module regardless of which runtime (torch/onnx) does
        # the forward pass, so one assignment covers both — there is no
        # separate ONNX knob to hack around. A cap only (min with whatever
        # the model shipped with), never a raise, so a model whose native
        # default is already shorter than the configured cap is untouched.
        existing_max_seq_len = getattr(self.model, "max_seq_length", None)
        self.model.max_seq_length = min(
            existing_max_seq_len or 512, config.max_seq_length,
        )
        self._dim = self.model.get_sentence_embedding_dimension()
        # Positive confirmation of the active backend: the ONNX path fails
        # soft, so without this line a broken accelerator in the deployed
        # container would silently revert to torch while /health stays
        # green. "verify live" = grep the daemon log for this.
        logger.info(
            "Embedding backend: %s (model=%s, dim=%d, device=%s, dtype=%s)",
            self.backend, config.model_name, self._dim, self._device,
            self.dtype or "n/a",
        )

        self._cache_size = max(0, int(getattr(config, "cache_size", 0) or 0))
        self._cache: OrderedDict[tuple[str, bool], torch.Tensor] = OrderedDict()
        self._cache_lock = threading.Lock()

    @property
    def embedding_dim(self) -> int:
        """Dimensionality of the output embeddings."""
        return self._dim

    def describe(self) -> dict[str, str | None]:
        """What is actually serving: backend, device, resident dtype."""
        return {"backend": self.backend, "device": self._device,
                "dtype": self.dtype}

    def encode(
        self,
        texts: str | list[str],
        normalize: bool = True,
    ) -> torch.Tensor:
        """Encode text(s) into embedding vectors.

        Args:
            texts: A single string or list of strings to encode.
            normalize: If True, L2-normalize the embeddings (recommended for
                       Hopfield retrieval where we use dot-product similarity).

        Returns:
            torch.Tensor of shape (N, embedding_dim) on the configured device.
        """
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            # torch.stack raises on an empty list; preserve the pre-cache
            # contract of returning an empty (0, dim) tensor.
            return torch.empty((0, self._dim))

        rows: list[torch.Tensor | None] = [None] * len(texts)
        misses: list[int] = []
        if self._cache_size:
            with self._cache_lock:
                for i, text in enumerate(texts):
                    key = (text, normalize)
                    cached = self._cache.get(key)
                    if cached is None:
                        misses.append(i)
                    else:
                        self._cache.move_to_end(key)
                        rows[i] = cached
        else:
            misses = list(range(len(texts)))

        if misses:
            # Tensors, not numpy: older sentence-transformers (the declared
            # 2.2 floor among them) convert with emb.numpy(), which cannot
            # represent a bf16 model's output.
            # Whatever the model computes in, rows leave as float32, so
            # stored vectors and every cosine stay fp32.
            embeddings = self.model.encode(
                [texts[i] for i in misses],
                batch_size=self.config.batch_size,
                normalize_embeddings=normalize,
                convert_to_tensor=True,
                show_progress_bar=False,
            )
            if isinstance(embeddings, torch.Tensor):
                fresh = embeddings.detach().to(device="cpu", dtype=torch.float32)
                # sentence-transformers 5 encodes under inference_mode, and
                # .to() is a no-op for fp32 CPU rows; cached rows stay
                # ordinary tensors, as the numpy path always produced.
                if fresh.is_inference():
                    fresh = fresh.clone()
            else:
                fresh = torch.from_numpy(np.array(embeddings)).float()
            for row, i in enumerate(misses):
                rows[i] = fresh[row]
            if self._cache_size:
                with self._cache_lock:
                    for i in misses:
                        key = (texts[i], normalize)
                        self._cache[key] = rows[i]
                        self._cache.move_to_end(key)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)

        # stack COPIES each row — callers can never mutate cached storage
        # through the returned tensor.
        tensor = torch.stack(rows)
        if self._device == "cuda":
            tensor = tensor.cuda()

        return tensor

    def encode_single(self, text: str, normalize: bool = True) -> torch.Tensor:
        """Encode a single text string. Returns shape (embedding_dim,)."""
        return self.encode(text, normalize=normalize).squeeze(0)

    def encode_query(self, text: str, normalize: bool = True) -> torch.Tensor:
        """Encode a retrieval QUERY (the asymmetric-model, query side).

        Prepends ``config.query_prefix`` before encoding and delegates to
        ``encode_single`` — so it flows through the exact same LRU cache as
        document encodes, keyed on the PREFIXED text. That makes the
        keyspace disjoint from document-side ``encode``/``encode_single``
        calls of identical raw text (no second cache to keep in sync).

        With ``query_prefix=""`` (a symmetric model, e.g. the current
        MiniLM default) this is byte-identical to ``encode_single(text)``.

        Returns shape (embedding_dim,).
        """
        return self.encode_single(self.config.query_prefix + text, normalize=normalize)
