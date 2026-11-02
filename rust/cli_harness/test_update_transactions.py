"""Fault-injected updater acceptance against isolated subprocess Docker stubs.

Set PSEUDOLIFE_UPDATE_TEST_BINARY to the built native CLI to run its arm.
Python is an oracle/test fixture only; no real Docker command can resolve.
"""
from contextlib import contextmanager
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "pseudolife-daemon"
FILES = ("docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.rust-daemon.yml",
         "docker-compose.rust-shadow.yml", "docker-compose.rust-ghcr.yml")

# Runs only inside the disposable home. Its sole authority is the explicit
# fixture-state path passed by the harness, never the caller's environment.
DOCKER = r'''
import gzip,io,json,os,pathlib,sys,tarfile
p=pathlib.Path(os.environ["UPDATE_FIXTURE_STATE"])
s=json.loads(p.read_text());a=sys.argv[1:]
s["calls"].append(a)
code=0;out=""
if a[:1]==["inspect"]:
 if "--format" in a: out=s["running"]["Image"]
 elif "-f" in a:
  f=a[a.index("-f")+1]
  if "Labels" in f: out=json.dumps(s["running"]["Config"]["Labels"])
  elif "|" in f: out=s["running"]["Image"]+"|"+s["running"]["Config"]["Image"]
  else: out=s["running"]["Id"]
 else: out=json.dumps([s["running"]])
elif a[:2]==["image","inspect"]:
 if "Created" in " ".join(a): out="2026-01-01|"+s["running"]["Image"]
 elif a[-1] in s["images"]: out=s["images"][a[-1]]
 else: code=1
elif a[:2]==["image","ls"]: out=""
elif a[:1]==["ps"]: out=s["running"]["Id"]
elif a[:1]==["pull"]: code=int(s["failure"]=="pull")
elif a[:1]==["run"]: out="58" if "--schema-version" in a else "SCHEMA_META_VERSION = 58\n"
elif a[:1]==["tag"]: s["images"][a[-1]]=a[-2]
elif a[:1]==["compose"]:
 if "config" in a: out=json.dumps(s["config"])
 elif "ps" in a: out="fixture-pg" if a[-1]=="pseudolife-pg" else "fixture-daemon"
 elif "build" in a and s["failure"]=="build":code=1
 elif "up" in a:
  code=int(s["failure"]=="up")
  if code==0:
   s["deployed"]=True
   overlays=[a[i+1] for i,v in enumerate(a[:-1]) if v=="-f"]
   for path in overlays:
    try:
     service=json.loads(pathlib.Path(path).read_text())["services"]["pseudolife-daemon"]
     if "labels" in service:s["running"]["Config"]["Labels"]=service["labels"]
    except (OSError,ValueError,KeyError):pass
   s["running"]["Image"]="sha256:replacement"
elif a[:1]==["exec"]:
 if "psql" in a:
  if "cms_counters" in a[-1]:out="f" if s["failure"]=="pending_import" else "t"
  else:out="56" if s["failure"]=="old_schema" else "unreadable" if s["failure"]=="unreadable_schema" else "58"
 elif any("test ! -e /data/memory_state/weights.pt" in v for v in a):code=int(s["failure"] in ("pending_import","imported_weights"))
 elif any("pg_dump" in v for v in a):code=int(s["failure"]=="backup")
elif a[:1]==["cp"]:
 target=pathlib.Path(a[-1]);target.parent.mkdir(parents=True,exist_ok=True)
 if ".sql.gz" in a[-2]:target.write_bytes(gzip.compress(b"COPY x FROM stdin;\n\\.\n"+(b"" if s["failure"]=="incomplete" else b"-- PostgreSQL database dump complete\n")))
 else:
  with tarfile.open(target,"w:gz") as tar:
   member=tarfile.TarInfo("config.yaml");member.size=2;tar.addfile(member,io.BytesIO(b"{}"))
p.write_text(json.dumps(s))
print(out)
raise SystemExit(code)
'''

