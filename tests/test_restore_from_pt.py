import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import restore_from_pt  # noqa: E402

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

torch = pytest.importorskip("torch")


def test_restore_from_pt_loads_cms_snapshot_with_weights_only(tmp_path, pg_conn, pg_url, monkeypatch):
    """storage.migrate's legacy .pt loader deliberately uses weights_only=True
    to avoid unpickling arbitrary objects from an imported bank file (CWE-502).
    restore_from_pt.py reads the SAME .pt file format and must use the same
    guard — a stale/tampered .bak restored from an untrusted copy must not be
    able to execute arbitrary code via pickle."""
    cms_path = tmp_path / "cms_state.pt.pre-v8.bak"
    torch.save({"bands": {}, "episodes": {"episodes": {}}}, cms_path)
    cortex_path = tmp_path / "cortex_state.pt.pre-v8.bak"  # left absent on purpose

    calls: list[dict] = []
    real_load = torch.load

    def spy_load(*args, **kwargs):
        calls.append(kwargs)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(torch, "load", spy_load)
    monkeypatch.setattr(
        sys, "argv",
        ["restore_from_pt.py", "--dsn", pg_url,
         "--cms", str(cms_path), "--cortex", str(cortex_path)],
    )

    restore_from_pt.main()

    assert calls, "torch.load was never called"
    assert calls[0].get("weights_only") is True


def _rand_vec(seed: int, dim: int):
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(dim, generator=g)
    return v / v.norm()


def test_refuses_on_entry_dim_mismatch_and_writes_nothing(tmp_path, pg_conn, pg_url, monkeypatch):
    """A pre-v25 384-d snapshot restored against the live vector(1024) bank
    must refuse outright — restore_from_pt.py restores a same-era snapshot
    verbatim, it does not re-embed (2026-07-28 review escalation; see the
    module docstring's "DIMENSION SAFETY" section). Before the fix this
    inserted the 384-d vector verbatim, which either raised a pgvector
    dimension error mid-loop (partial restore) or, worse, could silently
    corrupt a future batched insert path."""
    cms_path = tmp_path / "cms_state.pt.pre-v8.bak"
    torch.save({
        "bands": {"instant": {"entries": [{
            "text": "legacy entry from a 384-d bank",
            "embedding": _rand_vec(1, 384).tolist(),
            "surprise_score": 0.1, "timestamp": 1.0,
        }]}},
        "episodes": {"episodes": {}},
    }, cms_path)
    cortex_path = tmp_path / "cortex_state.pt.pre-v8.bak"  # absent on purpose

    monkeypatch.setattr(
        sys, "argv",
        ["restore_from_pt.py", "--dsn", pg_url,
         "--cms", str(cms_path), "--cortex", str(cortex_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        restore_from_pt.main()
    assert exc_info.value.code == 1

    from pseudolife_memory.storage.postgres import PostgresStorage
    storage = PostgresStorage(pg_url)
    try:
        assert storage.load_entries() == [], (
            "the refusal must fire before any row is written")
    finally:
        storage.close()


def test_refuses_on_fact_dim_mismatch_and_writes_nothing(tmp_path, pg_conn, pg_url, monkeypatch):
    """Same refusal, cortex-facts side: a 384-d legacy cortex snapshot
    restored against the live vector(1024) bank must refuse before writing
    either the (dim-clean) entries or the mismatched facts."""
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot

    cms_path = tmp_path / "cms_state.pt.pre-v8.bak"  # absent on purpose
    cortex_path = tmp_path / "cortex_state.pt.pre-v8.bak"
    cortex = CortexStore()
    cortex.write_fact(Slot("legacy-proj", "language", "rust"),
                      _rand_vec(2, 384), confidence=0.9, support="user")
    cortex.save(cortex_path)

    monkeypatch.setattr(
        sys, "argv",
        ["restore_from_pt.py", "--dsn", pg_url,
         "--cms", str(cms_path), "--cortex", str(cortex_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        restore_from_pt.main()
    assert exc_info.value.code == 1

    from pseudolife_memory.storage.postgres import PostgresStorage
    storage = PostgresStorage(pg_url)
    try:
        assert storage.load_facts() == []
    finally:
        storage.close()
