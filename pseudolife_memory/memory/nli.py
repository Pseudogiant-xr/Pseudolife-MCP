"""NLI-based contradiction scorer for the fourth detection path.

Wraps ``cross-encoder/nli-deberta-v3-xsmall`` loaded **entirely from a local
directory** — the model is bundled into the PyInstaller distribution at
build time and must never be fetched from the HuggingFace Hub at runtime.

Offline enforcement
-------------------
``TRANSFORMERS_OFFLINE`` and ``HF_HUB_OFFLINE`` are set to ``"1"`` before any
``transformers`` or ``huggingface_hub`` import in this module.  This is a
defence-in-depth measure: even if the calling code has already imported those
libraries, the env-vars prevent any subsequent *network* call for cache
refreshes or config lookups.

Fail-soft semantics
-------------------
If the local model directory cannot be found or loading fails for any reason,
``_disabled`` is set to ``True`` and every public method becomes a harmless
no-op / empty return.  The three heuristic contradiction paths remain active
regardless.

Label mapping (verified against downloaded config.json)
-------------------------------------------------------
  0 → contradiction
  1 → entailment
  2 → neutral
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Contradiction label index (verified from config.json id2label).
_CONTRADICTION_IDX = 0
# Default model sub-directory name.
_MODEL_SLUG = "nli-deberta-v3-xsmall"


def _set_offline_env() -> None:
    """Block all HuggingFace network calls before any HF library code runs."""
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


def _resolve_model_path(model_dir: str | None = None) -> Path | None:
    """Return the local path to the NLI model, or None if not found.

    When *model_dir* is provided, **only** that path is checked — no fallback.
    This ensures that tests and custom installs that pass an explicit path
    get a clear failure if the path is wrong, rather than silently falling
    back to the dev-mode model.

    When *model_dir* is None, the following locations are tried in order:

    1. ``PSEUDOLIFE_MODELS_DIR`` env var — set by the Electron launcher in
       the slim-installer architecture to point at ``<installDir>/models``.
    2. PyInstaller bundle: ``sys._MEIPASS / models / <slug>`` (offline bundle variant).
    3. Directory next to the running executable.
    4. Dev-mode repo-local: ``<backend dir> / models / <slug>``
       (``src/memory/nli.py`` → ``src/memory`` → ``src`` → ``backend``).
    """
    if model_dir is not None:
        # Explicit override: check only the given path, no fallback.
        explicit = Path(model_dir)
        return explicit if (explicit / "config.json").exists() else None

    candidates: list[Path] = []

    # Slim-installer: Electron passes the install-dir models path via env.
    env_dir = os.environ.get("PSEUDOLIFE_MODELS_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / _MODEL_SLUG)

    # PyInstaller frozen bundle (offline bundle variant — unused in slim installer).
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "models" / _MODEL_SLUG)

    # Directory next to the running executable.
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "models" / _MODEL_SLUG)

    # Dev mode: repo ``backend/models/<slug>``.
    dev_backend = Path(__file__).resolve().parent.parent.parent
    candidates.append(dev_backend / "models" / _MODEL_SLUG)

    for candidate in candidates:
        if (candidate / "config.json").exists():
            return candidate

    return None


class NLIContradictionScorer:
    """Score memory-entry pairs for contradiction using a local NLI model.

    Parameters
    ----------
    model_dir:
        Explicit path to the model directory.  When ``None`` (default),
        :func:`_resolve_model_path` searches standard locations.
    threshold:
        Contradiction-score cut-off (0–1).  Pairs whose contradiction
        probability exceeds this value are flagged.
    """

    def __init__(
        self,
        model_dir: str | None = None,
        threshold: float = 0.70,
    ) -> None:
        self._model_dir = model_dir
        self.threshold = threshold
        self._model = None          # loaded lazily by _ensure_loaded
        self._disabled = False      # set True on any load failure

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the model is (or can be) loaded."""
        if self._disabled:
            return False
        return _resolve_model_path(self._model_dir) is not None

    def _ensure_loaded(self) -> bool:
        """Load the model if not yet loaded.  Return True on success."""
        if self._disabled:
            return False
        if self._model is not None:
            return True

        path = _resolve_model_path(self._model_dir)
        if path is None:
            logger.warning(
                "NLI model not found in any search location — disabling NLI path. "
                "Run: cd backend && python scripts/fetch_nli_model.py"
            )
            self._disabled = True
            return False

        # Enforce offline mode before the first HF import.
        _set_offline_env()

        try:
            from sentence_transformers import CrossEncoder  # noqa: PLC0415
            logger.info("Loading NLI model from %s", path)
            self._model = CrossEncoder(str(path))
            logger.info("NLI model loaded successfully")
            return True
        except Exception as exc:
            logger.warning("NLI model load failed (%s) — disabling NLI path", exc)
            self._disabled = True
            return False

    def score_contradictions(
        self,
        pairs: list[tuple[str, str]],
    ) -> list[float]:
        """Return contradiction scores (0–1) for each (premise, hypothesis) pair.

        The CrossEncoder's ``predict`` with ``apply_softmax=True`` returns a
        3-column array ``[contradiction, entailment, neutral]`` — index 0 is
        the contradiction probability.

        Returns an empty list when the scorer is unavailable.
        """
        if not pairs:
            return []
        if not self._ensure_loaded():
            return []

        try:
            import numpy as np  # noqa: PLC0415
            scores = self._model.predict(
                list(pairs),
                apply_softmax=True,
            )
            # scores shape: (N, 3) — column 0 is contradiction.
            arr = np.asarray(scores)
            if arr.ndim == 1:
                # Single pair edge case: CrossEncoder may return 1-D.
                return [float(arr[_CONTRADICTION_IDX])]
            return [float(row[_CONTRADICTION_IDX]) for row in arr]
        except Exception as exc:
            logger.warning("NLI scoring failed (%s)", exc)
            return []

    def flagged_indices(
        self,
        pairs: list[tuple[str, str]],
        threshold: float | None = None,
    ) -> list[int]:
        """Return the indices of pairs whose contradiction score ≥ threshold."""
        cutoff = threshold if threshold is not None else self.threshold
        scores = self.score_contradictions(pairs)
        return [i for i, s in enumerate(scores) if s >= cutoff]
