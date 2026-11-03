"""Fault-injected updater acceptance against isolated subprocess Docker stubs.

The native arm accepts the standard PSEUDOLIFE_RUST_BINARY lane candidate
or the explicit PSEUDOLIFE_UPDATE_TEST_BINARY override.
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
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "pseudolife-daemon"
FILES = ("docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.rust-daemon.yml",
         "docker-compose.rust-shadow.yml", "docker-compose.rust-ghcr.yml", "Dockerfile.extractor")


def _native_binary():
    candidate = os.environ.get("PSEUDOLIFE_UPDATE_TEST_BINARY") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    return str(Path(candidate).resolve()) if candidate else None


def _fallback_fixture(tmp_path, source):
    """Use explicit legacy delegation to exercise the shipped process boundary."""
    binary = _native_binary()
    if not binary:
        pytest.skip("native candidate required")
    package = tmp_path / "pseudolife_memory"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(source, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
    env.update(PYTHONPATH=str(tmp_path), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               PSEUDOLIFE_MCP_DATA_DIR=str(tmp_path / "data"))
    argv = [binary, "update", "--python-runtime", sys.executable]
    if os.name == "posix":
        # Background shells may ignore INT/HUP. Establish the fixture's own
        # dispositions in a fresh process without a threaded preexec_fn hook.
        bootstrap = ("import os, signal, sys; "
                     "[signal.signal(s, signal.SIG_DFL) for s in "
                     "(signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGTSTP)]; "
                     "os.execv(sys.argv[1], sys.argv[1:])")
        argv = [sys.executable, "-c", bootstrap, *argv]
    return subprocess.Popen(argv, cwd=tmp_path, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _await_file(path, process):
    deadline = time.monotonic() + 30
    while not path.exists() and time.monotonic() < deadline:
        assert process.poll() is None, "fixture exited before readiness"
        time.sleep(0.02)
    assert path.exists(), "fixture did not become ready"


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX process-group cancellation")
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"])
@pytest.mark.parametrize("resistant", [False, True])
def test_native_update_forwards_cancellation_to_owned_descendants(tmp_path, signal_name, resistant):
    import signal

    child = tmp_path / "child.py"
    if resistant:
        (tmp_path / "resist").touch()
    child.write_text('''import os, pathlib, signal, time
root = pathlib.Path(__file__).parent
def stop(signum, frame):
    (root / "child-signal").write_text(str(signum))
    if (root / "resist").exists():
        return
    raise SystemExit(0)
for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(signum, stop)
(root / "ready").write_text(str(os.getpid()))
deadline = time.monotonic() + 60
while not (root / "stop").exists() and time.monotonic() < deadline:
    time.sleep(.02)
''', encoding="utf-8")
    proc = _fallback_fixture(tmp_path, '''import pathlib, signal, subprocess, sys, time
root = pathlib.Path.cwd()
child = subprocess.Popen([sys.executable, str(root / "child.py")])
def stop(signum, frame):
    child.wait(timeout=10)
    raise SystemExit(128 + signum)
for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(signum, stop)
deadline = time.monotonic() + 60
while not (root / "stop").exists() and time.monotonic() < deadline:
    time.sleep(.02)
child.wait(timeout=10)
''')
    try:
        _await_file(tmp_path / "ready", proc)
        proc.send_signal(getattr(signal, signal_name))
        proc.wait(timeout=15)
        assert (tmp_path / "child-signal").exists(), "updater abandoned its child group"
        assert int((tmp_path / "child-signal").read_text()) == getattr(signal, signal_name)
        assert proc.returncode in (-getattr(signal, signal_name), 128 + getattr(signal, signal_name))
        status = Path("/proc") / (tmp_path / "ready").read_text() / "stat"
        assert not status.exists() or status.read_text().split(")", 1)[1].split()[0] == "Z"
    finally:
        (tmp_path / "stop").touch()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=15)


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX process-group cancellation")
def test_native_update_forwards_second_interrupt_during_cleanup(tmp_path):
    import signal

    child = tmp_path / "child.py"
    child.write_text("""import pathlib, signal, time
root = pathlib.Path(__file__).parent
count = 0
def stop(signum, frame):
    global count
    count += 1
    (root / f"signal-{count}").touch()
    if count == 2:
        raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
(root / "ready").touch()
while not (root / "stop").exists():
    time.sleep(.02)
""", encoding="utf-8")
    proc = _fallback_fixture(tmp_path, """import pathlib, signal, subprocess, sys
signal.signal(signal.SIGINT, lambda *_: None)
child = subprocess.Popen([sys.executable, str(pathlib.Path.cwd() / "child.py")])
child.wait()
""")
    try:
        _await_file(tmp_path / "ready", proc)
        proc.send_signal(signal.SIGINT)
        _await_file(tmp_path / "signal-1", proc)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
        assert (tmp_path / "signal-2").exists(), "second interrupt never reached the owned group"
        assert proc.returncode in (-signal.SIGINT, 128 + signal.SIGINT)
    finally:
        (tmp_path / "stop").touch()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=15)


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX job control")
def test_native_update_suspends_and_resumes_owned_command(tmp_path):
    import signal

    proc = _fallback_fixture(tmp_path, """import os, pathlib, signal, time
root = pathlib.Path.cwd()
def suspend(signum, frame):
    (root / "suspended").touch()
    os.kill(os.getpid(), signal.SIGSTOP)
    (root / "resumed").touch()
signal.signal(signal.SIGTSTP, suspend)
(root / "ready").touch()
while not (root / "stop").exists():
    time.sleep(.02)