BACKUP = r'''
import gzip,io,json,os,pathlib,sys,tarfile
p=pathlib.Path(os.environ["UPDATE_FIXTURE_STATE"]);s=json.loads(p.read_text());s["calls"].append(["fixture-backup",*sys.argv[1:]])
p.write_text(json.dumps(s))
if s["failure"]=="backup":raise SystemExit(1)
a=sys.argv[1:];flag="-OutDir" if "-OutDir" in a else "--out-dir"
directory=pathlib.Path(a[a.index(flag)+1]) if flag in a else pathlib.Path(os.environ["UPDATE_FIXTURE_BACKUPS"])
directory.mkdir(parents=True,exist_ok=True)
(directory/"pseudolife_memory-20260101-000001.sql.gz").write_bytes(gzip.compress(b"-- PostgreSQL database dump complete\n"))
if s["failure"]!="incomplete":
 with tarfile.open(directory/"pseudolife_state-20260101-000001.tgz","w:gz") as tar:
  member=tarfile.TarInfo("config.yaml");member.size=2;tar.addfile(member,io.BytesIO(b"{}"))
'''


def _program(path, script):
    source = path.with_suffix(".py")
    source.write_text(script, encoding="utf-8")
    if os.name == "nt":
        # A cmd trampoline would interpret Docker's Go-template pipe as a
        # shell pipeline. Use pip's already-built console launcher, so argv
        # reaches the fixture byte for byte without compiling anything.
        import zipfile
        try:
            from pip._vendor.distlib.scripts import ScriptMaker
        except ModuleNotFoundError:
            # uv-created oracle environments can omit pip. CPython's bundled
            # ensurepip wheel supplies the same launcher without installing it
            # or mutating the shared interpreter environment.
            import ensurepip
            import struct
            import sysconfig
            wheels = sorted((Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl"))
            if not wheels:
                raise RuntimeError("Windows Docker fixture needs pip or CPython's bundled ensurepip wheel") from None
            bits = "64" if struct.calcsize("P") == 8 else "32"
            suffix = "-arm" if sysconfig.get_platform() == "win-arm64" else ""
            with zipfile.ZipFile(wheels[-1]) as bundle:
                launcher = bundle.read(f"pip/_vendor/distlib/t{bits}{suffix}.exe")
        else:
            launcher = ScriptMaker(None, None)._get_launcher("t")
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("__main__.py", f"import runpy\nrunpy.run_path({str(source)!r},run_name='__main__')\n")
        path = path.with_suffix(".exe")
        path.write_bytes(launcher + f'#!"{sys.executable}"\n'.encode() + payload.getvalue())
    else:
        import shlex
        path.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(source))} \"$@\"\n")
        path.chmod(0o755)
    return path


