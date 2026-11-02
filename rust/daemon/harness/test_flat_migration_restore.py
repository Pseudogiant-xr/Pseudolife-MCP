"""Physical restore proof on an owned disposable PostgreSQL owner connection.

The ordinary suite login cannot change extension metadata. The restore runner
supplies an isolated server and selects this file explicitly.
"""

from pseudolife_memory import migrate_flat_cli as migration
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

def test_real_backup_restores_bank_and_config(pg_conn, pg_url, tmp_path, monkeypatch, capsys):
    import gzip
    import os
    import subprocess
    import tarfile
    import uuid
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from pseudolife_memory.backup_cli import _find_pg_dump

    config = tmp_path / "config.yaml"
    original = b"unrelated: yes\nmemory:\n  miras:\n    preset: continuum\n"
    config.write_bytes(original)
    pg_conn.execute("INSERT INTO episodes (id,title,started_at) VALUES ('ep','session',42)")
    pg_conn.execute("INSERT INTO entries (band,text,embedding,surprise,ts,episode_id) "
                    "VALUES ('forever','survives',%s::vector,0.4,42,'ep')",
                    ("[" + ",".join(["0.1"] * 1024) + "]",))
    pg_conn.commit()
    before = pg_conn.execute("SELECT to_jsonb(e) FROM entries e ORDER BY id").fetchall()
    pg_conn.commit()
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    assert migration.main(["--apply", "--data-dir", str(tmp_path), "--external-recovery-verified"]) == 0
    output = capsys.readouterr()
    assert "WARNING: external recovery acknowledged" in output.err
    assert "ops/restore" not in output.out
    result = pg_conn.execute("SELECT value FROM meta WHERE key=%s", (migration.MARKER,)).fetchone()[0]
    pg_conn.commit()
    assert result["complete"] is True
    with tarfile.open(result["backup"]["state"]) as archive:
        assert archive.extractfile("config.yaml").read() == original
    dump = _find_pg_dump()
    assert dump is not None, "pg_dump is required for this migration proof"
    psql = dump.with_name("psql.exe" if os.name == "nt" else "psql")
    assert psql.is_file(), "psql is required for the restore rehearsal"
    target = "pseudolife_memory_test_r11_restore_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        try:
            params = conninfo_to_dict(str(pg_url))
            params["dbname"] = target
            env = dict(os.environ)
            password = params.pop("password", None)
            if password is not None:
                env["PGPASSWORD"] = password
            target_dsn = make_conninfo(**params)
            with gzip.open(result["backup"]["dump"], "rb") as source:
                restored = subprocess.run([str(psql), "--dbname", target_dsn, "-X", "-q",
                                           "-v", "ON_ERROR_STOP=1"], input=source.read(),
                                          capture_output=True, env=env, timeout=120)
            assert restored.returncode == 0, "physical dump restore failed"
            with psycopg.connect(pg_url, dbname=target) as restored_conn:
                assert restored_conn.execute("SELECT to_jsonb(e) FROM entries e ORDER BY id").fetchall() == before
                assert restored_conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1
                assert restored_conn.execute("SELECT value FROM meta WHERE key=%s", (migration.MARKER,)).fetchone() is None
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target)))
