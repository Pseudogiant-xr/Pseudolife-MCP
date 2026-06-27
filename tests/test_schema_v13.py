import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_ADMIN = os.environ.get("PSEUDOLIFE_BENCH_ADMIN_URL",
                        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres")


def _pg_up() -> bool:
    try:
        import psycopg
        with psycopg.connect(_ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_schema_v13_traces_and_reinforcements(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
    assert SCHEMA_META_VERSION == 15
    svc = build_service(tmp_path)
    svc._ensure_init()  # noqa: SLF001
    st = svc._storage  # noqa: SLF001
    assert st.conn.execute("SELECT to_regclass('public.memory_traces')").fetchone()[0]
    cols = {r[0] for r in st.conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='memory_traces'").fetchall()}
    # Slot-keyed shape (NOT fact_id) — lock it so a regression to the old anchor fails.
    assert {"entity_norm", "attribute_norm", "entry_id", "created_at"} <= cols
    assert "fact_id" not in cols
    col = st.conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='entries' AND column_name='reinforcements'").fetchone()
    assert col is not None
