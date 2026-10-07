"""Dependency-free validation shared by runtime and image provisioning."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


_TRANSFORMERS = {
    "sentence_transformers.models.Transformer",
    "sentence_transformers.base.modules.Transformer",
    "sentence_transformers.base.modules.transformer.Transformer",
}
_POSTPROCESSORS = {
    f"sentence_transformers.{namespace}.{name}"
    for namespace in ("models", "base.modules")
    for name in ("Pooling", "Normalize", "Dense")
} | {
    "sentence_transformers.sentence_transformer.modules.pooling.Pooling",
    "sentence_transformers.sentence_transformer.modules.normalize.Normalize",
    "sentence_transformers.base.modules.dense.Dense",
}


def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("module paths must use relative POSIX paths")
    if value == "":
        return ()
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        not part or part in {".", ".."} or ":" in part
        for part in value.split("/")
    ):
        raise ValueError("module path leaves its model directory")
    return path.parts


def _existing_model_file(
    root: Path,
    parts: tuple[str, ...],
    *,
    allow_hub_blob_links: bool,
) -> bool:
    """Check one model-relative file without following a link off the path."""
    candidate = root.joinpath(*parts)
    if not candidate.is_file():
        return False
    try:
        resolved_root = root.resolve(strict=True)
        # Optimum discovers artifacts with ``Path(model_id).glob("**/*.onnx")``
        # and CPython's recursive glob does not descend into linked
        # directories, so a link anywhere between the root and the file hides
        # it from discovery even when the link target stays inside the root —
        # and a hidden artifact is exactly the export trigger this guards.
        if any(
            root.joinpath(*parts[:depth]).is_symlink()
            for depth in range(1, len(parts))
        ):
            return False
        candidate.parent.resolve(strict=True).relative_to(resolved_root)
        resolved_candidate = candidate.resolve(strict=True)
        if not candidate.is_symlink():
            resolved_candidate.relative_to(resolved_root)
            return True

        # Hub snapshots store files as leaf symlinks into this repository's
        # blobs directory — the only link this check accepts.
        if not allow_hub_blob_links or root.parent.name != "snapshots":
            return False
        blobs = root.parent.parent / "blobs"
        if not blobs.is_dir():
            return False
        resolved_candidate.relative_to(blobs.resolve(strict=True))
        return True
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False


def has_nested_transformer_module(root: str | Path) -> bool:
    """Report whether any recognized Transformer module loads from a subfolder.

    With the pinned Optimum stack, artifact discovery compiles the loader
    subfolder into a regular expression and matches it against OS-native path
    strings. A nested subfolder such as ``0_Transformer/onnx`` therefore never
    matches on native Windows, where the separator is a backslash, and export
    is re-enabled despite ``export=False``. A flat ``onnx`` subfolder matches
    on both platforms. Unreadable metadata reports nesting so callers that use
    this as a fallback gate stay on torch.
    """
    root = Path(root)
    modules_path = root / "modules.json"
    try:
        if not modules_path.exists():
            # Plain Hugging Face models use the root Transformer directly.
            return False
        modules = json.loads(modules_path.read_text(encoding="utf-8"))
        if not isinstance(modules, list):
            return True
        return any(
            isinstance(module, dict)
            and module.get("type") in _TRANSFORMERS
            and module.get("path")
            for module in modules
        )
    except (OSError, ValueError):
        return True


def onnx_layout_available(
    root: str | Path,
    file_name: str,
    *,
    allow_hub_blob_links: bool = False,
) -> bool:
    """Verify the effective artifact for each supported Transformer module.

    The caller's file_name/export override saved model arguments, but each
    module's path becomes a loader subfolder. Checking only the model root
    misses that subfolder. Unknown module classes fall back to torch rather
    than guessing their loading behavior. This is an artifact-availability
    check, not a guarantee that model files cannot change during loading.
    """
    root = Path(root)
    try:
        parts = _relative_parts(file_name)
        if not parts or PurePosixPath(file_name).suffix != ".onnx":
            return False
        modules_path = root / "modules.json"
        if not modules_path.exists():
            # Plain Hugging Face models use the root Transformer directly.
            return _existing_model_file(
                root, parts, allow_hub_blob_links=allow_hub_blob_links,
            )
        if not _existing_model_file(
            root,
            ("modules.json",),
            allow_hub_blob_links=allow_hub_blob_links,
        ):
            return False
        modules = json.loads(modules_path.read_text(encoding="utf-8"))
        if not isinstance(modules, list) or not modules:
            return False
        transformers = 0
        for module in modules:
            if not isinstance(module, dict):
                return False
            subfolder = _relative_parts(module["path"])
            kind = module["type"]
            if kind in _TRANSFORMERS:
                transformers += 1
                if not _existing_model_file(
                    root,
                    (*subfolder, *parts),
                    allow_hub_blob_links=allow_hub_blob_links,
                ):
                    return False
            elif kind not in _POSTPROCESSORS:
                return False
        return transformers > 0
    except (KeyError, TypeError, ValueError, OSError):
        return False
