"""The :class:`MIRASBand` — one band in the continuum, configurable along all
four MIRAS axes.

A :class:`MIRASBand` combines a :class:`MemoryModule` (parametric memory body),
an :class:`UpdateRule` (online optimisation step), a :class:`RetentionObjective`
(loss function), and a :class:`RetentionPolicy` (eviction + decay) into the same
"single bank with text entries plus a neural memory" abstraction that
:class:`src.memory.titans_memory.TitansMemoryBank` provided in v0.4.x.

Public surface
--------------
For drop-in compatibility with the previous ``TitansMemoryBank``:

* ``entries: list[MemoryEntry]`` — text + embeddings + metadata.
* ``size: int`` — len of ``entries``.
* ``name: str`` — band identifier (``instant`` / ``short_term`` / …).
* ``surprise_ema: float`` — EMA of past surprise scores.
* ``compute_surprise(embedding) -> float`` — predict-and-measure.
* ``update_memory(embedding) -> float`` — one online update step.
* ``store(text, embedding, source, surprise) -> None`` — append + update.
* ``retrieve(query_embedding, top_k) -> RetrievalResult`` — neural + exact blend.
* ``get_state_dict() / load_state_dict()`` — pickle-able state.

Construction is via :func:`build_band` which reads a
:class:`src.utils.config.MIRASBandSpec` — see the module docstring for the
mapping from spec strings to concrete component classes.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.memory.miras.protocols import (
    MemoryModule,
    UpdateRule,
    RetentionObjective,
    RetentionPolicy,
)
from pseudolife_memory.memory.miras.modules import build_module
from pseudolife_memory.memory.miras.update_rules import build_update_rule
from pseudolife_memory.memory.miras.objectives import build_objective
from pseudolife_memory.memory.miras.retention import build_policy, now_seconds

if TYPE_CHECKING:
    from pseudolife_memory.utils.config import MIRASBandSpec


class MIRASBand:
    """One configurable band in the Continuum Memory System.

    Roughly: ``MemoryModule + UpdateRule + RetentionObjective + RetentionPolicy``
    plus a text-entry list for retrieval and bookkeeping. All four axes are
    swappable per band, so a CMS can mix-and-match (e.g. ``yaad`` runs a
    Linear+SGD instant band ahead of MLP+Adam slow bands).
    """

    def __init__(
        self,
        name: str,
        embedding_dim: int,
        memory_module: MemoryModule,
        update_rule: UpdateRule,
        objective: RetentionObjective,
        retention: RetentionPolicy,
        max_entries: int,
        update_interval: int,
        promotion_access_count: int,
        promotion_surprise: float,
        device: str = "cuda",
    ):
        self.name = name
        self.embedding_dim = embedding_dim
        self.max_entries = max_entries
        self.update_interval = update_interval
        self.promotion_access_count = promotion_access_count
        self.promotion_surprise = promotion_surprise
        self.device = device if torch.cuda.is_available() else "cpu"

        self.memory: MemoryModule = memory_module.to(self.device)
        self.update_rule = update_rule
        self.objective = objective
        self.retention = retention

        # v0.7+ optional torch.compile of the memory MLP forward pass.
        # Each band has ~750K-1.5M params at hidden 384-1024; on a CPU
        # like the 5800X3D the inductor backend gives roughly 2-3x speed-up
        # on the inner forward+backward (mostly from kernel fusion across
        # the GELU+Linear stack).  Lazy — compiled on first ``update_memory``
        # call so band construction stays cheap.  Falls back silently to
        # eager when compile is unavailable (older torch, missing C++
        # compiler on a slim runtime, model shape too dynamic, etc.).
        self._compiled_forward: object | None = None
        self._compile_attempted: bool = False

        # Surprise-EMA bookkeeping — used by introspection and consolidation.
        # Decay rate matches the v0.4.x default (0.95) so behaviour is unchanged
        # under the titans preset.
        self.surprise_ema: float = 0.0
        self.surprise_ema_decay: float = 0.95

        # Explicit entry store with lazy pattern-matrix cache for retrieval.
        # Touching ``entries`` directly is supported (the CMS does it during
        # consolidation) — flip ``_dirty`` whenever the list changes.
        self.entries: list[MemoryEntry] = []
        self._pattern_matrix: torch.Tensor | None = None
        self._dirty: bool = True

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self.entries)

    @property
    def base_lr(self) -> float:
        """Convenience alias for the update rule's base LR.

        The legacy ``TitansMemoryBank.base_lr`` was read externally for
        diagnostics; preserve the attribute so existing callers keep working.
        """
        return getattr(self.update_rule, "base_lr", 0.0)

    @property
    def weight_decay(self) -> float:
        """Convenience alias for the retention policy's weight decay."""
        return self.retention.weight_decay

    # ------------------------------------------------------------------
    # Online learning
    # ------------------------------------------------------------------

    def compute_surprise(self, embedding: torch.Tensor) -> float:
        """How poorly the memory predicts ``embedding``.

        Returns ``1.0`` when the bank is empty (everything is surprising
        before anything is learned) — matches the v0.4.x convention so
        the surprise gate's behaviour at cold-start is unchanged.
        """
        if not self.entries:
            return 1.0

        self.memory.eval()
        x = embedding.to(self.device)
        # Normalise both sides — matches v0.4.x ``TitansMemoryBank.compute_surprise``.
        x = F.normalize(x.unsqueeze(0), p=2, dim=1).squeeze(0)
        with torch.no_grad():
            predicted = self.memory(x)
        return self.objective.surprise_scalar(predicted, x)

    def _ensure_compiled_forward(self) -> None:
        """Lazily compile the memory MLP forward pass via ``torch.compile``.

        Idempotent.  Sets ``self._compiled_forward`` to the compiled
        callable on success, ``None`` on failure (which we then never
        retry).  Compile-time cost is paid on the first call into the
        compiled artefact — usually 1-3 s with the default inductor
        backend — then every subsequent ``update_memory`` /
        ``compute_surprise`` call uses the cached graph.

        Failure modes auto-fall-back to eager: missing Triton (CPU torch
        on Windows / our embedded runtime ships without it), unsupported
        op patterns, dynamic-shape recompiles, etc.  We trial-run the
        compiled callable once here to surface any compile-time errors
        synchronously so the first ``update_memory`` call doesn't crash
        — much friendlier than a delayed runtime failure deep inside
        a chat-flow.
        """
        if self._compile_attempted:
            return
        self._compile_attempted = True
        # Disable compile for very small MLPs — the dispatch overhead beats
        # the kernel-fusion win at hidden < 256.  At our band shapes
        # (hidden 256-1024) inductor wins clearly.
        if getattr(self.memory, "hidden_dim", 0) < 256:
            return
        # Globally suppress dynamo errors so a compile failure mid-call
        # doesn't crash the request — instead dynamo falls back to eager
        # transparently. Setting this at module import would be too eager;
        # setting it here means it's only flipped if a band actually tries
        # to compile.
        try:
            import torch._dynamo  # noqa: PLC0415
            torch._dynamo.config.suppress_errors = True
        except Exception:  # noqa: BLE001
            pass
        try:
            compiled = torch.compile(
                self.memory.forward,
                mode="reduce-overhead",
                dynamic=False,
            )
            # Synchronous warm-up — invokes the compile pipeline so we
            # learn now whether the backend is functional.  Use a fresh
            # unit-norm tensor to match the real call shape.
            with torch.no_grad():
                test_x = F.normalize(
                    torch.zeros(self.embedding_dim, device=self.device)
                    .add_(1e-6).unsqueeze(0),
                    p=2, dim=1,
                ).squeeze(0)
                _ = compiled(test_x)
            self._compiled_forward = compiled
        except Exception as exc:  # noqa: BLE001
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).info(
                "torch.compile unavailable for band %r (%s: %s) — using eager.",
                self.name, exc.__class__.__name__, str(exc)[:120],
            )
            self._compiled_forward = None

    def update_memory(self, embedding: torch.Tensor) -> float:
        """One online optimisation step.

        Returns the loss value before stepping (used as the per-entry
        surprise score in :meth:`store`). The actual update is driven
        by ``self.update_rule`` with the η modulation produced by
        ``self.memory.compute_eta``.
        """
        self.memory.train()
        self._ensure_compiled_forward()
        x = embedding.to(self.device)
        x = F.normalize(x.unsqueeze(0), p=2, dim=1).squeeze(0)

        # Forward pass: predict value from key (self-association by default).
        # Use compiled forward when available — eager fallback otherwise.
        if self._compiled_forward is not None:
            predicted = self._compiled_forward(x)
        else:
            predicted = self.memory(x)
        loss = self.objective.loss(predicted, x)

        # Backward pass driven by the update rule (which also handles
        # gradient clipping and the η-scaled LR).
        self.update_rule.zero_grad()
        loss.backward()
        eta = self.memory.compute_eta(x)
        self.update_rule.step(self.memory, loss, eta=eta)

        surprise_value = float(loss.item())

        # Update surprise EMA with the theta gate — same form as v0.4.x.
        theta = self.memory.compute_theta(x)
        self.surprise_ema = (
            self.surprise_ema_decay * self.surprise_ema + theta * surprise_value
        )
        return surprise_value

    def contrastive_update(
        self,
        embedding: torch.Tensor,
        scale: float = 0.1,
    ) -> float:
        """One *negated* online optimisation step (Slice F, v0.7.6).

        Pushes the band's memory module *away* from mapping ``embedding``
        to itself. Used by :mod:`src.memory.contrastive` when the user
        signals that a retrieved memory was wrong — the wrong memory's
        embedding becomes a contrastive target so the next retrieval
        ranks similar patterns lower.

        Math: identical to :meth:`update_memory` except the loss is
        **negated** and **scaled down** to a fraction of the normal LR-
        equivalent. A small ``scale`` (default 0.1) is critical for
        stability — a full-LR negative step is identical to an
        anti-gradient ascent move which can blow up the band's MLP.

        ``scale`` is clamped to ``[0.0, 0.5]`` defensively.

        Returns the loss value before stepping (the magnitude of the
        pre-contrastive surprise, useful for logging).
        """
        scale = max(0.0, min(0.5, float(scale)))
        if scale == 0.0:
            return 0.0

        self.memory.train()
        self._ensure_compiled_forward()
        x = embedding.to(self.device)
        x = F.normalize(x.unsqueeze(0), p=2, dim=1).squeeze(0)

        if self._compiled_forward is not None:
            predicted = self._compiled_forward(x)
        else:
            predicted = self.memory(x)
        # Negate and scale the loss. The update rule then takes a
        # gradient *descent* step on this negated quantity, which is
        # the *ascent* step we want — pushing predicted *away* from x.
        loss = -self.objective.loss(predicted, x) * scale

        self.update_rule.zero_grad()
        loss.backward()
        # No η modulation here — the band's η/θ gates assume positive
        # surprise; applying them to a negative loss can either suppress
        # the contrastive (if surprise is low) or amplify it
        # uncontrollably (if surprise is high). Use a flat η=1.0 instead.
        self.update_rule.step(self.memory, loss, eta=1.0)
        return float(loss.item())

    def store(
        self,
        text: str,
        embedding: torch.Tensor,
        source: str = "",
        surprise: float = 0.0,
    ) -> None:
        """Append an entry and run one update step."""
        entry = MemoryEntry(
            text=text,
            embedding=embedding.detach().to(self.device),
            surprise_score=surprise,
            source=source,
            bank=self.name,
        )

        if len(self.entries) >= self.max_entries:
            self._evict_one()

        self.entries.append(entry)
        self._dirty = True
        self.update_memory(embedding)

    def _evict_one(self) -> None:
        """Drop the entry with the lowest source-weighted retention score.

        ``source_weighted_score`` composes the base eviction score
        (recency / surprise / balanced) with the per-source multiplier
        from :attr:`RetentionPolicy.source_weights`. Net effect: under
        capacity pressure, ``llm_thinking`` entries evict before
        ``user_msg`` entries even when their base scores are similar —
        the right behaviour for agentic deployments.
        """
        if not self.entries:
            return
        now = now_seconds()
        scores = [self.retention.source_weighted_score(e, now) for e in self.entries]
        worst = min(range(len(scores)), key=lambda i: scores[i])
        self.entries.pop(worst)
        self._dirty = True

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve(
        self, query_embedding: torch.Tensor, top_k: int = 5
    ) -> RetrievalResult:
        """Top-k entries by blended (neural + exact) similarity.

        Same blend as v0.4.x: ``0.6 * neural + 0.4 * exact``, where
        "neural" is the cosine similarity between stored embeddings and
        the *predicted* output of the memory module, and "exact" is the
        cosine similarity between stored embeddings and the raw query.
        """
        if not self.entries:
            return RetrievalResult(entries=[], scores=[], surprises=[])

        if self._dirty:
            self._rebuild_pattern_matrix()
        assert self._pattern_matrix is not None  # implied by len(entries) > 0

        query = query_embedding.to(self.device)
        query = F.normalize(query.unsqueeze(0), p=2, dim=1).squeeze(0)

        # Neural retrieval — what the memory predicts for this query.
        self.memory.eval()
        with torch.no_grad():
            predicted = self.memory(query)
            predicted = F.normalize(predicted.unsqueeze(0), p=2, dim=1).squeeze(0)

        neural_scores = self._pattern_matrix @ predicted
        exact_scores = self._pattern_matrix @ query
        scores = 0.6 * neural_scores + 0.4 * exact_scores

        k = min(top_k, len(self.entries))
        top_scores, top_indices = torch.topk(scores, k)

        result_entries: list[MemoryEntry] = []
        result_surprises: list[float] = []
        for idx in top_indices.tolist():
            entry = self.entries[idx]
            entry.access_count += 1
            result_entries.append(entry)
            result_surprises.append(entry.surprise_score)

        return RetrievalResult(
            entries=result_entries,
            scores=top_scores.detach().cpu().tolist(),
            surprises=result_surprises,
        )

    def _rebuild_pattern_matrix(self) -> None:
        if not self.entries:
            self._pattern_matrix = None
            self._dirty = False
            return
        embeddings = [e.embedding.to(self.device) for e in self.entries]
        self._pattern_matrix = torch.stack(embeddings)
        self._pattern_matrix = F.normalize(self._pattern_matrix, p=2, dim=1)
        self._dirty = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def get_state_dict(self) -> dict:
        """Serialise this band's state.

        Layout intentionally extends v0.4.x's ``TitansMemoryBank.get_state_dict``
        — same keys preserved so the v1→v2 loader can map cleanly — plus a
        new ``axes`` block recording which MIRAS components produced the
        state. The optimiser-state migration in :class:`ContinuumMemorySystem.load`
        consults ``axes`` to decide whether to keep or discard the optimiser
        state across rule-type changes.
        """
        return {
            "memory_state": self.memory.state_dict(),
            "optimizer_state": self.update_rule.state_dict(),
            "surprise_ema": self.surprise_ema,
            "axes": {
                "update_rule": self.update_rule.name,
                "objective": self.objective.name,
                "retention_policy": self.retention.name,
                "memory_module": type(self.memory).__name__,
            },
            "entries": [
                {
                    "text": e.text,
                    "embedding": e.embedding.cpu(),
                    "surprise_score": e.surprise_score,
                    "timestamp": e.timestamp,
                    "access_count": e.access_count,
                    "source": e.source,
                    "superseded_at": e.superseded_at,
                    # v5 schema field — accidentally dropped pre-v6; restored
                    # here so corrections survive a save/load cycle.
                    "superseded_by_text": e.superseded_by_text,
                    "last_logical_turn": e.last_logical_turn,
                    "slots": e.slots,
                    # v6 schema fields — episode anchoring + tags.
                    "episode_id": e.episode_id,
                    "episode_title": e.episode_title,
                    "tags": e.tags,
                }
                for e in self.entries
            ],
        }

    def load_state_dict(self, state: dict) -> None:
        """Restore a previously-saved bank.

        Tolerant of partial state:

        * Missing ``axes`` (v0.4.x layout) is fine.
        * ``optimizer_state`` whose ``name`` doesn't match the current update
          rule is silently dropped — see :meth:`UpdateRule.load_state_dict`.
        * Memory-module *shape* mismatches (e.g. saved as MLP3, current
          config is MLP2) are caught: the memory weights stay at fresh init
          but the text entries + surprise EMA still restore. Without this,
          a config change that swaps the module body would silently wipe
          the user's memory entries.
        """
        # Memory weights — best-effort. A shape mismatch means the user
        # changed presets / band module body; we can't port the weights,
        # but the entries (text + embeddings) are still useful.
        try:
            self.memory.load_state_dict(state["memory_state"])
            self.memory.to(self.device)
        except Exception as exc:  # noqa: BLE001
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).warning(
                "MIRASBand %r: memory weights mismatch saved state "
                "(%s). Restoring entries only; weights stay at fresh init.",
                self.name, exc,
            )

        # Optimizer — best-effort; UpdateRule.load_state_dict already
        # silently drops mismatched layouts.
        self.update_rule.load_state_dict(state.get("optimizer_state", {}))

        # Surprise EMA + entries are architecture-agnostic — always load.
        self.surprise_ema = state.get("surprise_ema", 0.0)
        self.entries = [
            MemoryEntry(
                text=e["text"],
                embedding=e["embedding"].to(self.device),
                surprise_score=e["surprise_score"],
                timestamp=e["timestamp"],
                access_count=e["access_count"],
                source=e.get("source", ""),
                bank=self.name,
                superseded_at=e.get("superseded_at"),
                # ``superseded_by_text`` was declared in schema v5 but wasn't
                # actually persisted until v6's serialiser fix. Pre-v6 entries
                # default to ``None``; v6+ entries restore the field.
                superseded_by_text=e.get("superseded_by_text"),
                # ``last_logical_turn`` was added in schema v3; pre-v3 entries
                # don't have it and stay ``None`` (no breaking change).
                last_logical_turn=e.get("last_logical_turn"),
                # ``slots`` was added in schema v4; pre-v4 entries get [].
                slots=e.get("slots", []),
                # v6 fields — episode anchoring + tags. Pre-v6 entries default
                # to ``None`` / ``[]`` so existing on-disk state still loads.
                episode_id=e.get("episode_id"),
                episode_title=e.get("episode_title"),
                tags=list(e.get("tags") or []),
            )
            for e in state["entries"]
        ]
        self._dirty = True


