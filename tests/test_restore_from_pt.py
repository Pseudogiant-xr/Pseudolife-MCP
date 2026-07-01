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
