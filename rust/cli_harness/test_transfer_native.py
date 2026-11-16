"""Native transfer refusals and atomic import on disposable test databases."""
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from rust.cli_harness.rows import _bank


@pytest.fixture
def native(tmp_path):
    binary = os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if not binary:
        pytest.skip("PSEUDOLIFE_RUST_BINARY must name the native client")
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               APPDATA=str(tmp_path / "roaming"), LOCALAPPDATA=str(tmp_path / "local"),
               PSEUDOLIFE_MCP_DAEMON_URL="http://127.0.0.1:9",
               PSEUDOLIFE_MCP_DATABASE_URL="postgresql://nobody@127.0.0.1:9/pl_cf_absent",
               PSEUDOLIFE_RELEASE_CHECK="0", BROWSER="no-browser")

    def run(*args, dsn=None, extra=None, bootstrap=None):
        child = dict(env)
        if dsn:
            child["PSEUDOLIFE_MCP_DATABASE_URL"] = dsn
        for key, value in (extra or {}).items():
            if value is None:
                child.pop(key, None)
            else:
                child[key] = value
        command = [str(Path(binary).resolve()), *args]
        if bootstrap:
            command = [sys.executable, "-c", bootstrap, *command]
        result = subprocess.run(command, cwd=tmp_path,
                                env=child, capture_output=True, timeout=60)
        return result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8")
    return run


def archive(path, manifest=None, members=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("manifest.json", manifest if isinstance(manifest, bytes) else
                     json.dumps(manifest if manifest is not None else {"format_version": 1, "schema_version": 59}))
        for name, value in (members or {}).items():
            out.writestr(name, value)


def refused(result, cause):
    code, stdout, stderr = result
    assert code == 1
    assert not stdout
    assert "defer" not in stderr.lower()
    assert cause in stderr, stderr
    assert " — " in stderr, stderr


@pytest.mark.parametrize("mode", ["export", "import"])
@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_is_independent_of_terminal_width(native, mode, flag):
    code, stdout, stderr = native(mode, flag, extra={"COLUMNS": "120"})
    assert code == 0 and "usage:" in stdout and not stderr


@pytest.mark.parametrize("args,cause", [
    (("export", "--ou", "x.zip"), "--ou"),
    (("export", "--out"), "--out"),
    (("export", "--out", ""), "--out"),
    (("export", "--force"), "--force"),
    (("import",), "archive"),
    (("import", "a.zip", "b.zip"), "b.zip"),
    (("import", "a.zip", "--data-dir"), "--data-dir"),
])
def test_argument_refusals(native, args, cause):
    refused(native(*args), cause)


@pytest.mark.parametrize("args", [
    ("export", "--out=x.zip"), ("export", "--out", "a.zip", "--out", "b.zip"),
    ("export", "--out=./x.zip"), ("export", "--out=-x.zip"),
    ("export", "--data-dir=x", "--out", "x.zip"),
])
def test_valid_argument_and_path_spellings_reach_connection(native, args):
    refused(native(*args), "PostgreSQL connection failed")


def test_dsn_refusal_is_specific_and_does_not_echo_credentials(native):
    result = native("export", extra={"PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://fixture-secret@host/?unknown=1"})
    refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")
    assert "fixture-secret" not in result[2]


@pytest.mark.parametrize("extra", [{"TZ": "UTC"}, {"HOME": None, "LOCALAPPDATA": None},
                                   {"PYTHONINTMAXSTRDIGITS": "0"}])
def test_default_export_ignores_python_only_environment(native, extra):
    refused(native("export", extra=extra), "PostgreSQL connection failed")


def test_native_dsn_options_are_preserved(native):
    refused(native("export", extra={"PSEUDOLIFE_MCP_DATABASE_URL":
            "postgresql://nobody@127.0.0.1:9/pl_cf_absent?options=-c%20timezone%3DUTC"}),
            "unsupported PostgreSQL DSN option: options")
    refused(native("export", extra={"PSEUDOLIFE_MCP_DATABASE_URL":
            "postgresql://nobody@127.0.0.1:9/pl_cf_absent?connect_timeout=1&application_name=transfer-test"}),
            "PostgreSQL connection failed")


@pytest.mark.parametrize("kind,cause", [("directory", "directory"), ("part", ".part"), ("parent", "parent")])
def test_destination_refusals_preserve_files(native, tmp_path, kind, cause):
    if kind == "directory":
        (tmp_path / "bank.zip").mkdir()
    elif kind == "part":
        (tmp_path / "bank.zip.part").write_bytes(b"owned elsewhere")
    else:
        (tmp_path / "parent").write_bytes(b"not a directory")
    target = "parent/bank.zip" if kind == "parent" else "bank.zip"
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    refused(native("export", "--out", target), cause)
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("manifest,cause", [
    (b"{", "manifest.json"), (b"[]", "object"), (b"\xff", "UTF-8"),
    (b'{"format_version":[]}', "format_version"),
    (b'{"format_version":1,"embedding_dim":{}}', "embedding_dim"),
    (b'{"format_version":1,"schema_version":"bad"}', "schema_version"),
    (b'{"format_version":1,"x":"\\ud800"}', "surrogate"),
    (b'{"format_version":1,"x":' + b"9" * 4301 + b"}", "4300"),
    (b'{"format_version":1,"x":' + b"[" * 201 + b"0" + b"]" * 201 + b"}", "200"),
], ids=["syntax", "object", "encoding", "format", "dimension", "schema", "surrogate", "integer-limit", "depth-limit"])
def test_manifest_refusals_before_connect(native, tmp_path, manifest, cause):
    archive(tmp_path / "bank.zip", manifest, {"entries.jsonl": ""})
    refused(native("import", "bank.zip"), cause)


@pytest.mark.parametrize("manifest", [b'{"format_version":1}', b'\xef\xbb\xbf{"format_version":1}',
    b'{"format_version":1,"x":"\\ud83d\\ude00"}',
    b'{"format_version":1,"x":' + b"9" * 4300 + b"}",
    b'{"format_version":1,"x":' + b"[" * 199 + b"0" + b"]" * 199 + b"}"])
def test_manifest_utf8_near_miss_reaches_connection(native, tmp_path, manifest):
    archive(tmp_path / "bank.zip", manifest)
    refused(native("import", "./bank.zip", "--force", "--force"), "PostgreSQL connection failed")


@pytest.mark.parametrize("kind,cause", [("missing", "archive"), ("invalid", "ZIP"),
                                       ("duplicate", "duplicate"), ("bzip2", "compression")])
def test_archive_container_refusals(native, tmp_path, kind, cause):
    path = tmp_path / "bank.zip"
    if kind == "invalid":
        path.write_bytes(b"not a ZIP archive")
    elif kind in ("duplicate", "bzip2"):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_BZIP2 if kind == "bzip2" else zipfile.ZIP_STORED) as out:
            out.writestr("manifest.json", '{"format_version":1}')
            if kind == "duplicate":
                out.writestr("manifest.json", '{"format_version":1}')
    refused(native("import", "bank.zip"), cause)


