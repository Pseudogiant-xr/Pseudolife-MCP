"""Unit tests for the pure logic in evals/distill_datagen_arm1.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from distill_datagen_arm1 import (  # noqa: E402
    VOCAB_MAX, _registry_hint, _stored_system, _teacher_system,
    _update_registry,
)
from pseudolife_memory.memory.dream import _SYSTEM_PROMPT  # noqa: E402


def _claim(entity, attribute, value, source=1):
    return {"entity": entity, "attribute": attribute, "value": value,
            "confidence": 0.9, "source": source}


def test_registry_hint_empty_returns_empty_string():
    assert _registry_hint({}) == ""


def test_registry_hint_formats_entries():
    hint = _registry_hint({("miso", "species"): "cat"})
    assert "miso | species: cat" in hint
    assert "CHAIN REGISTRY" in hint
    assert "reuse the EXACT SAME entity/attribute key" in hint
    assert "Never emit a claim this session's notes don't evidence" in hint


def test_registry_hint_caps_at_vocab_max():
    registry = {(f"e{i}", "a"): "v" for i in range(VOCAB_MAX + 20)}
    hint = _registry_hint(registry)
    assert hint.count(" | a: v") == VOCAB_MAX


def test_update_registry_last_write_wins():
    registry: dict = {}
    _update_registry(registry, [_claim("miso", "species", "cat")])
    assert registry[("miso", "species")] == "cat"
    _update_registry(registry, [_claim("Miso", "Species", "dog")])
    assert registry[("miso", "species")] == "dog"          # normalized + overwritten


def test_update_registry_keeps_distinct_keys_apart():
    registry: dict = {}
    _update_registry(registry, [_claim("miso", "species", "cat"),
                                _claim("miso", "color", "black")])
    assert registry == {("miso", "species"): "cat", ("miso", "color"): "black"}


def test_teacher_system_starts_with_system_prompt():
    # required for the shim's prefix-substitution contract
    # (evals/sonnet_shim.py::ClaudeCli.chat checks system.startswith(_SYSTEM_PROMPT))
    system = _teacher_system(["miso.species"], {("miso", "species"): "cat"})
    assert system.startswith(_SYSTEM_PROMPT)
    assert "miso.species" in system                        # vocab hint present
    assert "miso | species: cat" in system                 # registry hint present


def test_stored_system_excludes_registry():
    system = _stored_system(["miso.species"])
    assert system.startswith(_SYSTEM_PROMPT)
    assert "miso.species" in system
    assert "CHAIN REGISTRY" not in system
    assert "registry" not in system.lower()


def test_teacher_system_with_empty_registry_matches_stored_system():
    vocab = ["miso.species"]
    assert _teacher_system(vocab, {}) == _stored_system(vocab)
