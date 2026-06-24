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
def test_schema_v12_creates_community_tables(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
    assert SCHEMA_META_VERSION == 12
    svc = build_service(tmp_path)
    svc._ensure_init()  # noqa: SLF001
    st = svc._storage  # noqa: SLF001
    for tbl in ("communities", "entity_communities"):
        row = st.conn.execute(
            "SELECT to_regclass(%s)", (f"public.{tbl}",)).fetchone()
        assert row[0] is not None, f"{tbl} not created"