@pytest.mark.parametrize("member,row,cause", [
    ("meta.jsonl", '{"key":"x","value":0,"unknown":1}', "column(s)"),
    ("meta.jsonl", "[]", "object"), ("meta.jsonl", '{"key":1,"value":0}', "meta.key"),
    ("meta.jsonl", '{"key":"x"}', "meta.value"),
    ("meta.jsonl", '{"key":"x\\u0000","value":0}', "U+0000"),
    ("meta.jsonl", '{"key":"x","value":NaN}', "finite"),
    ("relations.jsonl", '{"name":"a","inverse_of":1}', "inverse_of"),
    ("relations.jsonl", '{"inverse_of":"a"}', "relations.name"),
    ("relations.jsonl", '{"name":"a","inverse_of":"b\\u0000"}', "U+0000"),
    ("episodes.jsonl", '{}', "empty"), ("episodes.jsonl", b"\xff", "UTF-8"),
])
def test_archive_values_refused_before_connect(native, tmp_path, member, row, cause):
    archive(tmp_path / "bank.zip", members={member: row})
    refused(native("import", "bank.zip"), cause)


@pytest.fixture
def bank():
    name = _bank.name("pl_cf_native_transfer")
    _bank.create(name)
    try:
        yield name
    finally:
        _bank.drop(name)