def build_band(spec: "MIRASBandSpec", embedding_dim: int, device: str) -> MIRASBand:
    """Construct a :class:`MIRASBand` from a :class:`MIRASBandSpec`.

    This is the standard factory used by :class:`ContinuumMemorySystem`.
    Looks up the registered MIRAS components by name from the four
    registries (:data:`module.MODULE_REGISTRY`,
    :data:`update_rules.UPDATE_RULE_REGISTRY`, etc.) and wires them
    together with the band-level parameters (capacity, interval,
    promotion thresholds).
    """
    module = build_module(spec.memory_module, dim=embedding_dim, hidden_dim=spec.hidden_dim)
    # Build the policy first so we can forward its ``l1_coef`` to the update
    # rule — elastic-net retention's sparse-update behaviour is implemented
    # inside :class:`SurpriseModulatedUpdate`, not in the policy data itself.
    policy = build_policy(spec.retention_policy, weight_decay=spec.weight_decay)
    rule = build_update_rule(
        spec.update_rule,
        params=module.parameters(),
        base_lr=spec.learning_rate,
        weight_decay=spec.weight_decay,
        l1_coef=policy.l1_coef,
    )
    objective = build_objective(spec.objective, p=spec.objective_p)

    return MIRASBand(
        name=spec.name,
        embedding_dim=embedding_dim,
        memory_module=module,
        update_rule=rule,
        objective=objective,
        retention=policy,
        max_entries=spec.max_entries,
        update_interval=spec.update_interval,
        promotion_access_count=spec.promotion_access_count,
        promotion_surprise=spec.promotion_surprise,
        device=device,
    )
