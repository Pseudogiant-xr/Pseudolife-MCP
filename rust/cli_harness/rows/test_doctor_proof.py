"""Native proof admission checks and explicit live actual-adapter parity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest


def _candidate() -> Path:
    value = os.environ.get("PSEUDOLIFE_PROOF_CANDIDATE") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if not value:
        pytest.skip("set PSEUDOLIFE_PROOF_CANDIDATE to the native CLI")
    return Path(value).resolve()


def _run(tmp_path: Path, *arguments: str, dsn: str | None = None, models: Path | None = None,
         extra_env: dict | None = None):
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
    env.update(extra_env or {})
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
@pytest.mark.parametrize("authentication", ["inline", "environment", "embedding-only", "dead-proxy", pytest.param(
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
    if authentication == "dead-proxy":
        env.update({key: "http://127.0.0.1:1" for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
        env.update(NO_PROXY="", no_proxy="")
    if authentication == "embedding-only":
        configured = env.get("PSEUDOLIFE_DAEMON_ONNX_DIR")
        if configured:
            original = Path(configured)
        else:
            candidates = list(Path(env["PSEUDOLIFE_DAEMON_MODELS_DIR"]).glob("qwen3-msmarco-fp32-v1-*/embedding"))
            assert len(candidates) == 1
            original = candidates[0]
        embedding = tmp_path / "models" / "embedding"
        def link_or_copy(source, destination):
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)
        shutil.copytree(original, embedding, copy_function=link_or_copy)
        assert not embedding.with_name("reranker").exists()
        env["PSEUDOLIFE_DAEMON_ONNX_DIR"] = str(embedding)
    if authentication in ("environment", "password-file"):
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


@pytest.mark.parametrize("option", ["sslmode=require", "sslmode=verify-full", "sslrootcert=relative.pem",
                                 "sslcert=relative.pem", "sslkey=relative.pem", "sslcrl=relative.pem"])
def test_unsupported_daemon_tls_refuses_before_create(tmp_path, option):
    (tmp_path / ("pseudolife-daemon.exe" if os.name == "nt" else "pseudolife-daemon")).write_bytes(b"must not execute")
    result = _run(tmp_path, dsn=f"host=127.0.0.1 port=1 user=fixture dbname=r2_admission {option}",
                  models=_admission_models(tmp_path / "models"))
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["error"] == "NativeProofTlsUnsupported"
    assert "sslmode=disable" in report["recovery"]
    assert "nothing was changed" in report["recovery"]


def test_empty_configured_database_refuses_before_create(tmp_path):
    (tmp_path / ("pseudolife-daemon.exe" if os.name == "nt" else "pseudolife-daemon")).write_bytes(b"must not execute")
    result = _run(tmp_path, dsn="host=127.0.0.1 port=1 user=fixture dbname=r2_admission",
                  models=_admission_models(tmp_path / "models"),
                  extra_env={"PSEUDOLIFE_MCP_DATABASE_URL": "host=127.0.0.1 dbname=''"})
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "ProductionDatabaseError"


@pytest.mark.skipif(os.environ.get("PSEUDOLIFE_DOCTOR_LIVE_PROOF") != "1",
                   reason="explicit disposable server and installed native daemon required")
@pytest.mark.parametrize("damage", ["metadata", "checksum"])
def test_corrupt_model_names_repair_and_removes_owned_bank(tmp_path, damage):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    dsn = os.environ["PSEUDOLIFE_TEST_DATABASE_URL"]
    assert conninfo_to_dict(dsn)["dbname"].startswith("r2_")
    admin = make_conninfo(dsn, dbname="postgres", connect_timeout=5)
    def banks():
        with psycopg.connect(admin, autocommit=True) as connection:
            return {row[0] for row in connection.execute(
                "SELECT datname FROM pg_database WHERE starts_with(datname, 'r2_')")}
    before = banks()
    candidate = _candidate()
    env = {key: value for key, value in os.environ.items()
           if key in ("SYSTEMROOT", "WINDIR", "PATH", "LD_LIBRARY_PATH", "ORT_DYLIB_PATH")}
    models = _admission_models(tmp_path / "corrupt-model")
    if damage == "checksum":
        (models / "modules.json").write_text(json.dumps([
            {"path": "", "type": "sentence_transformers.models.Transformer"},
            {"path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
        ]))
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               TMPDIR=str(tmp_path), TEMP=str(tmp_path), TMP=str(tmp_path),
               PSEUDOLIFE_TEST_DATABASE_URL=make_conninfo(dsn, connect_timeout=5, application_name="proof fixture"),
               PSEUDOLIFE_DAEMON_ONNX_DIR=str(models))
    result = subprocess.run([str(candidate), "doctor", "--disposable-proof"],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["error"] == "ProvisionedModelIntegrityError"
    assert "pseudolife-daemon provision models" in report["recovery"]
    assert not result.stderr
    assert banks() == before


@pytest.mark.parametrize("fault", ["create-denied", "create-query-canceled", "create-timeout", "drop-lost-reply", "drop-checkpoint-delay", pytest.param(
    "create-interrupt", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM contract")), pytest.param(
    "drop-interrupt", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM contract"))])
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
                statement_timeout = None
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
                    if query.startswith("SET statement_timeout"):
                        statement_timeout = int(query.split("'")[1].removesuffix("s"))
                    if query.startswith("CREATE DATABASE") and fault in ("create-denied", "create-query-canceled"):
                        reached.set()
                        code = b"42501" if fault == "create-denied" else b"57014"
                        self.frame(b"E", b"SERROR\0C" + code + b"\0Msynthetic refusal\0\0")
                        self.frame(b"Z", b"I")
                        continue
                    dropping = query.startswith("DROP DATABASE")
                    if dropping and fault in ("drop-checkpoint-delay", "drop-interrupt"):
                        reached.set()
                        # A real DROP may wait 150–172 s for a checkpoint. This
                        # shorter witness still exceeds the old 5/10 s limits.
                        if release.wait(min(12, statement_timeout)):
                            return
                        if statement_timeout < 12:
                            self.frame(b"E", b"SERROR\0C57014\0Mstatement timeout\0\0")
                        else:
                            self.frame(b"C", b"DROP DATABASE\0")
                        self.frame(b"Z", b"I")
                        continue
                    stalled = (query.startswith("CREATE DATABASE") and fault.startswith("create")) or (
                        dropping and fault == "drop-lost-reply")
                    if stalled:
                        reached.set()
                        release.wait(0.1 if dropping else 90)
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
            if fault in ("create-interrupt", "drop-interrupt"):
                child.send_signal(signal.SIGTERM)
            stdout, stderr = child.communicate(timeout=45)
            assert child.returncode == 1
            report = json.loads(stdout)
            expected = {"create-denied": "InsufficientPrivilege", "create-query-canceled": "QueryCanceled",
                        "create-interrupt": "KeyboardInterrupt", "create-timeout": "TimeoutError",
                        "drop-lost-reply": "FixtureCleanupError",
                        "drop-checkpoint-delay": "NativeDaemonSpawnError",
                        "drop-interrupt": "NativeDaemonSpawnError"}[fault]
            assert report["error"] == expected
            if fault == "drop-interrupt":
                assert stderr == "Finishing disposable-bank cleanup; PostgreSQL may take up to 11 minutes.\n"
            else:
                assert not stderr
            drops = [query for query in queries if query.startswith('DROP DATABASE IF EXISTS "r2_blackhole_proof_')]
            if fault in ("create-denied", "create-query-canceled"):
                assert not drops, "server rejection proves CREATE did not succeed"
            else:
                assert drops
            if fault == "drop-lost-reply":
                assert sum(query.startswith("DROP DATABASE") for query in queries) == 2
                assert report["fixture_removed"] is False
                assert report["fixture_bank"].startswith("r2_blackhole_proof_")
                assert "DROP DATABASE" in report["recovery"]
            if fault.startswith("drop-"):
                assert "SET statement_timeout = '300s'" in queries
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
            server.shutdown()
            thread.join(5)


@pytest.mark.parametrize("dsn", ["host='unterminated", "host=127.0.0.1 dbname=r2_unknown foo=bar",
                                 "host=127.0.0.1 dbname=r2_unknown sslunknown=bar", "postgresql://fixture@127.0.0.1:1/",
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
@pytest.mark.parametrize("mode", ["signal", "exited-descendant"])
def test_signal_during_daemon_start_removes_owned_process_files_and_bank(tmp_path, mode):
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
    script = f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
    if mode == "exited-descendant":
        script += ("if os.fork():\n"
                   f" while not Path({str(marker)!r}).exists(): time.sleep(0.01)\n"
                   " os._exit(0)\n")
    script += f"Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(120)\n"
    daemon.write_text(script)
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
        if mode == "signal":
            child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 1
        expected = "KeyboardInterrupt" if mode == "signal" else "NativeDaemonStartupError"
        assert json.loads(stdout)["error"] == expected
        assert not stderr
        import psutil
        # An orphan can briefly remain as a zombie awaiting its reaper; it must
        # never keep executing or retain the fixture's stderr or database socket.
        try:
            assert psutil.Process(daemon_pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
        assert banks() == before
        assert not list(temp.iterdir())
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            child.communicate(timeout=30)


@pytest.mark.parametrize("dsn", [
    "dbname=r2_unsupported", "host=/var/run/postgresql dbname=r2_unsupported",
    "host=localhost dbname=r2_unsupported options='-c statement_timeout=5000'",
    "host=localhost dbname=r2_unsupported target_session_attrs=read-write",
    "service=fixture dbname=r2_unsupported",
])
def test_native_proof_dsn_domain_refusal_names_the_actual_limit(tmp_path, dsn):
    daemon = tmp_path / ("pseudolife-daemon.exe" if os.name == "nt" else "pseudolife-daemon")
    daemon.write_bytes(b"must not execute")
    models = _admission_models(tmp_path / "models")
    result = _run(tmp_path, dsn=dsn, models=models)
    assert result.returncode == 1 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["error"] == "NativeProofDsnUnsupported"
    assert "TCP host" in report["recovery"] and "application_name" in report["recovery"]
    assert "CREATE/DROP DATABASE permission" not in report["recovery"]


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("PSEUDOLIFE_DOCTOR_LIVE_PROOF") != "1",
                   reason="Linux installed daemon, CPU models and disposable PostgreSQL required")
@pytest.mark.parametrize("selection", ["empty", "daemon-relative", "daemon-relative-shadowed",
                                        "bare", "cwd-relative", "absolute"])
def test_runtime_library_loads_through_actual_daemon(tmp_path, selection):
    """A completed native proof requires the real daemon to load its ORT model session."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    dsn = os.environ["PSEUDOLIFE_TEST_DATABASE_URL"]
    assert conninfo_to_dict(dsn)["dbname"].startswith("r2_")
    admin = make_conninfo(dsn, dbname="postgres", connect_timeout=5)
    def banks():
        with psycopg.connect(admin, autocommit=True) as connection:
            return {row[0] for row in connection.execute(
                "SELECT datname FROM pg_database WHERE starts_with(datname, 'r2_')")}

    original = _candidate()
    original_daemon = original.with_name("pseudolife-daemon")
    library = Path(os.environ["ORT_DYLIB_PATH"]).resolve()
    assert original_daemon.is_file() and library.is_file()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    candidate = binary_dir / "pseudolife-stdio"
    shutil.copy2(original, candidate)
    shutil.copy2(original_daemon, binary_dir / "pseudolife-daemon")
    cwd = tmp_path / "work"
    cwd.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {key: value for key, value in os.environ.items() if key in (
        "PATH", "LD_LIBRARY_PATH", "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_MODELS_DIR")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), TMPDIR=str(temp),
               TMP=str(temp), TEMP=str(temp), PSEUDOLIFE_TEST_DATABASE_URL=dsn,
               CUDA_VISIBLE_DEVICES="-1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if selection == "empty":
        destination, override = binary_dir / "libonnxruntime.so", ""
    elif selection.startswith("daemon-relative"):
        destination, override = binary_dir / "runtime" / library.name, "runtime/" + library.name
        if selection.endswith("shadowed"):
            invalid = cwd / "runtime" / library.name
            invalid.parent.mkdir()
            invalid.write_bytes(b"not a dynamic library")
    elif selection == "bare":
        destination, override = tmp_path / "loader-search" / library.name, library.name
        env["LD_LIBRARY_PATH"] = str(destination.parent)
    elif selection == "cwd-relative":
        destination, override = cwd / "runtime" / library.name, "./runtime/" + library.name
    else:
        destination, override = tmp_path / "absolute" / library.name, str(tmp_path / "absolute" / library.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(library, destination)
    env["ORT_DYLIB_PATH"] = override
    before = banks()
    result = subprocess.run([str(candidate), "doctor", "--disposable-proof"], env=env,
                            cwd=cwd, capture_output=True, text=True, timeout=720)
    assert banks() == before
    assert not list(temp.iterdir()), "owned native fixture files must be removed"
    assert result.returncode == 0, result.stdout
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["ok"] and report["fixture_removed"]
    assert [stage["path"] for stage in report["stages"]] == ["hint", "ring"]
    assert all(stage["signal_observed"] and stage["harness_ack"] for stage in report["stages"])
