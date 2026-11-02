"""Native proof admission checks and explicit live actual-adapter parity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest


def _candidate() -> Path:
    value = os.environ.get("PSEUDOLIFE_PROOF_CANDIDATE") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if not value:
        pytest.skip("set PSEUDOLIFE_PROOF_CANDIDATE to the native CLI")
    return Path(value).resolve()


def _run(tmp_path: Path, *arguments: str, dsn: str | None = None, models: Path | None = None):
    source = _candidate()
    candidate = tmp_path / source.name
    shutil.copy2(source, candidate)
    env = {key: value for key, value in os.environ.items()
           if key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PATH=str(tmp_path),
               COLUMNS="80")
    if dsn is not None:
        env["PSEUDOLIFE_TEST_DATABASE_URL"] = dsn
    if models is not None:
        env["PSEUDOLIFE_DAEMON_ONNX_DIR"] = str(models)
    return subprocess.run([str(candidate), "doctor", "--disposable-proof", *arguments],
                          env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)


def test_missing_fixture_dsn_never_starts_daemon(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "ExplicitDisposableDatabaseRequired"


def test_client_only_install_refuses_before_connecting(tmp_path):
    result = _run(tmp_path, dsn="postgresql://fixture@127.0.0.1:1/r2_proof_admission")
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["error"] == "NativeDaemonRequired"
    assert "docker exec" in report["recovery"]
    assert "--disposable-proof" in report["recovery"]
    assert not result.stderr


@pytest.mark.skipif(os.environ.get("PSEUDOLIFE_DOCTOR_LIVE_PROOF") != "1",
                    reason="explicit disposable server and provisioned native models required")
@pytest.mark.parametrize("authentication", ["inline", "environment", pytest.param(
    "password-file", marks=pytest.mark.skipif(os.name == "nt", reason="Windows password files use the real Shell32 profile; isolated resolver coverage is native"))])
def test_actual_adapter_proof_matches_python(tmp_path, monkeypatch, capsys, authentication):
    """Compare actual adapter proofs; only bank namespace and transport differ.

    The oracle's bank context is substituted to honor the test host's r2_*
    namespace. Its schema, authenticated API, adapter and proof are unchanged.
    Native drives its sibling daemon over loopback; Python drives its ASGI app.
    """
    from contextlib import contextmanager
    import re
    import sys
    import uuid
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from pseudolife_memory import coordination_proof, doctor_cli
    from pseudolife_memory.storage.schema import refuse_production_database

    dsn = os.environ["PSEUDOLIFE_TEST_DATABASE_URL"]
    selected = conninfo_to_dict(dsn)["dbname"]
    assert selected.startswith("r2_"), "live proof needs the stream's disposable namespace"
    refuse_production_database(selected)
    admin_dsn = make_conninfo(dsn, dbname="postgres", connect_timeout=5)

    def banks():
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            return {r[0] for r in connection.execute(
                "SELECT datname FROM pg_database WHERE starts_with(datname, 'r2_')")}

    @contextmanager
    def own_bank(explicit_dsn):
        assert explicit_dsn == dsn
        name = "r2_doctor_oracle_" + uuid.uuid4().hex
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute("SET statement_timeout = '5s'")
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(dsn, dbname=name, connect_timeout=5)
        finally:
            with psycopg.connect(admin_dsn, autocommit=True) as connection:
                connection.execute("SET statement_timeout = '300s'")
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

    before = banks()
    monkeypatch.setattr(coordination_proof, "_disposable_bank", own_bank)
    monkeypatch.setattr(sys, "argv", ["cli.py", "doctor", "--disposable-proof"])
    with pytest.raises(SystemExit) as exited:
        doctor_cli.run_doctor()
    assert exited.value.code == 0
    captured = capsys.readouterr()
    oracle = json.loads(captured.out)
    assert banks() == before

    candidate = Path(os.environ["PSEUDOLIFE_PROOF_CANDIDATE"]).resolve()
    env = {key: value for key, value in os.environ.items() if key in (
        "SYSTEMROOT", "WINDIR", "PATH", "LD_LIBRARY_PATH", "ORT_DYLIB_PATH",
        "PSEUDOLIFE_DAEMON_MODELS_DIR", "PSEUDOLIFE_DAEMON_ONNX_DIR")}
    temp = tmp_path / "temp"
    temp.mkdir()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), TMPDIR=str(temp),
               TEMP=str(temp), TMP=str(temp), PSEUDOLIFE_TEST_DATABASE_URL=dsn)
    if authentication != "inline":
        fields = conninfo_to_dict(dsn)
        password = fields.pop("password")
        env["PSEUDOLIFE_TEST_DATABASE_URL"] = make_conninfo(**fields)
        if authentication == "environment":
            env["PGPASSWORD"] = password
        else:
            escape = lambda value: value.replace("\\", "\\\\").replace(":", "\\:")
            entry = [fields["host"], fields.get("port", "5432"), "*", fields["user"], password]
            passfile = tmp_path / ".pgpass"
            passfile.write_text(":".join(map(escape, entry)) + "\n", encoding="utf-8")
            passfile.chmod(0o600)
    result = subprocess.run([str(candidate), "doctor", "--disposable-proof"],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=720)
    assert result.returncode == 0, result.stdout
    assert not result.stderr
    native = json.loads(result.stdout)
    assert banks() == before
    assert not list(temp.iterdir()), "native fixture files must be removed"

    for report, instrument in ((oracle, "disposable_asgi_actual_adapter"),
                               (native, "disposable_http_actual_adapter")):
        assert report["ok"] is True and report["fixture_removed"] is True
        assert report["instrument"] == instrument
        assert re.fullmatch(r"coordination-proof-[0-9a-f]{32}", report["tag"])
        report["tag"] = "<proof-tag>"
        report["instrument"] = "<actual-adapter-transport>"
        assert [stage["path"] for stage in report["stages"]] == ["hint", "ring"]
        for stage in report["stages"]:
            assert re.fullmatch(r"[0-9a-f]{32}", stage["message_id"])
            stage["message_id"] = "<message-id>"
            for key in ("enqueued", "signal_observed", "received", "harness_ack"):
                assert stage[key] is True
    assert native == oracle


def test_proof_never_uses_saved_agent_state(tmp_path):
    result = _run(tmp_path, "--agent-state", "saved-instance.json")
    assert result.returncode == 2
    assert "--disposable-proof cannot use a saved --agent-state" in result.stderr
    assert not result.stdout


def test_missing_models_refuses_before_connecting(tmp_path):
    # Presence is enough for discovery; admission must not execute this file.
    daemon = tmp_path / ("pseudolife-daemon.exe" if os.name == "nt" else "pseudolife-daemon")
    daemon.write_bytes(b"not a daemon")
    result = _run(tmp_path, dsn="postgresql://fixture@127.0.0.1:1/r2_proof_admission")
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["error"] == "ProvisionedModelsRequired"
    assert "pseudolife-daemon provision models" in report["recovery"]
    assert not result.stderr


def _admission_models(root):
    for name in ("onnx/model.onnx", "onnx/model.onnx_data", "tokenizer.json", "config.json",
                 "modules.json", "1_Pooling/config.json", "tokenizer_config.json",
                 "config_sentence_transformers.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture admission only")
    return root


@pytest.mark.parametrize("fault", ["create-timeout", "drop-timeout", pytest.param(
    "create-interrupt", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM contract"))])
def test_lost_database_replies_are_bounded_and_cleanup_is_reported(tmp_path, fault):
    """A loopback PostgreSQL wire fixture loses replies after accepting SQL."""
    import signal
    import socketserver
    import struct
    import threading

    source = _candidate()
    reached = threading.Event()
    release = threading.Event()
    queries = []

    class Handler(socketserver.BaseRequestHandler):
        def exact(self, count):
            result = b""
            while len(result) < count:
                part = self.request.recv(count - len(result))
                if not part:
                    raise EOFError
                result += part
            return result

        def frame(self, tag, body):
            self.request.sendall(tag + struct.pack("!I", len(body) + 4) + body)

        def handle(self):
            try:
                size = struct.unpack("!I", self.exact(4))[0]
                self.exact(size - 4)
                self.frame(b"R", struct.pack("!I", 0))
                self.frame(b"Z", b"I")
                while True:
                    kind = self.exact(1)
                    size = struct.unpack("!I", self.exact(4))[0]
                    body = self.exact(size - 4)
                    if kind != b"Q":
                        return
                    query = body[:-1].decode()
                    queries.append(query)
                    stalled = (query.startswith("CREATE DATABASE") and fault.startswith("create")) or (
                        query.startswith("DROP DATABASE") and fault == "drop-timeout")
                    if stalled:
                        reached.set()
                        release.wait(90)
                        return
                    self.frame(b"C", b"SET\0")
                    self.frame(b"Z", b"I")
            except (EOFError, ConnectionError):
                return

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    candidate = tmp_path / ("pseudolife-stdio.exe" if os.name == "nt" else "pseudolife-stdio")
    shutil.copy2(source, candidate)
    daemon = tmp_path / ("pseudolife-daemon.exe" if os.name == "nt" else "pseudolife-daemon")
    daemon.write_bytes(b"intentionally not executable")
    models = _admission_models(tmp_path / "models")
    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        env = {key: value for key, value in os.environ.items() if key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
        env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PATH=str(tmp_path),
                   PSEUDOLIFE_DAEMON_ONNX_DIR=str(models),
                   PSEUDOLIFE_TEST_DATABASE_URL=f"host=127.0.0.1 port={server.server_address[1]} user=fixture password=synthetic dbname=r2_blackhole sslmode=disable")
        child = subprocess.Popen([str(candidate), "doctor", "--disposable-proof"], env=env,
                                 cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert reached.wait(30), "proof never reached the accepted database statement"
            if fault == "create-interrupt":
                child.send_signal(signal.SIGTERM)
            stdout, stderr = child.communicate(timeout=45)
            assert child.returncode == 1
            report = json.loads(stdout)
            expected = {"create-interrupt": "KeyboardInterrupt", "create-timeout": "TimeoutError",
                        "drop-timeout": "FixtureCleanupError"}[fault]
            assert report["error"] == expected
            assert not stderr
            assert any(query.startswith('DROP DATABASE IF EXISTS "r2_blackhole_proof_') for query in queries)
            if fault == "drop-timeout":
                assert report["fixture_removed"] is False
                assert report["fixture_bank"].startswith("r2_blackhole_proof_")
                assert "DROP DATABASE" in report["recovery"]
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
            server.shutdown()
            thread.join(5)


@pytest.mark.parametrize("dsn", ["host='unterminated", "postgresql://fixture@127.0.0.1:1/",
                                 "postgresql://fixture@127.0.0.1:1/pseudolife_memory"])
def test_invalid_or_production_database_report_matches_python(tmp_path, monkeypatch, capsys, dsn):
    import sys
    from pseudolife_memory import doctor_cli

    daemon = tmp_path / ("pseudolife-daemon.exe" if os.name == "nt" else "pseudolife-daemon")
    daemon.write_bytes(b"must not execute")
    models = _admission_models(tmp_path / "models")
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", dsn)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    monkeypatch.setattr(sys, "argv", ["cli.py", "doctor", "--disposable-proof"])
    with pytest.raises(SystemExit) as exited:
        doctor_cli.run_doctor()
    captured = capsys.readouterr()
    native = _run(tmp_path, dsn=dsn, models=models)
    assert native.returncode == exited.value.code == 1
    assert native.stdout == captured.out
    assert native.stderr == captured.err == ""


@pytest.mark.skipif(os.name == "nt" or os.environ.get("PSEUDOLIFE_DOCTOR_LIVE_PROOF") != "1",
                    reason="POSIX signal and an explicit disposable server required")
def test_signal_during_daemon_start_removes_owned_process_files_and_bank(tmp_path):
    import signal
    import sys
    import time
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from pseudolife_memory.storage.schema import refuse_production_database

    dsn = os.environ["PSEUDOLIFE_TEST_DATABASE_URL"]
    selected = conninfo_to_dict(dsn)["dbname"]
    assert selected.startswith("r2_")
    refuse_production_database(selected)
    admin = make_conninfo(dsn, dbname="postgres", connect_timeout=5)

    def banks():
        with psycopg.connect(admin, autocommit=True) as connection:
            return {r[0] for r in connection.execute(
                "SELECT datname FROM pg_database WHERE starts_with(datname, 'r2_')")}

    candidate = tmp_path / "pseudolife-stdio"
    shutil.copy2(os.environ["PSEUDOLIFE_PROOF_CANDIDATE"], candidate)
    marker = tmp_path / "daemon-started"
    daemon = tmp_path / "pseudolife-daemon"
    daemon.write_text(f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
                      f"Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(120)\n")
    daemon.chmod(0o700)
    models = _admission_models(tmp_path / "models")
    temp = tmp_path / "temp"
    temp.mkdir()
    env = dict(HOME=str(tmp_path), PATH=str(tmp_path), TMPDIR=str(temp),
               PSEUDOLIFE_TEST_DATABASE_URL=dsn, PSEUDOLIFE_DAEMON_ONNX_DIR=str(models))
    before = banks()
    child = subprocess.Popen([str(candidate), "doctor", "--disposable-proof"],
                             env=env, cwd=tmp_path, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert child.poll() is None, "proof exited before its daemon started"
            assert time.monotonic() < deadline, "fixture daemon did not start"
            time.sleep(0.02)
        daemon_pid = int(marker.read_text())
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 1
        assert json.loads(stdout)["error"] == "KeyboardInterrupt"
        assert not stderr
        with pytest.raises(ProcessLookupError):
            os.kill(daemon_pid, 0)
        assert banks() == before
        assert not list(temp.iterdir())
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            child.communicate(timeout=30)