""")
    try:
        _await_file(tmp_path / "ready", proc)
        proc.send_signal(signal.SIGTSTP)
        _await_file(tmp_path / "suspended", proc)
        deadline = time.monotonic() + 30
        state = Path(f"/proc/{proc.pid}/stat")
        while time.monotonic() < deadline:
            assert proc.poll() is None
            if state.read_text().split(")", 1)[1].split()[0] == "T":
                break
            time.sleep(.02)
        else:
            pytest.fail("updater did not suspend with its owned command")
        proc.send_signal(signal.SIGCONT)
        _await_file(tmp_path / "resumed", proc)
        (tmp_path / "stop").touch()
        assert proc.wait(timeout=15) == 0
    finally:
        (tmp_path / "stop").touch()
        proc.send_signal(signal.SIGCONT) if proc.poll() is None else None
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=15)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object completion")
def test_native_update_does_not_wait_for_successful_background_helpers(tmp_path):
    proc = _fallback_fixture(tmp_path, '''import pathlib, subprocess, sys, time
source = """import pathlib, time
root = pathlib.Path.cwd()
(root / 'helper-ready').touch()
deadline = time.monotonic() + 60
while not (root / 'stop').exists() and time.monotonic() < deadline:
    (root / 'heartbeat').write_text(str(time.monotonic()))
    time.sleep(.05)