@pytest.mark.parametrize("member,row,cause", [
    ("episodes.jsonl", '{"id":"x","started_at":"bad"}', "episodes.started_at"),
    ("entries.jsonl", '{"surprise":1e39}', "entries.surprise"),
    ("entries.jsonl", '{"embedding":"[1,2]"}', "entries.embedding"),
    ("entries.jsonl", '{"surprise":1e-100}', "entries.surprise"),
    ("entities.jsonl", '{"id":9223372036854775808}', "entities.id"),
    ("episodes.jsonl", '{"closed_by_new_start":"yes"}', "episodes.closed_by_new_start"),
])
def test_refusal_rolls_back_schema_and_every_table(native, tmp_path, bank, member, row, cause):
    with _bank.connect(bank) as conn:
        conn.execute("UPDATE meta SET value = '54' WHERE key = 'schema_version'")
        conn.commit()
    before = _bank.dump(bank)
    archive(tmp_path / "bank.zip", members={member: row})
    refused(native("import", "bank.zip", dsn=_bank.url(bank)), cause)
    assert _bank.dump(bank) == before


def test_success_commits_schema_and_data_together(native, tmp_path, bank):
    _bank.drop(bank)
    _bank.create(bank, schema=False)
    archive(tmp_path / "bank.zip", members={"meta.jsonl": '{"key":"native_roundtrip","value":{"ok":true}}'})
    code, stdout, stderr = native("import", "bank.zip", dsn=_bank.url(bank))
    assert code == 0 and "imported:" in stdout and not stderr
    with _bank.connect(bank) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == 59
        assert conn.execute("SELECT value FROM meta WHERE key='native_roundtrip'").fetchone()[0] == {"ok": True}


@pytest.mark.parametrize("member,row", [
    ("meta.jsonl", '{"key":"x","value":null}'),
    ("meta.jsonl", '{"key":"x","value":{"n":1.25,"s":"literal U+0000"}}'),
    ("episodes.jsonl", '{"id":"x","title":"test","started_at":1.25}'),
    ("entities.jsonl", '{"id":1,"canonical":"a","display":"a","created_at":1}'),
    ("episodes.jsonl", '{"id":"x","title":"test","started_at":1,"closed_by_new_start":true}'),
    ("relations.jsonl", '{"name":"inverse_one","description":"test","created_at":1,"inverse_of":null}'),
    ("relations.jsonl", '{"name":"inverse_one","description":"test","created_at":1,"inverse_of":"inverse_two"}\n{"name":"inverse_two","description":"test","created_at":1,"inverse_of":"inverse_one"}'),
])
def test_valid_archive_values_are_not_blocked(native, tmp_path, bank, member, row):
    archive(tmp_path / "bank.zip", members={member: row})
    code, stdout, stderr = native("import", "bank.zip", dsn=_bank.url(bank))
    assert code == 0 and "imported:" in stdout and not stderr


@pytest.mark.parametrize("sql,cause", [
    ("UPDATE meta SET value='54' WHERE key='schema_version'", "schema_version"),
    ("CREATE TABLE unclassified (id int)", "unclassified"),
    ("ALTER TABLE episodes ADD COLUMN unhandled bytea", "episodes.unhandled"),
    ("DROP TABLE reference_chunks CASCADE", "reference_chunks"),
])
def test_export_schema_refusals_leave_no_files(native, tmp_path, bank, sql, cause):
    with _bank.connect(bank) as conn:
        conn.execute(sql)
        conn.commit()
    before = _bank.dump(bank)
    refused(native("export", "--out", "new/bank.zip", dsn=_bank.url(bank)), cause)
    assert not (tmp_path / "new").exists()
    assert _bank.dump(bank) == before


@pytest.mark.parametrize("timestamp", ["infinity", "-infinity", "12000-01-01 00:00:00+00", "2026-01-01 00:00:00+00"])
def test_export_native_timestamps_ignore_session_rendering(native, tmp_path, bank, timestamp):
    with _bank.connect(bank) as conn:
        conn.execute("INSERT INTO chronicle_events (occurred_at,recorded_at,actor,actor_norm,description,description_norm) VALUES (%s,1,'a','a','test','test')", (timestamp,))
        conn.execute('ALTER DATABASE "' + bank + '" SET timezone = \'America/New_York\'')
        conn.execute('ALTER DATABASE "' + bank + '" SET datestyle = \'SQL, DMY\'')
        conn.commit()
    code, _, stderr = native("export", "--out", "./bank.zip", dsn=_bank.url(bank))
    assert code == 0 and not stderr
    with zipfile.ZipFile(tmp_path / "bank.zip") as source:
        row = json.loads(source.read("chronicle_events.jsonl"))
    assert row["occurred_at"].startswith(timestamp.split(" ")[0])


