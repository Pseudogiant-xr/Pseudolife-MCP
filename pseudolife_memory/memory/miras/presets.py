"""Named MIRAS presets — point configurations in the design space.

A preset returns a list of :class:`MIRASBandSpec` for the
:class:`src.utils.config.MIRASConfig` to load. Users select a preset by
name in ``backend/config.yaml``:

.. code-block:: yaml

    memory:
      miras:
        preset: titans   # or: moneta / yaad / memora / custom

When ``preset = custom``, the loader leaves whatever ``bands`` block the
user provided in YAML untouched.

The presets are not meant to be "best" — they're hypothesis-tested
points in the MIRAS space. ``titans`` is the safe default (reproduces
v0.4.x behaviour); the others let users experiment without writing band
specs by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pseudolife_memory.utils.config import MIRASBandSpec


def _spec(**overrides: object) -> "MIRASBandSpec":
    """Construct a band spec with current dataclass defaults, then apply overrides.

    We import inside the function to dodge the circular import between
    ``config.py`` (defines :class:`MIRASBandSpec`) and ``presets.py`` (used by
    ``config.py``'s default factory).
    """
    from pseudolife_memory.utils.config import MIRASBandSpec  # noqa: PLC0415

    return MIRASBandSpec(**overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# titans — the v0.4.x default. Reproduces existing behaviour bit-for-bit.
# ---------------------------------------------------------------------------


def titans_bands() -> list["MIRASBandSpec"]:
    """3 bands: instant / short_term / long_term.

    Values mirror the v0.4.x ``TitansConfig`` defaults exactly so that
    ``preset: titans`` is a zero-surprise upgrade path for existing
    installs.
    """
    return [
        _spec(
            name="instant",
            hidden_dim=512,
            max_entries=2000,
            learning_rate=0.01,
            update_interval=1,
            promotion_access_count=2,
            promotion_surprise=0.5,
            memory_module="mlp3",
            update_rule="sgd_momentum",
            objective="l2",
            objective_p=2.0,
            retention_policy="balanced",
            weight_decay=0.001,
        ),
        _spec(
            name="short_term",
            hidden_dim=512,
            max_entries=5000,
            learning_rate=0.001,
            update_interval=5,
            promotion_access_count=3,
            promotion_surprise=0.7,
            memory_module="mlp3",
            update_rule="sgd_momentum",
            objective="l2",
            objective_p=2.0,
            retention_policy="balanced",
            weight_decay=0.001,
        ),
        _spec(
            name="long_term",
            hidden_dim=768,
            max_entries=10000,
            learning_rate=0.0001,
            update_interval=20,
            # Long-term is the terminal band; promotion fields don't matter
            # but we keep them populated for symmetry / introspection.
            promotion_access_count=999_999,
            promotion_surprise=1.0,
            memory_module="mlp3",
            update_rule="sgd_momentum",
            objective="l2",
            objective_p=2.0,
            retention_policy="balanced",
            weight_decay=0.001,
        ),
    ]


# ---------------------------------------------------------------------------
# moneta — Adam optimiser + L_p(1.5) loss. Tests whether adaptive moments
# help when the loss surface is between L1 and L2.
# ---------------------------------------------------------------------------


def moneta_bands() -> list["MIRASBandSpec"]:
    """3 bands, Adam + L_p(1.5), same intervals/capacities as titans.

    Adam normalises by gradient magnitude, so the base LR is an order of
    magnitude smaller than the SGD-momentum analogue.
    """
    return [
        _spec(
            name="instant",
            hidden_dim=512,
            max_entries=2000,
            learning_rate=0.001,
            update_interval=1,
            promotion_access_count=2,
            promotion_surprise=0.5,
            memory_module="mlp3",
            update_rule="adam",
            objective="lp",
            objective_p=1.5,
            retention_policy="recency_heavy",
            weight_decay=0.001,
        ),
        _spec(
            name="short_term",
            hidden_dim=512,
            max_entries=5000,
            learning_rate=0.0003,
            update_interval=5,
            promotion_access_count=3,
            promotion_surprise=0.7,
            memory_module="mlp3",
            update_rule="adam",
            objective="lp",
            objective_p=1.5,
            retention_policy="balanced",
            weight_decay=0.001,
        ),
        _spec(
            name="long_term",
            hidden_dim=768,
            max_entries=10000,
            learning_rate=0.0001,
            update_interval=20,
            promotion_access_count=999_999,
            promotion_surprise=1.0,
            memory_module="mlp3",
            update_rule="adam",
            objective="lp",
            objective_p=1.5,
            retention_policy="surprise_heavy",
            weight_decay=0.0005,
        ),
    ]


# ---------------------------------------------------------------------------
# yaad — mixed architectures. The first band is a cheap linear memory
# with SGD; slower bands use MLPs + Adam. Tests whether the instant tier
# benefits from the cheaper module's tighter feedback loop.
# ---------------------------------------------------------------------------


def yaad_bands() -> list["MIRASBandSpec"]:
    return [
        _spec(
            name="instant",
            hidden_dim=384,  # linear ignores this; kept for symmetry
            max_entries=2000,
            learning_rate=0.02,
            update_interval=1,
            promotion_access_count=2,
            promotion_surprise=0.5,
            memory_module="linear",
            update_rule="sgd_momentum",
            objective="l2",
            objective_p=2.0,
            retention_policy="recency_heavy",
            weight_decay=0.002,
        ),
        _spec(
            name="short_term",
            hidden_dim=512,
            max_entries=5000,
            learning_rate=0.0005,
            update_interval=5,
            promotion_access_count=3,
            promotion_surprise=0.7,
            memory_module="mlp3",
            update_rule="adam",
            objective="l2",
            objective_p=2.0,
            retention_policy="balanced",
            weight_decay=0.001,
        ),
        _spec(
            name="long_term",
            hidden_dim=768,
            max_entries=10000,
            learning_rate=0.0001,
            update_interval=20,
            promotion_access_count=999_999,
            promotion_surprise=1.0,
            memory_module="mlp3",
            update_rule="adam",
            objective="l2",
            objective_p=2.0,
            retention_policy="surprise_heavy",
            weight_decay=0.0005,
        ),
    ]


# ---------------------------------------------------------------------------
# memora — N>3 continuum. 5 bands with geometric LR/interval spacing,
# Lion update + L_p(1.5) loss. Demonstrates the "memory as a continuum"
# claim from the Nested Learning paper.
# ---------------------------------------------------------------------------


def memora_bands() -> list["MIRASBandSpec"]:
    """5 bands at intervals 1 / 3 / 10 / 30 / 100, LRs 0.02 .. 5e-5."""
    return [
        _spec(
            name="instant",
            hidden_dim=384,
            max_entries=1500,
            learning_rate=0.02,
            update_interval=1,
            promotion_access_count=2,
            promotion_surprise=0.4,
            memory_module="mlp2",
            update_rule="lion",
            objective="lp",
            objective_p=1.5,
            retention_policy="recency_heavy",
            weight_decay=0.001,
        ),
        _spec(
            name="fast",
            hidden_dim=512,
            max_entries=3000,
            learning_rate=0.005,
            update_interval=3,
            promotion_access_count=3,
            promotion_surprise=0.5,
            memory_module="mlp3",
            update_rule="lion",
            objective="lp",
            objective_p=1.5,
            retention_policy="recency_heavy",
            weight_decay=0.001,
        ),
        _spec(
            name="medium",
            hidden_dim=512,
            max_entries=5000,
            learning_rate=0.001,
            update_interval=10,
            promotion_access_count=3,
            promotion_surprise=0.6,
            memory_module="mlp3",
            update_rule="lion",
            objective="lp",
            objective_p=1.5,
            retention_policy="balanced",
            weight_decay=0.0008,
        ),
        _spec(
            name="slow",
            hidden_dim=768,
            max_entries=7500,
            learning_rate=0.0002,
            update_interval=30,
            promotion_access_count=4,
            promotion_surprise=0.7,
            memory_module="mlp3",
            update_rule="lion",
            objective="lp",
            objective_p=1.5,
            retention_policy="surprise_heavy",
            weight_decay=0.0005,
        ),
        _spec(
            name="archival",
            hidden_dim=768,
            max_entries=10000,
            learning_rate=0.00005,
            update_interval=100,
            promotion_access_count=999_999,
            promotion_surprise=1.0,
            memory_module="mlp3",
            update_rule="momentum_only",
            objective="neg_sim",
            objective_p=2.0,
            retention_policy="surprise_heavy",
            weight_decay=0.0,
        ),
    ]


# ---------------------------------------------------------------------------
# continuum — v0.6: 8-tier agentic-grade preset.
# ---------------------------------------------------------------------------
# Geometric spacing ~3× per tier across update_interval and learning_rate.
# Span: 1→1000 in interval (3 OOMs), 0.05→3e-5 in LR (3.2 OOMs).
# All MIRAS axes engaged appropriately per tier — fast tiers use
# elastic_net + sm_sgd + L2 for sharp, sparse, snap-to-new updates; mid
# tiers shift to Huber + Adam for outlier-robust consolidation; slow
# tiers use surprise_heavy + Lion + neg_sim for stable knowledge.  See
# backend/MIRAS.md for the literature basis (HOPE / MIRAS / TNT).


def continuum_bands() -> list["MIRASBandSpec"]:
    """8 tiers: working / micro / instant / fast / medium / slow / archival /
    forever.

    Designed for agentic deployments — many bookkeeping stores per logical
    turn, long-running sessions, source-aware retention (tool_call vs
    user_msg vs llm_thinking).  Pairs with
    :meth:`ContinuumMemorySystem.begin_logical_turn` so consolidation
    fires per agent step, not per raw store.  Sequential
    ``chain_residual`` is on (see :func:`preset_chain_residual`) for
    HOPE-style read-time abstraction.

    Per-tier rationale:

    * **working / micro / instant** — high LR, MLP3 / 384,
      elastic-net retention.  Sparse weight changes per update so many
      distinct very-recent patterns coexist without interfering.
      Surprise-modulated SGD-momentum for principled plasticity.
    * **fast** — transition tier; same module shape, balanced retention.
    * **medium / slow** — wider MLP / 512, switch to ``huber`` objective
      so noisy consolidation from faster tiers doesn't dominate.  Adam
      adapts to per-dim gradient scales.  surprise_heavy retention
      protects the rare-but-important signals.
    * **archival** — MLP3 / 768, Lion sign-based updates (cheap +
      robust at slow timescale), huber objective.
    * **forever** — widest module / 1024, ``momentum_only`` (no LR
      modulation past the schedule), ``neg_sim`` objective.  Identity /
      core-invariants tier — small steps only, very high promotion bar.
    """
    return [
        # 0 — working: every store, snap to brand-new facts
        _spec(
            name="working",
            hidden_dim=256, max_entries=1500, learning_rate=0.05,
            update_interval=1, promotion_access_count=2, promotion_surprise=0.4,
            memory_module="mlp3",
            update_rule="sm_sgd_momentum", objective="l2",
            retention_policy="elastic_net", weight_decay=0.005,
        ),
        # 1 — micro: within-turn working set
        _spec(
            name="micro",
            hidden_dim=384, max_entries=2000, learning_rate=0.02,
            update_interval=1, promotion_access_count=2, promotion_surprise=0.45,
            memory_module="mlp3",
            update_rule="sm_sgd_momentum", objective="l2",
            retention_policy="elastic_net", weight_decay=0.004,
        ),
        # 2 — instant: last few turns
        _spec(
            name="instant",
            hidden_dim=384, max_entries=2500, learning_rate=0.008,
            update_interval=2, promotion_access_count=2, promotion_surprise=0.5,
            memory_module="mlp3",
            update_rule="sm_sgd_momentum", objective="l2",
            retention_policy="balanced", weight_decay=0.002,
        ),
        # 3 — fast: multi-turn topic
        _spec(
            name="fast",
            hidden_dim=512, max_entries=4000, learning_rate=0.003,
            update_interval=5, promotion_access_count=3, promotion_surprise=0.55,
            memory_module="mlp3",
            update_rule="sm_adam", objective="l2",
            retention_policy="balanced", weight_decay=0.001,
        ),
        # 4 — medium: session-level
        _spec(
            name="medium",
            hidden_dim=512, max_entries=6000, learning_rate=0.001,
            update_interval=15, promotion_access_count=3, promotion_surprise=0.65,
            memory_module="mlp3",
            update_rule="sm_adam", objective="huber",
            retention_policy="balanced", weight_decay=0.0008,
        ),
        # 5 — slow: cross-session abstractions
        _spec(
            name="slow",
            hidden_dim=768, max_entries=8000, learning_rate=3e-4,
            update_interval=50, promotion_access_count=4, promotion_surprise=0.7,
            memory_module="mlp3",
            update_rule="sm_adam", objective="huber",
            retention_policy="surprise_heavy", weight_decay=0.0005,
        ),
        # 6 — archival: long-term consolidated knowledge
        _spec(
            name="archival",
            hidden_dim=768, max_entries=10000, learning_rate=1e-4,
            update_interval=200, promotion_access_count=5, promotion_surprise=0.8,
            memory_module="mlp3",
            update_rule="sm_lion", objective="huber",
            retention_policy="surprise_heavy", weight_decay=0.0,
        ),
        # 7 — forever: identity / invariants
        _spec(
            name="forever",
            hidden_dim=1024, max_entries=10000, learning_rate=3e-5,
            update_interval=1000, promotion_access_count=999_999, promotion_surprise=1.0,
            memory_module="mlp3",
            update_rule="momentum_only", objective="neg_sim",
            retention_policy="surprise_heavy", weight_decay=0.0,
        ),
    ]


# ---------------------------------------------------------------------------
# Registry + lookup
# ---------------------------------------------------------------------------

PRESET_REGISTRY = {
    "titans": titans_bands,
    "moneta": moneta_bands,
    "yaad": yaad_bands,
    "memora": memora_bands,
    "continuum": continuum_bands,
}

# Per-preset default value for ``MIRASConfig.chain_residual``.  Most presets
# match v0.4.x / v0.5.x behaviour (no chaining) — only ``continuum`` opts in
# to the HOPE-style sequential read.
PRESET_CHAIN_RESIDUAL: dict[str, bool] = {
    "titans": False,
    "moneta": False,
    "yaad": False,
    "memora": False,
    "continuum": True,
}


def preset_chain_residual(name: str) -> bool:
    """Default ``chain_residual`` flag for a named preset. ``custom`` → False."""
    return PRESET_CHAIN_RESIDUAL.get(name, False)


def preset_bands(name: str) -> list["MIRASBandSpec"]:
    """Return the band specs for a named preset.

    ``custom`` is not in the registry by design — when the YAML loader
    sees ``preset: custom`` it skips this lookup and uses whatever
    ``bands`` block the user wrote.
    """
    try:
        return PRESET_REGISTRY[name]()
    except KeyError as exc:
        raise ValueError(
            f"Unknown MIRAS preset {name!r}. Available: {list(PRESET_REGISTRY)} + 'custom'"
        ) from exc