"""
subprocess.Popen([sys.executable, '-c', source], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 30
while not pathlib.Path('helper-ready').exists() and time.monotonic() < deadline:
    time.sleep(.02)
pathlib.Path('ready').touch()
raise SystemExit(7)
''')
    try:
        _await_file(tmp_path / "ready", proc)
        assert proc.wait(timeout=10) == 7
        before = (tmp_path / "heartbeat").read_text()
        deadline = time.monotonic() + 5
        while (tmp_path / "heartbeat").read_text() == before and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / "heartbeat").read_text() != before, "completed command killed its helper"
    finally:
        (tmp_path / "stop").touch()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=15)


@pytest.mark.skipif(os.name != "nt", reason="Windows Git fsmonitor background service")
def test_native_update_preserves_git_fsmonitor(tmp_path):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git required")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run([git, "init", "-q", str(tmp_path)], env=env, check=True, timeout=15)
    subprocess.run([git, "-C", str(tmp_path), "config", "core.fsmonitor", "true"],
                   env=env, check=True, timeout=15)
    proc = _fallback_fixture(tmp_path, f'''import os, pathlib, subprocess
env = {{k:v for k,v in os.environ.items() if not k.startswith('GIT_')}}
env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
subprocess.run([{git!r}, 'status', '--porcelain'], env=env, check=True, timeout=15)
pathlib.Path('ready').touch()
''')
    try:
        _await_file(tmp_path / "ready", proc)
        assert proc.wait(timeout=10) == 0
        status = subprocess.run([git, "-C", str(tmp_path), "fsmonitor--daemon", "status"],
                                env=env, capture_output=True, timeout=15)
        assert status.returncode == 0, "completed git status lost its fsmonitor daemon"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=15)
        subprocess.run([git, "-C", str(tmp_path), "fsmonitor--daemon", "stop"],
                       env=env, capture_output=True, timeout=15)


def test_native_transactions_use_the_standard_lane_candidate(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_UPDATE_TEST_BINARY", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_RUST_BINARY", "lane-candidate")
    assert _native_binary() == str(Path("lane-candidate").resolve())
    monkeypatch.setenv("PSEUDOLIFE_UPDATE_TEST_BINARY", "explicit-candidate")
    assert _native_binary() == str(Path("explicit-candidate").resolve())

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
  if "State.Running" in f:
   if a[-1]=="missing-daemon":code=1;out="No such container"
   else:out="false" if s.get("deployed") and s["failure"]=="recreated_stopped" else "true"
  elif "State.Status" in f:
   out={"recreated_stopped":"exited","recreated_restarting":"restarting"}.get(s["failure"],"running") if s.get("deployed") else "running"
  elif "index .Config.Labels" in f: out=s.get("setup_engine","python")
  elif "Labels" in f: out=json.dumps(s["running"]["Config"]["Labels"])
  elif "|" in f: out=s["running"]["Image"]+"|"+s["running"]["Config"]["Image"]
  else: out=s["running"]["Id"]
 else: out=json.dumps([s["running"]])
elif a[:2]==["image","inspect"]:
 if "Created" in " ".join(a): out="2026-01-01|"+s["images"].get(a[-1],s["running"]["Image"])
 elif a[-1] in s["images"]: out=s["images"][a[-1]]
 else: code=1
elif a[:2]==["image","ls"]: out="\n".join(s["images"]) if s.get("list_images") else ""
elif a[:1]==["port"]:out="127.0.0.1:5433"
elif a[:2]==["volume","inspect"]:
 if s.get("volume_inspect_failure"):code=1
 else:out=json.dumps(s.get("volume_inspections",{}).get(a[-1],[]))
elif a[:1]==["ps"]: out=s["running"]["Id"]
elif a[:1]==["pull"]: code=int(s["failure"]=="pull")
elif a[:1]==["run"]: out="58" if "--schema-version" in a else "SCHEMA_META_VERSION = 58\n"
elif a[:1]==["tag"]: s["images"][a[-1]]=a[-2]
elif a[:1]==["compose"]:
 if "config" in a:
  config=json.loads(json.dumps(s["config"]))
  if s.get("relative_mount"):
   directory=pathlib.Path(a[a.index("--project-directory")+1]) if "--project-directory" in a else pathlib.Path(a[a.index("-f")+1]).parent
   config["services"]["pseudolife-daemon"]["volumes"].append({"type":"bind","source":str(directory/"config.yaml"),"target":"/data/config.yaml","read_only":True})
  out=json.dumps(config)
 elif "ps" in a:
  out="fixture-pg" if a[-1]=="pseudolife-pg" else s["running"]["Id"]
  if s["failure"]=="missing_project_daemon" and a[-1]=="pseudolife-daemon":out=""
  if s["failure"]=="ambiguous_project_daemon" and a[-1]=="pseudolife-daemon":out="first\nsecond"
  if s.get("deployed") and s["failure"]=="recreated_missing" and a[-1]=="pseudolife-daemon":out=""
 elif "build" in a and s["failure"]=="build":code=1
 elif "up" in a:
  code=int(s["failure"]=="up")
  if code==0:
   s["deployed"]=True
   overlays=[a[i+1] for i,v in enumerate(a[:-1]) if v=="-f"]
   for path in overlays:
    try:
     service=json.loads(pathlib.Path(path).read_text())["services"]["pseudolife-daemon"]
     if "labels" in service:s["running"]["Config"]["Labels"]={**(s["running"]["Config"]["Labels"] or {}),**service["labels"]}
    except (OSError,ValueError,KeyError):pass
   s["running"]["Image"]="sha256:replacement"
   s["running"]["Id"]=s.get("replacement_id",s["running"]["Id"])
elif a[:1]==["exec"]:
 if "--host-state" in a or "pseudolife_memory.maintainer_setup" in a:
  out=s.get("setup_reply",json.dumps({"problem":None,"keys":[{"state":"active"}]}))
 elif "psql" in a:
  if "cms_counters" in a[-1]:out="f" if s["failure"]=="pending_import" else "t"
  else:out="56" if s["failure"]=="old_schema" else "unreadable" if s["failure"]=="unreadable_schema" else "58"
 elif any("test ! -e /data/memory_state/weights.pt" in v for v in a):code=int(s["failure"] in ("pending_import","imported_weights"))
 elif any("pg_dump" in v for v in a):code=int(s["failure"]=="backup")
 elif any("tar czf" in v for v in a):code=int(s["failure"]=="missing_state")
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
if s["failure"] not in ("incomplete", "missing_state"):
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
    if os.name != "nt":
        # PowerShell can live outside Bash's directory on POSIX runners.
        # Expose only the needed interpreter, keeping Docker isolated.
        bash = shutil.which("bash")
        assert bash, "checkout backup fixture requires Bash"
        (programs / "bash").symlink_to(Path(bash).resolve())
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
                         "Mounts": [{"Destination": "/data", "Name": "fixture-state", "Type": "volume", "RW": True}],
                         "Config": {"Image": "pseudolife-daemon:0.21.0", "Labels": None,
                                    "Env": ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://fixture:fixture@pseudolife-pg:5432/fixture"]}}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            current = json.loads(state_path.read_text())
            if (self.path.endswith("SHA256SUMS") and current.get("missing_sums")) or (self.path.endswith(".tar.gz") and current.get("missing_bundle")):
                self.send_response(404)
                self.end_headers()
                return
            if self.path.endswith("SHA256SUMS"):
                body = b"" if current.get("missing_checksum") else f"{hashlib.sha256(asset).hexdigest()}  pseudolife-compose-{self.path.split(chr(47))[-2][1:]}.tar.gz\n".encode()
            elif self.path.endswith(".tar.gz"):
                body = asset
            elif self.path.startswith("/releases/tags/"):
                if current.get("missing_release"):
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({"tag_name": self.path.rsplit("/", 1)[-1], "draft": False}).encode()
            elif self.path == "/health":
                deployed = current["deployed"]
                body = json.dumps({"status": "error" if deployed and failure == "health" or not deployed and failure == "stopped" else "ok",
                                   "version": "wrong" if deployed and failure == "version" else current.get("target_version", "0.21.1") if deployed else "0.21.0",
                                   "schema": 59 if deployed and failure == "schema" else 58,
                                   "auth": True, "persist_errors": 0,
                                   "updates": {"unattended_daemon": True}}).encode()
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

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            current = json.loads(state_path.read_text())
            if self.path == "/api/coordination/register":
                payload = {"agent_id": "fixture-address", "credential": "fixture-key"}
            elif self.path == "/api/coordination/agents":
                # Backup completion brings a peer onto the board. Only the
                # callback inside deploy_release can observe this transition.
                active = any(a[:1] == ["fixture-backup"] for a in current["calls"])
                payload = {"agents": [{"agent_id": "fixture-peer", "task": "working"}] if active else []}
            else:
                payload = {"recipients": 1}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    state["config"] = {"name": "fixture-project", "services": {SERVICE: {"environment": {"PSEUDOLIFE_MCP_TOKEN": "fixture-bearer",
                             "PSEUDOLIFE_MCP_DATABASE_URL": state["running"]["Config"]["Env"][0].split("=", 1)[1]},
                             "volumes": [{"type": "volume", "source": "state", "target": "/data"}],
                             "ports": [{"target": 8765, "published": str(server.server_port), "host_ip": "127.0.0.1"}]},
                             "pseudolife-pg": {"environment": {"POSTGRES_USER": "fixture", "POSTGRES_DB": "fixture"}}},
                       "volumes": {"state": {"name": "fixture-state"}}}
    # Use actual labels for release discovery; null-label behavior is covered
    # by engine mode, where project discovery identifies the container.
    if mode in ("release", "checkout"):
        state["running"]["Config"]["Labels"] = {"com.docker.compose.project": "fixture-project", "com.docker.compose.service": SERVICE,
                    "com.docker.compose.project.config_files": str(ops / "docker-compose.yml"),
                    "com.docker.compose.project.working_dir": str(ops), "com.docker.compose.project.environment_file": str(ops / ".env")}
    state_path.write_text(json.dumps(state))
    # Consume exactly the producer's bundle, including Dockerfile.extractor.
    # A hand-built five-file tar missed the sixth release asset in review.
    from rust.ci import release_assets
    assets = home / "release-assets"
    stub = home / "asset-binary"
    stub.write_bytes(b"fixture native asset")
    for target in release_assets.TARGETS:
        release_assets.package(stub, "0.21.1", target, assets)
    release_assets.assemble(repo, "0.21.1", assets)
    asset = (assets / "pseudolife-compose-0.21.1.tar.gz").read_bytes()
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP") if k in os.environ}
    shell_dir = str(Path(shutil.which("pwsh") or sys.executable).parent)
    env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "local"),
               PATH=str(programs) + os.pathsep + shell_dir, PYTHONPATH=str(ROOT), PYTHONUTF8="1",
               PSEUDOLIFE_DOCKER=str(docker), PSEUDOLIFE_MCP_DATA_DIR=str(home / "data"),
               PSEUDOLIFE_MCP_DAEMON_URL=url, PSEUDOLIFE_RELEASE_BASE_URL=url,
               PSEUDOLIFE_RELEASE_API_BASE_URL=url + "/releases/tags",
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX checkout backup interpreter")
def test_fixture_backup_finds_bash_when_powershell_is_in_a_separate_directory(tmp_path, monkeypatch):
    powershell = tmp_path / "powershell-only" / "pwsh"
    powershell.parent.mkdir()
    real_which = shutil.which

    def which(command, *args, **kwargs):
        if command == "pwsh":
            return str(powershell)
        return real_which(command, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        assert real_which("docker", path=env["PATH"]) == env["PSEUDOLIFE_DOCKER"]
        assert real_which("bash", path=env["PATH"]), "fixture PATH hides its backup interpreter"
        result = subprocess.run(["bash", str(repo / "ops/backup.sh")], cwd=repo, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(state_path.read_text())["calls"] == [["fixture-backup"]]
        directory = Path(env["UPDATE_FIXTURE_BACKUPS"])
        assert (directory / "pseudolife_memory-20260101-000001.sql.gz").is_file()
        assert (directory / "pseudolife_state-20260101-000001.tgz").is_file()


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


@pytest.mark.parametrize("kind", ["bind", "volume"])
@pytest.mark.parametrize("mode", ["checkout", "release", "engine"])
def test_new_mount_is_allowed_without_replacing_existing_data(tmp_path, kind, mode):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, mode=mode) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["config"]["volumes"]["cache"] = {"name": "fixture-cache"}
        state["config"]["services"][SERVICE]["volumes"].append(
            {"type": kind, "source": "cache" if kind == "volume" else str(repo / "cache"),
             "target": "/new-cache"})
        state_path.write_text(json.dumps(state))
        args = ["--json", "--no-cache-prune", "--health-retries", "1", "--health-delay-ms", "0"]
        if mode == "release":
            args += ["--tag", "0.21.1", "--daemon-only"]
        else:
            args += ["--checkout", str(repo), "--no-test-login"]
            if mode == "engine":
                args += ["--engine", "rust"]
        result = run_arm(repo, env, binary, args)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "up" in stages(json.loads(state_path.read_text()))


@pytest.mark.parametrize("failure", ["recreated_stopped", "recreated_restarting", "recreated_missing"])
def test_checkout_health_from_another_daemon_cannot_hide_stopped_recreation(tmp_path, failure):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, failure, "checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["replacement_id"] = "recreated-checkout-daemon"
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-test-login",
                                           "--no-cache-prune", "--health-retries", "1", "--health-delay-ms", "0"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert "recreated" in result.stdout and "rollback" in result.stdout.lower()
        assert "nothing was changed" not in result.stdout
        assert "before updating" not in result.stdout
        state = json.loads(state_path.read_text())
        if failure != "recreated_missing":
            assert ["inspect", "-f", "{{.State.Status}}", "recreated-checkout-daemon"] in state["calls"]


@pytest.mark.parametrize("target", ["/data/x", "/data/x/../y", "/"])
@pytest.mark.parametrize("kind", ["bind", "volume", "tmpfs"])
def test_new_mount_cannot_shadow_existing_data(tmp_path, target, kind):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["config"]["volumes"]["cache"] = {"name": "fixture-cache"}
        state["config"]["services"][SERVICE]["volumes"].append(
            {"type": kind, "source": "cache" if kind == "volume" else str(repo / "cache"), "target": target})
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-test-login", "--no-cache-prune"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "overlaps" in result.stdout and "remove" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("target,allowed", [("/data/cache:mode=1777", False), ("/data", False), ("/", False), ("/new-cache", True)])
def test_service_tmpfs_mounts_cannot_shadow_persistent_data(tmp_path, target, allowed):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        # Captured from `docker compose config --format json`: service-level
        # tmpfs remains a separate list, including mount options after ':'.
        state["config"]["services"][SERVICE]["tmpfs"] = [target]
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-test-login", "--no-cache-prune"])
        assert result.returncode == (0 if allowed else 2), result.stdout + result.stderr
        if not allowed:
            assert "overlaps" in result.stdout or "persistent mount" in result.stdout
            assert "remove" in result.stdout or "restore" in result.stdout
            assert stages(json.loads(state_path.read_text())) == []


def test_optional_test_login_uses_selected_project_postgres(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        if os.name == "nt":
            # test-login uses bare `docker`, unlike update's explicit override.
            # Rust searches its executable directory and System32 before the
            # inherited PATH; hosted Windows has Docker in System32. Keep this
            # copied candidate beside our stub so no host Docker can resolve.
            binary = shutil.copyfile(binary, Path(env["PSEUDOLIFE_DOCKER"]).with_name(Path(binary).name))
        env["PSEUDOLIFE_TEST_PG_HOST_PORT"] = "127.0.0.1:5433"
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-cache-prune"])
        assert result.returncode == 0, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        assert ["port", "fixture-pg", "5432/tcp"] in calls
        assert not any("pseudolife-mcp-postgres" in call for call in calls)
        assert any(call[:3] == ["exec", "-i", "fixture-pg"] and any("psql" in arg for arg in call) for call in calls)


def test_builtin_backup_has_independent_container_deadlines(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        for name in ("backup.sh", "backup.ps1"):
            (repo / "ops" / name).unlink(missing_ok=True)
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-test-login", "--no-cache-prune"])
        assert result.returncode == 0, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        for command in ("pg_dump", "tar czf"):
            call = next(a for a in calls if a[0] == "exec" and any(command in v for v in a))
            assert call[2:5] == ["timeout", "--kill-after=5s", "3500s"]


def test_rollback_retention_leaves_other_projects_and_current_tag(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        args = ["--checkout", str(repo), "--json", "--no-test-login", "--no-cache-prune", "--keep-rollbacks", "0"]
        first = run_arm(repo, env, binary, args)
        assert first.returncode == 0, first.stdout + first.stderr
        state = json.loads(state_path.read_text())
        tag = next(call[-1] for call in state["calls"] if call[:1] == ["tag"])
        # New tags carry a project scope before their timestamp; legacy tags
        # and tags from another scope have no deletion authority here.
        prefix = tag.rsplit("-", 2)[0]
        own = prefix + "-20000101-000000"
        other = "pseudolife-daemon:0.21.0-pre-update-other-project-20000101-000000"
        legacy = "pseudolife-daemon:0.21.0-pre-update-19990101-000000"
        manual = own + "-keep"
        state["images"].update({own:"sha256:old-own", other:"sha256:other", legacy:"sha256:legacy", manual:"sha256:manual"})
        state.update(calls=[], deployed=False, list_images=True)
        state["running"]["Image"] = "sha256:validated"
        state_path.write_text(json.dumps(state))
        second = run_arm(repo, env, binary, args)
        assert second.returncode == 0, second.stdout + second.stderr
        calls = json.loads(state_path.read_text())["calls"]
        removed = [call[-1] for call in calls if call[:1] == ["rmi"]]
        assert own in removed
        assert other not in removed and legacy not in removed and tag not in removed
        assert manual not in removed


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("failure", ["", "backup", "up", "health"])
def test_ordinary_checkout_transaction_phase_parity(tmp_path, native, failure):
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, failure, "checkout") as (repo, env, state_path):
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--no-test-login", "--no-cache-prune",
                                           "--json", "--health-retries", "1", "--health-delay-ms", "0",
                                           "--rollback-tag", "pre-fixture"])
        assert result.returncode == (1 if failure else 0), result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert stages(json.loads(state_path.read_text())) == (["backup"] if failure == "backup" else ["backup", "tag", "up"])
        assert report["clients"] is None


@pytest.mark.parametrize("script_backup", [True, False], ids=["checkout-script", "native-backup"])
@pytest.mark.parametrize("failure", ["", "version", "schema", "read", "missing_state"])
def test_native_checkout_scopes_backup_and_rollback_to_its_compose_project(tmp_path, script_backup, failure):
    """Plain checkout retains project scoping and Python's health-only gate."""
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, failure, "checkout") as (repo, env, state_path):
        if not script_backup:
            (repo / "ops" / ("backup.ps1" if os.name == "nt" else "backup.sh")).unlink()
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--no-test-login", "--no-cache-prune",
                                           "--json", "--health-retries", "1", "--health-delay-ms", "0",
                                           "--rollback-tag", "pre-fixture"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        calls = state["calls"]
        assert not any("pseudolife-mcp-daemon" in args or "pseudolife-mcp-postgres" in args for args in calls)
        if script_backup:
            backup = next(args for args in calls if args[:1] == ["fixture-backup"])
            flags = ("-Container", "-DaemonContainer", "-User", "-Db") if os.name == "nt" else (
                "--container", "--daemon-container", "--user", "--db")
            assert [backup[backup.index(flag) + 1] for flag in flags] == ["fixture-pg", "fixture-daemon", "fixture", "fixture"]
        else:
            backup = next(args for args in calls if args[:1] == ["exec"] and any("pg_dump" in part for part in args))
            assert backup[1] == "fixture-pg" and backup[-2:] == ["fixture", "fixture"]
        assert stages(state) == ["backup", "tag", "up"]
        report = json.loads(result.stdout)
        assert report["clients"] is None
        if failure != "incomplete":
            assert ["inspect", "-f", "{{.Image}}|{{.Config.Image}}", "fixture-daemon"] in calls
            assert report["rollback"]["state"] == "tagged"
            assert state["images"][report["rollback"]["tag"]] == "sha256:validated"