def test_export_json_limit_refusal_and_near_miss(native, tmp_path, bank):
    for depth, works in [(201, False), (199, True)]:
        with _bank.connect(bank) as conn:
            conn.execute("INSERT INTO meta (key,value) VALUES ('deep',%s::jsonb) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("[" * depth + "0" + "]" * depth,))
            conn.commit()
        result = native("export", "--out", "bank.zip", dsn=_bank.url(bank))
        if works:
            assert result[0] == 0 and not result[2]
        else:
            refused(result, "meta.value")
            assert "200" in result[2]
            assert not (tmp_path / "bank.zip").exists()


def test_export_query_refusal_names_sqlstate_and_near_miss(native, tmp_path, bank):
    with _bank.connect(bank) as conn:
        conn.execute("REVOKE SELECT ON episodes FROM CURRENT_USER")
        conn.commit()
    refused(native("export", "--out", "bank.zip", dsn=_bank.url(bank)), "42501")
    assert not (tmp_path / "bank.zip").exists()
    with _bank.connect(bank) as conn:
        conn.execute("GRANT SELECT ON episodes TO CURRENT_USER")
        conn.commit()
    assert native("export", "--out", "bank.zip", dsn=_bank.url(bank))[0] == 0


def test_non_utf8_server_refused_and_utf8_near_miss(native, tmp_path, bank):
    _bank.drop(bank)
    with _bank._admin() as conn:
        conn.execute(f'CREATE DATABASE "{bank}" TEMPLATE template0 '
                     "ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C'")
        conn.execute(f'ALTER DATABASE "{bank}" SET client_encoding TO \'UTF8\'')
    archive(tmp_path / "bank.zip")
    for args in [("export", "--out", "out.zip"), ("import", "bank.zip")]:
        refused(native(*args, dsn=_bank.url(bank)), "server_encoding")
    _bank.drop(bank)
    _bank.create(bank)
    assert native("import", "bank.zip", dsn=_bank.url(bank))[0] == 0


@pytest.mark.parametrize("kind,cause", [("encrypted", "encrypted"), ("checksum", "decompress")])
def test_archive_integrity_refusals(native, tmp_path, kind, cause):
    path = tmp_path / "bank.zip"
    archive(path)
    blob = bytearray(path.read_bytes())
    central = blob.index(b"PK\x01\x02")
    if kind == "encrypted":
        blob[6] |= 1
        blob[central + 8] |= 1
    else:
        blob[central + 16] ^= 1
    path.write_bytes(blob)
    refused(native("import", "bank.zip"), cause)


if os.name != "nt":
    def test_nonunicode_arguments_and_dsn_refused(native):
        refused(native("import", b"\xff.zip"), "Unicode")
        refused(native("export", extra={"PSEUDOLIFE_MCP_DATABASE_URL": b"\xff"}), "Unicode")

    def test_deleted_cwd_requires_explicit_destination(native, tmp_path):
        (tmp_path / "gone").mkdir()
        bootstrap = "import os,sys; os.chdir('gone'); os.rmdir('../gone'); os.execv(sys.argv[1],sys.argv[1:])"
        refused(native("export", bootstrap=bootstrap), "current directory")
        (tmp_path / "gone").mkdir()
        refused(native("export", "--out", str(tmp_path / "bank.zip"), bootstrap=bootstrap), "PostgreSQL connection failed")


def test_nonempty_refusal_preserves_missing_schema_stamps(native, tmp_path, bank):
    with _bank.connect(bank) as conn:
        conn.execute("DELETE FROM meta WHERE key='schema_version'")
        conn.execute("INSERT INTO episodes (id,title,started_at) VALUES ('kept','existing',1)")
        conn.commit()
    before = _bank.dump(bank)
    archive(tmp_path / "bank.zip")
    refused(native("import", "bank.zip", dsn=_bank.url(bank)), "not empty")
    assert _bank.dump(bank) == before


def test_import_schema_lock_failure_rolls_back_and_near_miss(native, tmp_path, bank):
    archive(tmp_path / "bank.zip")
    before = _bank.dump(bank)
    with _bank.connect(bank) as conn:
        conn.execute("LOCK TABLE episodes IN ACCESS SHARE MODE")
        code, stdout, stderr = native("import", "bank.zip", "--force", dsn=_bank.url(bank))
        assert code == 1 and not stdout and "archive rows were not committed" in stderr
        conn.rollback()
    assert _bank.dump(bank) == before
    assert native("import", "bank.zip", dsn=_bank.url(bank))[0] == 0