@contextmanager
def host(tmp_path, failure="", mode="release"):
    home = tmp_path / "home"
    ops = home / "checkout/ops"
    ops.mkdir(parents=True)
    for name in FILES:
        (ops / name).write_text("services:\n  pseudolife-daemon:\n    image: pseudolife-daemon:0.21.0\n")
    (ops / ".env").write_text("PSEUDOLIFE_MCP_TOKEN=fixture-bearer\n")
    repo = ops.parent
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.21.1"\n')
    (repo / "pseudolife_memory/storage").mkdir(parents=True)
    (repo / "pseudolife_memory/storage/schema.py").write_text("SCHEMA_META_VERSION = 58\n")
    programs = home / "programs"
    programs.mkdir()
    docker = _program(programs / "docker", DOCKER)
    _program(programs / "git", 'import sys\nprint("a"*40 if "rev-parse" in sys.argv else "")\n')
    backup = programs / "backup.py"
    backup.write_text(BACKUP)
    if os.name == "nt":
        (ops / "backup.ps1").write_text(f"& '{sys.executable.replace(chr(39),chr(39)*2)}' '{str(backup).replace(chr(39),chr(39)*2)}' @args\nexit $LASTEXITCODE\n")
    else:
        import shlex
        (ops / "backup.sh").write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(backup))} \"$@\"\n")
    state_path = home / "fixture-state.json"
    state = {"calls": [], "deployed": False, "failure": failure, "images": {"pseudolife-daemon:0.21.0": "sha256:validated"},
             "running": {"Id": "fixture-daemon", "Image": "sha256:validated", "State": {"Running": True},
                         "Mounts": [{"Destination": "/data", "Name": "fixture-state"}],
                         "Config": {"Image": "pseudolife-daemon:0.21.0", "Labels": None,
                                    "Env": ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://fixture:fixture@pseudolife-pg:5432/fixture"]}}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            current = json.loads(state_path.read_text())
            if self.path.endswith("SHA256SUMS"):
                body = f"{hashlib.sha256(asset).hexdigest()}  pseudolife-compose-0.21.1.tar.gz\n".encode()
            elif self.path.endswith(".tar.gz"):
                body = asset
            elif self.path == "/health":
                deployed = current["deployed"]
                body = json.dumps({"status": "error" if deployed and failure == "health" else "ok",
                                   "version": "wrong" if deployed and failure == "version" else "0.21.1" if deployed else "0.21.0",
                                   "schema": 59 if deployed and failure == "schema" else 58,
                                   "auth": True, "persist_errors": 0}).encode()
            else:
                valid = self.headers.get("Authorization") == "Bearer fixture-bearer"
                if not valid or current["deployed"] and failure == "read":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = b'{"entries":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    state["config"] = {"services": {SERVICE: {"environment": {"PSEUDOLIFE_MCP_TOKEN": "fixture-bearer",
                             "PSEUDOLIFE_MCP_DATABASE_URL": state["running"]["Config"]["Env"][0].split("=", 1)[1]},
                             "volumes": [{"type": "volume", "source": "state", "target": "/data"}],
                             "ports": [{"target": 8765, "published": str(server.server_port), "host_ip": "127.0.0.1"}]},
                             "pseudolife-pg": {"environment": {"POSTGRES_USER": "fixture", "POSTGRES_DB": "fixture"}}},
                       "volumes": {"state": {"name": "fixture-state"}}}
    # Use actual labels for release discovery; null-label behavior is covered
    # by engine mode, where project discovery identifies the container.
    if mode == "release":
        state["running"]["Config"]["Labels"] = {"com.docker.compose.project.config_files": str(ops / "docker-compose.yml"),
                    "com.docker.compose.project.working_dir": str(ops), "com.docker.compose.project.environment_file": str(ops / ".env")}
    state_path.write_text(json.dumps(state))
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        for name in FILES:
            body = (ops / name).read_bytes()
            member = tarfile.TarInfo(name)
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
    asset = payload.getvalue()
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP") if k in os.environ}
    shell_dir = str(Path(shutil.which("pwsh") or sys.executable).parent)
    env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "local"),
               PATH=str(programs) + os.pathsep + shell_dir, PYTHONPATH=str(ROOT), PYTHONUTF8="1",
               PSEUDOLIFE_DOCKER=str(docker), PSEUDOLIFE_MCP_DATA_DIR=str(home / "data"),
               PSEUDOLIFE_MCP_DAEMON_URL=url, PSEUDOLIFE_RELEASE_BASE_URL=url,
               PSEUDOLIFE_SHIM_RUNTIMES=str(home / "runtimes"),
               PSEUDOLIFE_SHIM_LAUNCHER=str(home / ("launcher.exe" if os.name == "nt" else "launcher")),
               UPDATE_FIXTURE_STATE=str(state_path), UPDATE_FIXTURE_BACKUPS=str(repo / "data/backups"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield repo, env, state_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_arm(repo, env, native, args):
    if native:
        command = [native, "update", *args]
    else:
        command = [sys.executable, "-P", "-m", "pseudolife_memory.cli", "update", *args]
    return subprocess.run(command, env=env, cwd=repo, capture_output=True, text=True, timeout=60)


def stages(state):
    result = []
    for args in state["calls"]:
        if args[:1] == ["pull"]:
            result.append("pull")
        elif args[:1] == ["fixture-backup"] or args[:1] == ["exec"] and any("pg_dump" in s for s in args):
            result.append("backup")
        elif args[:1] == ["tag"]:
            result.append("tag")
        elif args[:1] == ["compose"] and "build" in args:
            result.append("build")
        elif args[:1] == ["compose"] and "up" in args:
            result.append("up")
    return result


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("failure", ["", "backup", "incomplete", "build", "up", "health", "version", "schema", "read"])
def test_engine_activation_faults_preserve_rollback_and_retry_guard(tmp_path, native, failure):
    binary = os.environ.get("PSEUDOLIFE_UPDATE_TEST_BINARY") if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_UPDATE_TEST_BINARY")
    with host(tmp_path, failure, "engine") as (repo, env, state_path):
        args = ["--checkout", str(repo), "--engine", "rust", "--no-test-login", "--no-cache-prune", "--json",
                "--health-retries", "1", "--health-delay-ms", "0", "--rollback-tag", "pre-fixture"]
        result = run_arm(repo, env, binary, args)
        assert result.returncode == (1 if failure else 0), result.stdout + result.stderr
        report = json.loads(result.stdout)
        state = json.loads(state_path.read_text())
        observed = stages(state)
        expected = ["backup"] if failure in ("backup", "incomplete") else ["backup", "tag", "build"] if failure == "build" else ["backup", "tag", "build", "up"]
        assert observed == expected
        selection = repo / "data/update-engine/docker-compose.engine.json"
        assert selection.exists() is (not failure)
        if failure not in ("backup", "incomplete"):
            assert Path(report["rollback"]["overlay"]).is_file()
            assert report["rollback"]["state"] == "tagged"
        if failure in ("health", "version", "schema", "read"):
            retry = run_arm(repo, env, binary, args)
            assert retry.returncode == 2, retry.stdout + retry.stderr
            assert stages(json.loads(state_path.read_text())) == observed
            assert "rollback" in retry.stdout.lower()


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("failure", ["", "pull", "backup", "up", "health", "version"])
def test_release_fault_chronology_and_no_client_movement(tmp_path, native, failure):
    binary = os.environ.get("PSEUDOLIFE_UPDATE_TEST_BINARY") if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_UPDATE_TEST_BINARY")
    with host(tmp_path, failure) as (repo, env, state_path):
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1", "--rollback-tag", "pre-fixture"])
        assert result.returncode == (1 if failure else 0), result.stdout + result.stderr
        report = json.loads(result.stdout)
        observed = stages(json.loads(state_path.read_text()))
        assert observed == (["pull"] if failure == "pull" else ["pull", "backup"] if failure == "backup" else ["pull", "backup", "tag", "up"])
        assert report["clients"] is None
        if failure not in ("pull", "backup"):
            assert report["rollback"]["state"] == "tagged"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("failure", ["old_schema", "pending_import", "unreadable_schema", "imported_weights"])
def test_checkout_d2_guard_is_reached_before_backup_and_accepts_imported_row(tmp_path, native, failure):
    binary = os.environ.get("PSEUDOLIFE_UPDATE_TEST_BINARY") if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_UPDATE_TEST_BINARY")
    with host(tmp_path, failure, "engine") as (repo, env, state_path):
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--engine", "rust", "--no-test-login", "--no-cache-prune",
                                           "--json", "--health-retries", "1", "--rollback-tag", "pre-fixture"])
        assert result.returncode == (0 if failure == "imported_weights" else 2), result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        sql = [a[-1] for a in state["calls"] if "psql" in a and "-Atc" in a]
        assert sql and "schema_version" in sql[0]
        if failure == "imported_weights":
            assert any("cms_counters" in query for query in sql)
            assert stages(state) == ["backup", "tag", "build", "up"]
            assert not any("test ! -e /data/memory_state/weights.pt" in " ".join(a) for a in state["calls"])
        else:
            assert stages(state) == []
            assert "import" in result.stdout.lower()


@pytest.mark.parametrize("failure", ["old_schema", "pending_import", "unreadable_schema", "imported_weights"])
def test_native_release_d2_guard_is_reached_before_pull_and_accepts_imported_row(tmp_path, failure):
    binary = os.environ.get("PSEUDOLIFE_UPDATE_TEST_BINARY")
    if not binary:
        pytest.skip("native release engine selection extends the unpublished-image Python oracle; build owned by parent")
    with host(tmp_path, failure) as (repo, env, state_path):
        result = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.21.1", "--json", "--health-retries", "1", "--rollback-tag", "pre-fixture"])
        assert result.returncode == (0 if failure == "imported_weights" else 2), result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        sql = [a[-1] for a in state["calls"] if "psql" in a and "-Atc" in a]
        assert sql and "schema_version" in sql[0]
        assert stages(state) == (["pull", "backup", "tag", "up"] if failure == "imported_weights" else [])
        report = json.loads(result.stdout)
        assert report["clients"] is None
        if failure == "imported_weights":
            assert any("cms_counters" in query for query in sql)
            assert Path(report["rollback"]["overlay"]).is_file()
        else:
            assert report["rollback"] == {}