@pytest.mark.parametrize("failure", ["missing_project_daemon", "ambiguous_project_daemon"])
def test_checkout_discovery_never_falls_back_to_another_projects_global_daemon(tmp_path, failure):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, failure, "checkout") as (repo, env, state_path):
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--allow-dirty", "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert stages(state) == []
        assert not any("pseudolife-mcp-daemon" in args or "pseudolife-mcp-postgres" in args for args in state["calls"])
        assert "restore its Compose project" in result.stdout


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("failure", ["", "backup", "incomplete", "build", "up", "health", "version", "schema", "read"])
def test_engine_activation_faults_preserve_rollback_and_retry_guard(tmp_path, native, failure):
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
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
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
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
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
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


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_rust_refuses_unpublished_image_before_any_effect(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        result = run_arm(repo, env, binary if native else None, ["--engine", "rust", "--tag", "0.21.1", "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "not published" in result.stdout + result.stderr
        assert "--checkout" in result.stdout + result.stderr
        assert json.loads(state_path.read_text())["calls"] == []


def test_native_release_rollback_supplies_required_compose_image_tag(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    with host(tmp_path, "health") as (repo, env, state_path):
        env.pop("PSEUDOLIFE_IMAGE_TAG", None)
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--daemon-only", "--json",
                                           "--health-retries", "1", "--health-delay-ms", "0", "--rollback-tag", "pre-fixture"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert stages(json.loads(state_path.read_text())) == ["pull", "backup", "tag", "up"]
        report = json.loads(result.stdout)
        assert report["rollback"]["state"] == "tagged"
        assert "PSEUDOLIFE_IMAGE_TAG=" in result.stdout
        assert "--pull never" in result.stdout


@pytest.mark.parametrize("native_first", [True, False])
@pytest.mark.parametrize("explicit_python", [False, True])
def test_release_updaters_can_follow_each_other(tmp_path, native_first, explicit_python):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        for index, native in enumerate([native_first, not native_first]):
            args = ["--tag", "0.21.1", "--daemon-only", "--reinstall", "--json",
                    "--health-retries", "1", "--rollback-tag", f"pre-round-{index}"]
            if native and explicit_python:
                args += ["--engine", "python"]
            result = run_arm(repo, env, binary if native else None, args)
            assert result.returncode == 0, result.stdout + result.stderr
            labels = json.loads(state_path.read_text())["running"]["Config"]["Labels"]
            if not explicit_python:
                assert not labels.get("org.pseudolife.activation")
            for backup in (repo / "data/backups").glob("pseudolife_*"):
                backup.unlink()


@pytest.mark.parametrize("mode", ["release", "checkout"])
def test_plain_update_accepts_tokenless_host_and_scaffolded_env(tmp_path, mode):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode=mode) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["config"]["services"][SERVICE]["environment"].pop("PSEUDOLIFE_MCP_TOKEN")
        state_path.write_text(json.dumps(state))
        if mode == "checkout":
            (repo / "ops/.env").unlink()
            (repo / "ops/.env.example").write_text("# PSEUDOLIFE_MCP_TOKEN=\n")
        args = ["--tag", "0.21.1", "--daemon-only"] if mode == "release" else [
            "--checkout", str(repo), "--no-test-login", "--no-cache-prune"]
        result = run_arm(repo, env, binary, [*args, "--json", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert stages(json.loads(state_path.read_text()))[-2:] == ["tag", "up"]


def test_plain_release_can_recover_an_unhealthy_daemon(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "stopped") as (repo, env, state_path):
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert stages(json.loads(state_path.read_text())) == ["pull", "backup", "tag", "up"]


@pytest.mark.parametrize("setup_reply,missing", [
    ('{"problem":null,"keys":[{"state":"active"}]}', False),
    ('{"problem":null,"keys":[]}', True),
    ('{"problem":null,"keys":null}', False),
    ('unreadable', False),
])
def test_maintainer_setup_check_uses_recreated_checkout_container(tmp_path, setup_reply, missing):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(setup_reply=setup_reply, replacement_id="second-project-new-daemon")
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--no-test-login", "--no-cache-prune", "--json", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert ("passkeys are not set up" in result.stdout) is missing
        calls = json.loads(state_path.read_text())["calls"]
        checks = [args for args in calls if "pseudolife_memory.maintainer_setup" in args or "--host-state" in args]
        assert checks and all("second-project-new-daemon" in args for args in checks)
        assert not any("pseudolife-mcp-daemon" in args for args in calls)


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("container", [None, "second-project-daemon", "missing-daemon"])
def test_maintainer_setup_explicit_container_scope(tmp_path, native, container):
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        command = [binary] if native else [sys.executable, "-P", "-m", "pseudolife_memory.cli"]
        command += ["maintainer", "setup", "--check"]
        if container is not None:
            command += ["--container", container]
        result = subprocess.run(command, env=env, cwd=repo, capture_output=True, text=True, timeout=30)
        if container == "missing-daemon":
            assert result.returncode == 2
            assert "--container" in result.stderr and "docker ps" in result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            assert result.stdout == "maintainer passkeys: in place\n"
            assert result.stderr == ""
        calls = json.loads(state_path.read_text())["calls"]
        assert calls and all((container or "pseudolife-mcp-daemon") in args for args in calls)
        if container == "missing-daemon":
            assert not any(args[0] == "exec" for args in calls)


def test_native_unattended_rechecks_board_after_backup_before_tagging(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        result = run_arm(repo, env, binary, ["--unattended", "--tag", "0.21.1", "--json"])
        assert result.returncode == 4, result.stdout + result.stderr
        assert "active" in result.stdout
        assert stages(json.loads(state_path.read_text())) == ["pull", "backup"]


def test_release_retains_original_project_directory_for_relative_mounts(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        override = repo / "ops/docker-compose.override.yml"
        override.write_text("services:\n  pseudolife-daemon:\n    volumes:\n      - ./config.yaml:/data/config.yaml:ro\n")
        (repo / "ops/config.yaml").write_text("fixture: true\n")
        state = json.loads(state_path.read_text())
        state["relative_mount"] = True
        state["running"]["Config"]["Labels"]["com.docker.compose.project.config_files"] += "," + str(override)
        state["running"]["Mounts"].append({"Type": "bind", "Source": str(repo / "ops/config.yaml"),
                                            "Destination": "/data/config.yaml", "RW": False})
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        for args in calls:
            if args[:1] == ["compose"] and ("config" in args or "up" in args):
                assert args[args.index("--project-directory") + 1] == str(repo / "ops")


@pytest.mark.parametrize("mode", ["release", "checkout"])
def test_changed_mount_or_wrong_checkout_endpoint_refuses_before_backup(tmp_path, mode):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode=mode) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["config"]["services"][SERVICE]["volumes"].append({
            "type": "bind", "source": str(repo / "bundle/config.yaml"), "target": "/data/config.yaml", "read_only": True})
        state["running"]["Mounts"].append({"Type": "bind", "Source": str(repo / "ops/config.yaml"),
                                            "Destination": "/data/config.yaml", "RW": False})
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.21.1"] if mode == "release" else ["--checkout", str(repo)]
        result = run_arm(repo, env, binary, [*args, "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "mount" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("rollback_state", ["kept", "none"])
def test_plain_checkout_unavailable_rollback_prints_recovery(tmp_path, rollback_state):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "health", "checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["images"] = {} if rollback_state == "none" else {"pseudolife-daemon:0.21.0": "sha256:unvalidated"}
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-backup", "--no-test-login",
                                            "--no-cache-prune", "--health-retries", "1"])
        assert result.returncode == 1, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["rollback"]["state"] == rollback_state
        assert "docker compose" in result.stdout
        assert ("surviving rollback tag" if rollback_state == "kept" else "Restore the previous checkout") in result.stdout


def test_plain_checkout_allows_a_forwarded_health_endpoint(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["config"]["services"][SERVICE]["ports"][0]["published"] = "1"
        state_path.write_text(json.dumps(state))
        state["config"]["services"][SERVICE]["ports"][0]["host_ip"] = "0.0.0.0"
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-test-login", "--no-cache-prune"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert stages(json.loads(state_path.read_text()))[-1] == "up"


@pytest.mark.parametrize("missing", ["missing_sums", "missing_bundle", "missing_checksum"])
def test_release_without_native_bundle_names_python_or_new_release(tmp_path, missing):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state[missing] = True
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--reinstall", "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "Python updater" in result.stdout and "--tag" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("engine", ["python", "rust"])
def test_discovered_checkout_backup_policy_uses_effective_engine(tmp_path, engine):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "missing_state", "checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        labels = state["running"]["Config"]["Labels"]
        labels.update({"org.pseudolife.engine": engine, "org.pseudolife.checkout": str(repo),
                       "org.pseudolife.activation": "accepted-fixture"})
        state_path.write_text(json.dumps(state))
        accepted = repo / "data/update-engine" / ("docker-compose.engine.json" if engine == "rust" else "docker-compose.python-accepted.json")
        accepted.parent.mkdir(parents=True)
        accepted.write_text(json.dumps({"services": {SERVICE: {"labels": labels}}}))
        args = [] if engine == "rust" else ["--checkout", str(repo), "--no-test-login", "--no-cache-prune"]
        result = run_arm(repo, env, binary, [*args, "--json", "--health-retries", "1"])
        if engine == "rust":
            assert result.returncode != 0, result.stdout + result.stderr
            assert "state" in result.stdout
            assert stages(json.loads(state_path.read_text())) == ["backup"]
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            assert "continuing with the complete SQL dump" in result.stdout


def test_plain_release_after_python_activation_does_not_require_bearer(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        labels = state["running"]["Config"]["Labels"]
        labels.update({"org.pseudolife.engine": "python", "org.pseudolife.release": "0.21.0",
                       "org.pseudolife.activation": "accepted-fixture"})
        state["config"]["services"][SERVICE]["environment"].pop("PSEUDOLIFE_MCP_TOKEN")
        state_path.write_text(json.dumps(state))
        accepted = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/docker-compose.release-accepted.json"
        accepted.parent.mkdir(parents=True)
        accepted.write_text(json.dumps({"services": {SERVICE: {"labels": labels}}}))
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr


def test_plain_checkout_refuses_a_container_from_another_project(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["running"]["Config"]["Labels"]["com.docker.compose.project"] = "other-project"
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "project" in result.stdout and "engine verification" not in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


def test_release_with_native_bundle_is_not_rejected_by_version_cutoff(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["target_version"] = "0.21.0"
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "0.21.0", "--reinstall", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("previous", ["bind", "volume"])
@pytest.mark.parametrize("replacement", ["tmpfs", "anonymous"])
def test_ephemeral_mount_cannot_replace_persistent_contents(tmp_path, previous, replacement):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        desired = {"type": "tmpfs" if replacement == "tmpfs" else "volume", "target": "/cache"}
        state["config"]["services"][SERVICE]["volumes"].append(desired)
        state["running"]["Mounts"].append({"Type": previous, "Destination": "/cache", "RW": False,
                                            "Source": "/persistent/cache", "Name": "persistent-cache" if previous == "volume" else ""})
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-test-login", "--no-cache-prune"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "/cache" in result.stdout and "restore" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("previous", ["tmpfs", "anonymous", "absent"])
@pytest.mark.parametrize("replacement", ["tmpfs", "anonymous"])
def test_ephemeral_mount_remains_safe_to_recreate(tmp_path, previous, replacement):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["config"]["services"][SERVICE]["volumes"].append({
            "type": "tmpfs" if replacement == "tmpfs" else "volume", "target": "/cache"})
        if previous != "absent":
            state["running"]["Mounts"].append({"Type": "tmpfs" if previous == "tmpfs" else "volume",
                                                "Name": "generated", "Source": "/docker/generated", "Destination": "/cache", "RW": True})
        if previous == "anonymous":
            state["running"]["HostConfig"] = {"Mounts": [{"Type": "volume", "Target": "/cache"}]}
            state["volume_inspections"] = {"generated": [{"Name": "generated", "Labels": {"com.docker.volume.anonymous": ""}}]}
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-test-login", "--no-cache-prune", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert stages(json.loads(state_path.read_text()))[-1] == "up"


@pytest.mark.parametrize("declared_named", [False, True], ids=["anonymous", "declared-named"])
def test_inherited_anonymous_volume_survives_second_update(tmp_path, declared_named):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode="checkout") as (repo, env, state_path):
        name = "a" * 64
        state = json.loads(state_path.read_text())
        state["config"]["services"][SERVICE]["volumes"].append({"type": "volume", "target": "/cache"})
        state["running"]["Mounts"].append({"Type": "volume", "Name": name,
            "Source": f"/var/lib/docker/volumes/{name}/_data", "Destination": "/cache", "RW": True})
        state["running"]["HostConfig"] = {"Mounts": [{"Type": "volume", "Target": "/cache"}]}
        state["volume_inspections"] = {name: [{"Name": name, "Labels": {"com.docker.volume.anonymous": ""}}]}
        state_path.write_text(json.dumps(state))
        args = ["--checkout", str(repo), "--json", "--no-test-login", "--no-cache-prune", "--health-retries", "1"]
        first = run_arm(repo, env, binary, args)
        assert first.returncode == 0, first.stdout + first.stderr
        state = json.loads(state_path.read_text())
        assert stages(state)[-1] == "up"
        # Docker's second-generation inspect names the inherited anonymous
        # volume even though Compose still declares only its destination.
        state["running"]["HostConfig"]["Mounts"][0]["Source"] = name
        if declared_named:
            state["config"]["volumes"]["cache"] = {"name": name}
        state["calls"] = []
        state_path.write_text(json.dumps(state))
        second = run_arm(repo, env, binary, args)
        assert second.returncode == (2 if declared_named else 0), second.stdout + second.stderr
        calls = stages(json.loads(state_path.read_text()))
        if declared_named:
            assert "/cache" in second.stdout and "restore" in second.stdout
            assert calls == []
        else:
            assert calls[-1] == "up"
            third = run_arm(repo, env, binary, args)
            assert third.returncode == 0, third.stdout + third.stderr
            assert stages(json.loads(state_path.read_text())).count("up") == 2


def test_nonexistent_release_does_not_recommend_another_updater(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(missing_sums=True, missing_release=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "9.9.9", "--daemon-only", "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "9.9.9" in result.stdout and "published" in result.stdout
        assert "Python updater" not in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("mode", ["checkout", "release", "engine"])
@pytest.mark.parametrize("evidence", ["external", "inspect-failed", "malformed", "wrong-name", "missing-name", "unresolved-named-set"])
def test_anonymous_mount_requires_docker_volume_provenance(tmp_path, mode, evidence):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, mode=mode) as (repo, env, state_path):
        name = "a" * 64
        state = json.loads(state_path.read_text())
        state["config"]["services"][SERVICE]["volumes"].append({"type": "tmpfs", "target": "/cache"})
        state["running"]["Mounts"].append({"Type": "volume", "Name": name,
            "Source": f"/var/lib/docker/volumes/{name}/_data", "Destination": "/cache", "RW": True})
        configured = {"Type": "volume", "Target": "/cache", "Source": name}
        state["running"]["HostConfig"] = {"Mounts": [configured]}
        record = {"Name": name, "Labels": {"com.docker.volume.anonymous": ""}}
        if evidence == "external":
            record["Labels"] = {}
        elif evidence == "inspect-failed":
            state["volume_inspect_failure"] = True
        elif evidence == "wrong-name":
            record["Name"] = "another-volume"
        elif evidence == "missing-name":
            state["running"]["Mounts"][-1].pop("Name")
            configured.pop("Source")
        elif evidence == "unresolved-named-set":
            state["config"]["volumes"]["unresolved"] = {}
            configured.pop("Source")
        state["volume_inspections"] = {name: {} if evidence == "malformed" else [record]}
        state_path.write_text(json.dumps(state))
        args = ["--json", "--daemon-only", "--health-retries", "1"]
        if mode == "release":
            args += ["--tag", "0.21.1"]
        else:
            args += ["--checkout", str(repo), "--no-test-login", "--no-cache-prune"]
            if mode == "engine":
                args += ["--engine", "rust"]
        result = run_arm(repo, env, binary, args)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "/cache" in result.stdout and "declare" in result.stdout and "recreate" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []
