"""The shim's prefix-substitution contract.

``evals/claude_shim.py::ClaudeCli.chat`` applies ``--system-override`` only
when the incoming system prompt ``startswith(dream._SYSTEM_PROMPT)``, swapping
that prefix and preserving whatever the caller appended after it (vocab hint,
registry hint). A harness that builds its system prompt any other way silently
runs the production prompt instead of the variant it thinks it measures — so
the builders keep the prefix, and the stored (non-teacher) prompt keeps the
teacher-only registry hint out.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from distill_datagen_arm1 import _stored_system, _teacher_system  # noqa: E402
from pseudolife_memory.memory.dream import _SYSTEM_PROMPT  # noqa: E402


def test_teacher_system_starts_with_system_prompt():
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
