"""Native audit refusals and complete file publication on disposable banks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from .rows import _bank


SECRET = "synthetic-audit-body-must-not-escape"


class Bank:
    def __init__(self, name, url):
        self.name, self.url = name, url

    def __getitem__(self, index):
        return (self.name, self.url)[index]

    def __repr__(self):
        return "<disposable audit bank>"


@pytest.fixture
def candidate():
    value = os.environ.get("PSEUDOLIFE_RUST_BINARY") or os.environ.get("R1_PAIR_CANDIDATE")
    if not value:
        pytest.skip("PSEUDOLIFE_RUST_BINARY is required")
    return str(Path(value).resolve())


def invoke(candidate, home, *args, dsn=None, extra=None, closed_stdout=False, umask=-1):
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PSEUDOLIFE_", "PG"))}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home),
               LOCALAPPDATA=str(home), XDG_CONFIG_HOME=str(home),
               PATH=str(home / "no-programs"), BROWSER="missing-browser",
               PSEUDOLIFE_MCP_URL="http://127.0.0.1:1",
               PSEUDOLIFE_MCP_CONFIG=str(home / "absent.yaml"),
               PSEUDOLIFE_MCP_DATA_DIR=str(home / "absent"))
    if dsn is not None:
        env["PSEUDOLIFE_MCP_DATABASE_URL"] = dsn
    env.update(extra or {})
    arguments = [a if isinstance(a, bytes) else str(a) for a in args]
    if closed_stdout:
        read, write = os.pipe()
        os.close(read)
        try:
            return subprocess.run([candidate, "board-audit", *arguments], env=env,
                                  stdout=write, stderr=subprocess.PIPE, timeout=30)
        finally:
            os.close(write)
    return subprocess.run([candidate, "board-audit", *arguments], env=env,
                          capture_output=True, timeout=30, umask=umask)


@pytest.fixture
def bank():
    name = _bank.name("pl_cf_audit_native")
    url = _bank.create(name, schema=False)
    try:
        with _bank.connect(name) as conn:
            conn.execute("""CREATE TABLE coordination_events (
                seq bigint PRIMARY KEY, event text, actor text, principal text,
                agent_id text, recipient_agent_id text, project text, task text,
                message_id text, payload text, created_at double precision,
                hlc text, prev_hash text, hash text, body text, body_salt text);
                CREATE TABLE coordination_agents (agent_id text);
                CREATE TABLE coordination_messages (
                message_id text, text text, fingerprint text, sender_agent_id text,
                recipient_agent_id text, project text, task text, expires_at double precision)
                """)
            row = event()
            columns = list(row)
            conn.execute(f"INSERT INTO coordination_events ({','.join(columns)}) "
                         f"VALUES ({','.join(['%s'] * len(columns))})", list(row.values()))
            conn.commit()
        yield Bank(name, url)
    finally:
        _bank.teardown(name)


def event(**changes):
    row = dict(seq=1, event="register", actor="agent", principal="example",
               agent_id="example-agent", recipient_agent_id=None, project="p",
               task="t", message_id=None, payload='{"safe":"雪"}', created_at=1700000000.0,
               hlc="", prev_hash="0" * 64, hash="", body=None, body_salt=None)
    row.update(changes)
    row["payload"] = json.dumps(json.loads(row["payload"]), ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"))
    fields = ["seq", "event", "actor", "principal", "agent_id", "recipient_agent_id",
              "project", "task", "message_id", "created_at", "hlc", "payload"]
    material = ["pseudolife-coordination-audit-v1", *(row[k] for k in fields)]
    row["hash"] = hashlib.sha256((row["prev_hash"] + json.dumps(
        material, ensure_ascii=False, separators=(",", ":"))).encode()).hexdigest()
    return row


def assert_refused(result, parameter):
    assert result.returncode == 2
    assert not result.stdout
    assert b"defer" not in result.stderr.lower()
    assert parameter.encode() in result.stderr
    assert SECRET.encode() not in (result.stdout or b"") + result.stderr


def test_agent_refusal_leaves_no_archive(candidate, bank, tmp_path):
    out = tmp_path / "out.jsonl"
    result = invoke(candidate, tmp_path, "export", "--agent", "ffffffff", "--out", out,
                    dsn=bank[1])
    assert result.returncode == 2
    assert not out.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    near = invoke(candidate, tmp_path, "export", "--agent", "example-agent", "--out", out,
                  dsn=bank[1])
    assert near.returncode == 0
    assert len(out.read_bytes().splitlines()) == 1


def test_successful_archive_bytes_match_baseline(candidate, bank, tmp_path):
    body, salt = "synthetic committed body 雪", "01" * 16
    commitment = hashlib.sha256(bytes.fromhex(salt) + body.encode()).hexdigest()
    sent = event(seq=2, event="send", message_id="message", body=body, body_salt=salt,
                 prev_hash=event()["hash"], payload=json.dumps({"text_commitment": commitment}))
    with _bank.connect(bank[0]) as conn:
        columns = list(sent)
        conn.execute(f"INSERT INTO coordination_events ({','.join(columns)}) "
                     f"VALUES ({','.join(['%s'] * len(columns))})", list(sent.values()))
        conn.commit()
    out = tmp_path / "out.jsonl"
    result = invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1])
    assert result.returncode == 0
    # The exporter orders payload before created_at, as the stored column order does.
    expected = b""
    for row in [event(), sent]:
        row["payload"] = json.loads(row["payload"])
        expected += (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    assert out.read_bytes() == expected
    baseline = os.environ.get("PSEUDOLIFE_AUDIT_BASELINE")
    if baseline:
        old = tmp_path / "baseline.jsonl"
        result = invoke(baseline, tmp_path, "export", "--out", old, dsn=bank[1])
        assert result.returncode == 0
        assert out.read_bytes() == old.read_bytes()


@pytest.mark.parametrize("mode", ["verify", "export", "redact", "stats"])
def test_bad_database_setting_has_actionable_secret_safe_refusal(candidate, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args, dsn=f"invalid:{SECRET}")
    assert_refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")


@pytest.mark.parametrize("mode", ["verify", "export", "redact", "stats"])
def test_nonunicode_database_setting_refuses_without_fallback(candidate, bank, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args, dsn=SECRET + "\udcff")
    assert_refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")
    near = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert near.returncode == (1 if mode == "redact" else 0)


@pytest.mark.parametrize("field,value,parameter", [
    ("seq", 2**63, "seq"),
    ("created_at", 1e309, "created_at"),
    ("principal", "surrogate\ud800" + SECRET, "principal"),
    ("body", "surrogate\ud800" + SECRET, "body"),
])
@pytest.mark.parametrize("mode", ["verify", "stats"])
def test_archive_value_refusals_do_not_disclose_values(candidate, tmp_path, field, value, parameter, mode):
    row = event()
    row["payload"] = json.loads(row["payload"])
    row[field] = value
    archive = tmp_path / "input.jsonl"
    archive.write_text(json.dumps(row), encoding="utf-8")
    result = invoke(candidate, tmp_path, mode, "--input", archive)
    assert_refused(result, parameter)
    row = event()
    row["payload"] = json.loads(row["payload"])
    archive.write_text(json.dumps(row), encoding="utf-8")
    near = invoke(candidate, tmp_path, mode, "--input", archive)
    assert near.returncode == 0


@pytest.mark.parametrize("reason", ["zero\u200bwidth", "private\ue000", "unassigned\u0378"])
def test_unclassified_reason_is_refused_without_redaction(candidate, bank, tmp_path, reason):
    with _bank.connect(bank[0]) as conn:
        before = conn.execute("SELECT * FROM coordination_events ORDER BY seq").fetchall()
    result = invoke(candidate, tmp_path, "redact", "--message-id", "example",
                    "--reason", reason + SECRET, dsn=bank[1])
    assert_refused(result, "--reason")
    with _bank.connect(bank[0]) as conn:
        assert conn.execute("SELECT * FROM coordination_events ORDER BY seq").fetchall() == before
    near = invoke(candidate, tmp_path, "redact", "--message-id", "example",
                  "--reason", "wrong paste 雪", dsn=bank[1])
    assert near.returncode == 1
    assert b"message_not_found" in near.stdout


def test_query_failure_names_operation_without_database_detail(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("ALTER TABLE coordination_events DROP COLUMN hash")
        conn.commit()
    result = invoke(candidate, tmp_path, "verify", dsn=bank[1])
    assert_refused(result, "audit")
    with _bank.connect(bank[0]) as conn:
        conn.execute("ALTER TABLE coordination_events ADD COLUMN hash text")
        conn.execute("UPDATE coordination_events SET hash=%s", (event()["hash"],))
        conn.commit()
    assert invoke(candidate, tmp_path, "verify", dsn=bank[1]).returncode == 0


@pytest.mark.parametrize("mode", ["verify", "export", "redact"])
def test_connection_failure_has_no_deferral(candidate, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args,
                    dsn=f"postgresql://example:{SECRET}@127.0.0.1:1/pl_cf_absent")
    assert_refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")


def test_connection_options_are_native(candidate, bank, tmp_path):
    result = invoke(candidate, tmp_path, "verify",
                    dsn=bank[1] + "?application_name=audit&connect_timeout=5")
    assert result.returncode == 0
    assert json.loads(result.stdout)["ok"] is True


@pytest.mark.parametrize("mode", ["verify", "export", "stats", "redact"])
def test_ambient_control_is_named_and_not_echoed(candidate, bank, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1], extra={"PGOPTIONS": SECRET})
    assert_refused(result, "PGOPTIONS")
    near = invoke(candidate, tmp_path, "verify", dsn=bank[1])
    assert near.returncode == 0


@pytest.mark.parametrize("prefix", ["PGſSL", "PGTLſ"])
def test_unicode_folded_ambient_name_never_echoes_key(candidate, bank, tmp_path, prefix):
    result = invoke(candidate, tmp_path, "verify", dsn=bank[1],
                    extra={prefix + SECRET: SECRET})
    if os.name == "nt":
        assert_refused(result, prefix.upper() + "*")
    else:
        # These names are not PostgreSQL controls on a case-sensitive OS.
        assert result.returncode == 0
    assert SECRET.encode() not in result.stdout + result.stderr
    near = invoke(candidate, tmp_path, "verify", dsn=bank[1],
                  extra={prefix[:-1] + "X": SECRET})
    assert near.returncode == 0


def test_stats_surrogate_payload_is_refused_without_echo(candidate, tmp_path):
    row = event()
    row["payload"] = {"metadata": "\ud800" + SECRET}
    archive = tmp_path / "input.jsonl"
    archive.write_text(json.dumps(row), encoding="utf-8")
    assert_refused(invoke(candidate, tmp_path, "stats", "--input", archive), "payload")
    row["payload"] = {"metadata": "雪"}
    archive.write_text(json.dumps(row), encoding="utf-8")
    assert invoke(candidate, tmp_path, "stats", "--input", archive).returncode == 0


@pytest.mark.parametrize("mode", ["verify", "export"])
def test_nonfinite_bank_timestamp_is_refused(candidate, bank, tmp_path, mode):
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET created_at='Infinity'::float8, body=%s", (SECRET,))
        conn.commit()
    args = [mode]
    out = tmp_path / "out.jsonl"
    if mode == "export":
        args += ["--out", out]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert_refused(result, "created_at")
    assert not out.exists()
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET created_at=%s, body=NULL", (event()["created_at"],))
        conn.commit()
    assert invoke(candidate, tmp_path, *args, dsn=bank[1]).returncode == 0


def test_bank_payload_surrogate_stats_refusal(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET payload=%s", (json.dumps({"value": "\ud800" + SECRET}),))
        conn.commit()
    assert_refused(invoke(candidate, tmp_path, "stats", "--since", "0", dsn=bank[1]), "payload")
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET payload=%s", (event()["payload"],))
        conn.commit()
    assert invoke(candidate, tmp_path, "stats", "--since", "0", dsn=bank[1]).returncode == 0


def test_sequence_exhaustion_rolls_back_every_redaction_change(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET seq=%s", (2**63 - 1,))
        conn.execute("INSERT INTO coordination_messages VALUES (%s,%s,'original','sender','recipient','p','t',1e12)",
                     ("example", SECRET))
        conn.commit()
        before = conn.execute("SELECT * FROM coordination_messages").fetchall()
    result = invoke(candidate, tmp_path, "redact", "--message-id", "example", "--reason", "wrong paste", dsn=bank[1])
    assert_refused(result, "seq")
    with _bank.connect(bank[0]) as conn:
        assert conn.execute("SELECT * FROM coordination_messages").fetchall() == before
        assert conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0] == 1
        conn.execute("UPDATE coordination_events SET seq=1")
        conn.commit()
    near = invoke(candidate, tmp_path, "redact", "--message-id", "example", "--reason", "wrong paste", dsn=bank[1])
    assert near.returncode == 0
    assert SECRET.encode() not in near.stdout + near.stderr


@pytest.mark.parametrize("operation", ["verify", "export", "redact"])
def test_database_column_decode_refusal_is_safe(candidate, bank, tmp_path, operation):
    with _bank.connect(bank[0]) as conn:
        conn.execute("ALTER TABLE coordination_events ALTER COLUMN seq TYPE text USING seq::text")
        conn.commit()
    args = [operation]
    if operation == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    assert_refused(invoke(candidate, tmp_path, *args, dsn=bank[1]), "audit")
    with _bank.connect(bank[0]) as conn:
        conn.execute("ALTER TABLE coordination_events ALTER COLUMN seq TYPE bigint USING seq::bigint")
        conn.commit()
    near = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert near.returncode == (1 if operation == "redact" else 0)


def test_agent_lookup_failure_leaves_no_archive(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("DROP TABLE coordination_agents")
        conn.commit()
    out = tmp_path / "out.jsonl"
    args = ["export", "--agent", "ffffffff", "--out", out]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert_refused(result, "--agent")
    assert b"schema" in result.stderr
    assert list(tmp_path.iterdir()) == []
    with _bank.connect(bank[0]) as conn:
        conn.execute("CREATE TABLE coordination_agents (agent_id text)")
        conn.commit()
    near = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert near.returncode == 2
    assert b"no agent id starts with" in near.stderr


@pytest.mark.parametrize("name", ["postgresql.crt", "postgresql.key", "root.crl"])
def test_implicit_tls_file_refusal_names_the_file_and_fix(candidate, bank, tmp_path, name):
    directory = tmp_path / ("postgresql" if os.name == "nt" else ".postgresql")
    directory.mkdir()
    implicit = directory / name
    implicit.write_text(SECRET)
    result = invoke(candidate, tmp_path, "verify", dsn=bank[1])
    assert_refused(result, name)
    assert b"file" in result.stderr and b"move" in result.stderr
    implicit.unlink()
    assert invoke(candidate, tmp_path, "verify", dsn=bank[1]).returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask")
@pytest.mark.parametrize("failure", [False, True])
def test_write_only_umask_publishes_or_removes_staging(candidate, bank, tmp_path, failure):
    out = tmp_path / "out.jsonl"
    args = ["export", "--out", out]
    if failure:
        args += ["--agent", "ffffffff"]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1], umask=0o400)
    assert result.returncode == (2 if failure else 0)
    assert not list(tmp_path.glob(".board-audit-*.tmp"))
    assert out.exists() != failure
    if not failure:
        assert out.stat().st_mode & 0o777 == 0o200
        out.chmod(0o600)
        assert len(out.read_bytes().splitlines()) == 1
    near = tmp_path / "near.jsonl"
    assert invoke(candidate, tmp_path, "export", "--out", near, dsn=bank[1], umask=0o077).returncode == 0


def test_staging_error_names_out_and_leaves_no_file(candidate, bank, tmp_path):
    missing = tmp_path / "missing"
    out = missing / "out.jsonl"
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET payload=%s", ("invalid " + SECRET,))
        conn.commit()
    result = invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1])
    assert_refused(result, "--out")
    assert b"parent directory does not exist" in result.stderr
    assert b"create the parent directory" in result.stderr
    assert not out.exists()
    with _bank.connect(bank[0]) as conn:
        conn.execute("UPDATE coordination_events SET payload=%s", (event()["payload"],))
        conn.commit()
    missing.mkdir()
    assert invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1]).returncode == 0


def test_unwritable_export_parent_has_permission_fix(candidate, bank, tmp_path):
    parent = tmp_path / "private"
    parent.mkdir()
    out = parent / "out.jsonl"
    if os.name == "nt":
        import csv
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  check=True, capture_output=True, text=True)
        sid = next(csv.reader(identity.stdout.splitlines()))[1]
        deny = subprocess.run(["icacls", str(parent), "/deny", f"*{sid}:(WD,AD)"],
                              capture_output=True)
        assert deny.returncode == 0
    else:
        parent.chmod(0o500)
    try:
        result = invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1])
        assert_refused(result, "--out")
        assert b"permission denied" in result.stderr
        assert b"choose a destination writable by your user" in result.stderr
        assert not out.exists()
        assert not list(parent.iterdir())
    finally:
        if os.name == "nt":
            reset = subprocess.run(["icacls", str(parent), "/remove:d", f"*{sid}"],
                                   capture_output=True)
            assert reset.returncode == 0
        else:
            parent.chmod(0o700)
    assert invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1]).returncode == 0


def test_input_verify_closed_stdout_has_safe_refusal(candidate, tmp_path):
    row = event()
    row["payload"] = json.loads(row["payload"])
    archive = tmp_path / "input.jsonl"
    archive.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = invoke(candidate, tmp_path, "verify", "--input", archive, closed_stdout=True)
    assert_refused(result, "stdout")
    assert b"writable destination" in result.stderr
    near = invoke(candidate, tmp_path, "verify", "--input", archive)
    assert near.returncode == 0
    assert json.loads(near.stdout)["ok"] is True


@pytest.mark.parametrize("mode", ["verify", "redact", "export"])
def test_closed_stdout_refuses_without_claiming_completion(candidate, bank, tmp_path, mode):
    args = [mode]
    if mode == "redact":
        args += ["--message-id", "example", "--reason", "wrong paste"]
    result = invoke(candidate, tmp_path, *args, dsn=bank[1], closed_stdout=True)
    assert result.returncode == 2
    assert b"stdout" in result.stderr
    assert b"defer" not in result.stderr
    assert SECRET.encode() not in result.stderr
    if mode == "export":
        assert b"incomplete" in result.stderr
    near = invoke(candidate, tmp_path, *args, dsn=bank[1])
    assert near.returncode == (1 if mode == "redact" else 0)


@pytest.mark.parametrize("option", ["options", "sslmode", "connect_timeout"])
def test_invalid_connection_option_is_secret_safe(candidate, bank, tmp_path, option):
    result = invoke(candidate, tmp_path, "verify", dsn=bank[1] + f"?{option}={SECRET}")
    assert_refused(result, "PSEUDOLIFE_MCP_DATABASE_URL")
    near = invoke(candidate, tmp_path, "verify", dsn=bank[1] + "?sslmode=disable&connect_timeout=5")
    assert near.returncode == 0


@pytest.mark.parametrize("timestamp", [1e36, -1e36])
def test_retention_timestamp_outside_verified_range_is_refused(candidate, tmp_path, timestamp):
    row = event(seq=2, event="audit_prune", actor="daemon", created_at=timestamp,
                payload=json.dumps(dict(through_seq=1, through_hash="0" * 64,
                                        retention_days=1, cutoff=0), separators=(",", ":")))
    row["payload"] = json.loads(row["payload"])
    archive = tmp_path / "input.jsonl"
    archive.write_text(json.dumps(row), encoding="utf-8")
    assert_refused(invoke(candidate, tmp_path, "verify", "--input", archive), "created_at")
    now = 1700000000.0
    cutoff = int((now - 86400) // 86400 * 86400)
    row = event(seq=2, event="audit_prune", actor="daemon", created_at=now,
                payload=json.dumps(dict(through_seq=1, through_hash="0" * 64,
                                        retention_days=1, cutoff=cutoff), separators=(",", ":")))
    row["payload"] = json.loads(row["payload"])
    archive.write_text(json.dumps(row), encoding="utf-8")
    assert invoke(candidate, tmp_path, "verify", "--input", archive).returncode == 0


def test_nonunicode_argument_is_refused_without_echo(candidate, tmp_path):
    argument = "\ud800" if os.name == "nt" else b"\xff"
    assert_refused(invoke(candidate, tmp_path, "verify", argument), "arguments")
    assert invoke(candidate, tmp_path, "verify", "--help").returncode == 0


def test_first_retained_timestamp_outside_comparison_range_is_refused(candidate, tmp_path):
    archive = tmp_path / "input.jsonl"
    now = 1700000000.0
    for timestamp, refused in [(1e32, True), (now, False)]:
        first = event(seq=2, created_at=timestamp)
        prune = event(seq=3, event="audit_prune", actor="daemon", created_at=now,
                      prev_hash=first["hash"], payload=json.dumps(dict(
                          through_seq=1, through_hash="0" * 64, retention_days=1,
                          cutoff=int((now - 86400) // 86400 * 86400))))
        for row in [first, prune]:
            row["payload"] = json.loads(row["payload"])
        archive.write_text(json.dumps(first) + "\n" + json.dumps(prune), encoding="utf-8")
        result = invoke(candidate, tmp_path, "verify", "--input", archive)
        if refused:
            assert_refused(result, "created_at")
        else:
            assert result.returncode == 0


def test_late_payload_failure_keeps_stdout_prefix_but_never_publishes_file(candidate, bank, tmp_path):
    with _bank.connect(bank[0]) as conn:
        conn.execute("INSERT INTO coordination_events SELECT 2,event,actor,principal,agent_id,"
                     "recipient_agent_id,project,task,message_id,%s,created_at,hlc,prev_hash,hash,body,body_salt "
                     "FROM coordination_events WHERE seq=1", ('not-json-' + SECRET,))
        conn.commit()
    out = tmp_path / "out.jsonl"
    result = invoke(candidate, tmp_path, "export", "--out", out, dsn=bank[1])
    assert_refused(result, "payload")
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
    streamed = invoke(candidate, tmp_path, "export", dsn=bank[1])
    assert streamed.returncode == 2
    assert len(streamed.stdout.splitlines()) == 1
    assert b"incomplete" in streamed.stderr
    assert SECRET.encode() not in streamed.stdout + streamed.stderr


def test_existing_and_locked_destinations_are_never_replaced(candidate, tmp_path):
    out = tmp_path / "out.jsonl"
    out.write_bytes(b"existing archive\n")
    handle = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        handle = create(str(out), 0x80000000, 0, None, 3, 0x80, None)
        assert handle != wintypes.HANDLE(-1).value
    try:
        result = invoke(candidate, tmp_path, "export", "--out", out, dsn="not-a-dsn")
        assert result.returncode == 2
        assert b"exists; export never replaces a file" in result.stderr
        assert b"choose an unused --out path" in result.stderr
    finally:
        if handle is not None:
            close = ctypes.WinDLL("kernel32").CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            assert close(handle)
    assert out.read_bytes() == b"existing archive\n"


@pytest.mark.parametrize("code,racing_destination", [(0, False), (23, False), (0, True)])
def test_container_export_is_invisible_until_success(candidate, tmp_path, code, racing_destination):
    import shlex
    import sys
    import threading
    import time

    script = tmp_path / "docker.py"
    script.write_text('''import os, sys, time
from pathlib import Path
home = Path(os.environ['HOME'])
if sys.argv[1] == 'inspect':
    print('true' if sys.argv[3] == '{{.State.Running}}' else 'rust')
    raise SystemExit(0)
sys.stdout.buffer.write(b'{"container":"fixture"}\\n')
sys.stdout.buffer.flush()
(home / 'ready').touch()
until = time.monotonic() + 15
while not (home / 'release').exists() and time.monotonic() < until:
    time.sleep(.01)
raise SystemExit(int(os.environ['FIXTURE_EXIT']))
''', encoding="utf-8")
    wrapper = tmp_path / ("docker.cmd" if os.name == "nt" else "docker.sh")
    wrapper.write_text((f'@echo off\n"{sys.executable}" "{script}" %*\nexit /b %errorlevel%\n'
                        if os.name == "nt" else
                        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'),
                       encoding="utf-8")
    wrapper.chmod(0o700)
    result = []
    out = tmp_path / "out.jsonl"
    worker = threading.Thread(target=lambda: result.append(invoke(
        candidate, tmp_path, "export", "--out", out,
        extra={"PSEUDOLIFE_DOCKER": str(wrapper), "FIXTURE_EXIT": str(code)})))
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "ready").exists() and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        ready = (tmp_path / "ready").exists()
        premature = out.exists()
        if racing_destination and not premature:
            out.write_bytes(b"racing destination\n")
    finally:
        (tmp_path / "release").touch()
        worker.join(timeout=35)
    assert ready and result
    assert not premature, "container output was published before its exit status"
    assert result[0].returncode == (2 if racing_destination else code)
    assert out.exists() == (code == 0)
    if racing_destination:
        assert out.read_bytes() == b"racing destination\n"
        assert b"publication" in result[0].stderr
        assert b"destination already exists" in result[0].stderr
        assert b"choose an unused --out path" in result[0].stderr
    elif code == 0:
        assert out.read_bytes() == b'{"container":"fixture"}\n'
    assert not list(tmp_path.glob(".board-audit-*.tmp"))
