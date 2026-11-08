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
def models_overlay(s,files):
 # The Rust overlay that carries the models volume at /models: the checkout
 # one always, the release one when the row models it (models_overlay).
 return any(v.endswith("docker-compose.rust-daemon.yml") for v in files) or bool(
  s.get("models_overlay") and any(v.endswith("docker-compose.rust-ghcr.yml") for v in files))
s["calls"].append(a)
code=0;out=""
if a[:1]==["inspect"]:
 if "--format" in a: out=s["running"]["Image"]
 elif "-f" in a:
  f=a[a.index("-f")+1]
  if "State.Running" in f:
   if a[-1]=="missing-daemon":code=1;out="No such container"
   else:out=("false" if s["failure"]=="recreated_stopped" else "true") if s.get("deployed") else str(s["running"].get("State",{}).get("Running",True)).lower()
  elif "State.Status" in f:
   out={"recreated_stopped":"exited","recreated_restarting":"restarting"}.get(s["failure"],"running") if s.get("deployed") else "running"
  elif "index .Config.Labels" in f: out=s.get("setup_engine","python")
  elif "Labels" in f: out=json.dumps(s["running"]["Config"]["Labels"])
  elif "Config.Env" in f: out=json.dumps(s["running"]["Config"]["Env"])
  elif "|" in f: out=s["running"]["Image"]+"|"+s["running"]["Config"]["Image"]
  elif f=="{{json .}}":out=json.dumps(s["running"])
  else: out=s["running"]["Id"]
 else: out=json.dumps([s["running"]] if a[-1]!="fixture-pg" else [{"Name":"/fixture-pg","NetworkSettings":{"Networks":{"fixture":{"Aliases":["pseudolife-pg"]}}}}])
elif a[:2]==["image","inspect"] and a[-1] in s.get("orphaned",[]):code=1;out="Error: No such image: "+a[-1]
elif a[:1] in (["run"],["create"]) and any(v in s.get("orphaned",[]) for v in a):
 # The containerd image store: an ID no name refers to any more no longer resolves.
 code=1;out="Error response from daemon: No such image"
elif a[:2]==["image","inspect"]:
 if "Created" in " ".join(a): out="2026-01-01|"+s["images"].get(a[-1],s["running"]["Image"])
 elif a[-1] in s["images"]: out=s["images"][a[-1]]
 elif s.get("image_inspect_error"): code=1;out="Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
 else: code=1;out="Error response from daemon: No such image: "+a[-1]
elif a[:2]==["image","ls"]: out="\n".join(s["images"]) if s.get("list_images") else ""
elif a[:1]==["port"]:out="127.0.0.1:9876" if a[-1]=="8765/tcp" else "127.0.0.1:5433"
elif a[:2]==["volume","inspect"]:
 if s.get("volume_inspect_failure"):code=1
 else:out=json.dumps(s.get("volume_inspections",{}).get(a[-1],[]))
elif a[:1]==["ps"]: out=s["running"]["Id"]
elif a[:1]==["pull"]: code=int(s["failure"]=="pull" or (bool(s.get("rust_pull_fails")) and "-rust:" in a[-1]) or (bool(s.get("python_pull_fails")) and "-rust:" not in a[-1]))
elif a[:2]==["manifest","inspect"]: code=0 if s.get("rust_published") else 1
elif a[:1]==["run"]:
 if "check-config" in a:
  code=s.get("check_config",0)
  out={3:"unsupported memory.reranker.model_name 'BAAI/bge-reranker-base': the native daemon supports only cross-encoder/ms-marco-MiniLM-L-6-v2; set memory.reranker.model_name to that model, or keep the Python engine for a different reranker",2:"storage resolution refused: the Rust daemon requires PSEUDOLIFE_MCP_DATABASE_URL"}.get(code,"the native daemon supports the configuration in /data/config.yaml")
 elif "provision" in a: code=int(s["failure"]=="provision")
 else: out="58" if "--schema-version" in a else "SCHEMA_META_VERSION = 58\n"
elif a[:1]==["tag"]:
 # The containerd image store: an ID no name refers to any more no longer resolves.
 if a[-2] in s.setdefault("orphaned",[]):code=1;out="Error response from daemon: No such image: "+a[-2]
 else:s["images"][a[-1]]=s["images"].get(a[-2],a[-2])
elif a[:1]==["compose"]:
 if "config" in a and any(not pathlib.Path(a[i+1]).is_file() for i,v in enumerate(a[:-1]) if v=="-f"):
  code=1;out="open docker-compose file: no such file or directory"
 elif "config" in a:
  config=json.loads(json.dumps(s["config"]))
  if s.get("relative_mount"):
   directory=pathlib.Path(a[a.index("--project-directory")+1]) if "--project-directory" in a else pathlib.Path(a[a.index("-f")+1]).parent
   config["services"]["pseudolife-daemon"]["volumes"].append({"type":"bind","source":str(directory/"config.yaml"),"target":"/data/config.yaml","read_only":True})
  if models_overlay(s,a):
   # A Rust overlay's model cache, as Compose renders it.
   daemon=config["services"]["pseudolife-daemon"]
   daemon["environment"]["PSEUDOLIFE_DAEMON_MODELS_DIR"]="/models"
   daemon["volumes"].append({"type":"volume","source":"pseudolife_rust_models","target":"/models"})
   config["volumes"]["pseudolife_rust_models"]={"name":"fixture-project_pseudolife_rust_models"}
  out=json.dumps(config)
 elif "ps" in a:
  out="fixture-pg" if a[-1]=="pseudolife-pg" else s["running"]["Id"]
  if s["failure"]=="missing_project_daemon" and a[-1]=="pseudolife-daemon":out=""
  if s["failure"]=="ambiguous_project_daemon" and a[-1]=="pseudolife-daemon":out="first\nsecond"
  if s.get("deployed") and s["failure"]=="recreated_missing" and a[-1]=="pseudolife-daemon":out=""
 elif "build" in a and s["failure"]=="build":code=1
 elif "build" in a:
  # The build moves the candidate's tag; the image it left, if no other
  # name refers to it, is no longer resolvable by ID (containerd store).
  for path in [a[i+1] for i,v in enumerate(a[:-1]) if v=="-f"]:
   try:name=json.loads(pathlib.Path(path).read_text())["services"]["pseudolife-daemon"]["image"]
   except (OSError,ValueError,KeyError):continue
   previous=s["images"].get(name);s["images"][name]="sha256:built-"+str(len(s["calls"]))
   if previous and previous not in s["images"].values():s.setdefault("orphaned",[]).append(previous)
 elif "up" in a:
  code=int(s["failure"]=="up")
  if code==0:
   s["deployed"]=True
   built=""
   overlays=[a[i+1] for i,v in enumerate(a[:-1]) if v=="-f"]
   for path in overlays:
    try:
     service=json.loads(pathlib.Path(path).read_text())["services"]["pseudolife-daemon"]
     if "labels" in service:s["running"]["Config"]["Labels"]={**(s["running"]["Config"]["Labels"] or {}),**service["labels"]}
     built=s["images"].get(service.get("image"),"")
     if service.get("labels",{}).get("org.pseudolife.engine")=="python" and not s.get("import_stuck"):s["imported"]=True
    except (OSError,ValueError,KeyError):pass
   s["running"]["Image"]=built if built.startswith("sha256:built-") else "sha256:replacement"
   # A Rust overlay's model directory and volume, as the recreated container
   # has them: the checkout overlay always, the release one under models_overlay.
   env=[e for e in s["running"]["Config"]["Env"] if not e.startswith("PSEUDOLIFE_DAEMON_MODELS_DIR=")]
   mounts=[m for m in s["running"]["Mounts"] if m.get("Destination")!="/models"]
   if models_overlay(s,overlays):
    env.append("PSEUDOLIFE_DAEMON_MODELS_DIR=/models")
    mounts.append({"Type":"volume","Name":"fixture-project_pseudolife_rust_models","Destination":"/models","RW":True})
   s["running"]["Config"]["Env"]=env;s["running"]["Mounts"]=mounts
   s["running"]["Id"]=s.get("replacement_id",s["running"]["Id"])
elif a[:1]==["exec"]:
 if "df" in a: out="Filesystem 1024-blocks Used Available Capacity Mounted on\n"+("overlay 99999999 1 1 99% /" if s.get("low_disk") else "overlay 99999999 1 99999998 1% /")
 elif "--host-state" in a or "pseudolife_memory.maintainer_setup" in a:
  out=s.get("setup_reply",json.dumps({"problem":None,"keys":[{"state":"active"}]}))
 elif "psql" in a:
  if "cms_counters" in a[-1]:out="f" if s["failure"]=="pending_import" and not s.get("imported") else "t"
  else:out="56" if s["failure"]=="old_schema" and not s.get("imported") else "unreadable" if s["failure"]=="unreadable_schema" else "58"
 elif any("test ! -e /data/memory_state/weights.pt" in v for v in a):code=int(s["failure"] in ("pending_import","imported_weights") and not s.get("imported"))
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


@pytest.mark.parametrize("path", ["/", "/tmp"])
def test_fixture_df_emits_one_terminated_filesystem_record(tmp_path, monkeypatch, capsys, path):
    state_path = tmp_path / "fixture-state.json"
    state_path.write_text(json.dumps({"calls": []}))
    args = ["exec", "fixture-pg", "df", "-Pk", path]
    monkeypatch.setenv("UPDATE_FIXTURE_STATE", str(state_path))
    monkeypatch.setattr(sys, "argv", ["docker", *args])
    with pytest.raises(SystemExit) as stopped:
        exec(compile(DOCKER, "fixture-docker", "exec"), {})
    assert stopped.value.code == 0
    assert capsys.readouterr().out == (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "overlay 99999999 1 99999998 1% /\n"
    )
    assert json.loads(state_path.read_text())["calls"] == [args]


@pytest.mark.parametrize("running,deployed,failure,expected", [
    (True, False, "", "true"), (False, False, "", "false"),
    (False, True, "", "true"), (True, True, "recreated_stopped", "false"),
])
def test_fixture_running_format_preserves_pre_recreate_state(tmp_path, monkeypatch, capsys, running, deployed, failure, expected):
    state_path = tmp_path / "fixture-state.json"
    state_path.write_text(json.dumps({"calls": [], "running": {"State": {"Running": running}},
                                      "deployed": deployed, "failure": failure}))
    args = ["inspect", "-f", "{{.State.Running}}", "fixture-daemon"]
    monkeypatch.setenv("UPDATE_FIXTURE_STATE", str(state_path))
    monkeypatch.setattr(sys, "argv", ["docker", *args])
    with pytest.raises(SystemExit) as stopped:
        exec(compile(DOCKER, "fixture-docker", "exec"), {})
    assert stopped.value.code == 0
    assert capsys.readouterr().out == expected + "\n"


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
             "volume_inspections": {"fixture-state": [{"Name": "fixture-state"}]},
             "running": {"Id": "fixture-daemon", "Image": "sha256:validated", "State": {"Running": True},
                         "NetworkSettings": {"Networks": {"fixture": {"Aliases": ["pseudolife-pg"]}}},
                         "Mounts": [{"Destination": "/data", "Name": "fixture-state", "Type": "volume", "RW": True}],
                         "Config": {"Image": "pseudolife-daemon:0.21.0", "Labels": None,
                                    "Env": ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://fixture:fixture@pseudolife-pg:5432/fixture",
                                            "PSEUDOLIFE_MCP_TOKEN=fixture-bearer"]}}}

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
                                   "schema": (59 if failure == "schema" else 58) if deployed else current.get("running_schema", 58),
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
                active = not current.get("no_board_peer") and any(a[:1] == ["fixture-backup"] for a in current["calls"])
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


def _report_text(value):
    """Every string in a JSON report, decoded, one per line."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        value = list(value.values())
    return "\n".join(_report_text(v) for v in value) if isinstance(value, list) else ""


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
            from psycopg.conninfo import conninfo_to_dict
            assert backup[1] == "fixture-pg" and backup[-3:-1] == ["5432", "fixture"]
            assert conninfo_to_dict(backup[-1]) == {"dbname": "fixture"}
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
@pytest.mark.parametrize("failure", ["", "build", "provision", "backup", "incomplete", "up", "health", "version", "schema", "read"])
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
        # The rollback tag precedes the build (which may move the running
        # image's name); the build and the model provisioning precede the
        # backup and the recreate.
        expected = (["tag", "build"] if failure in ("build", "provision") else ["tag", "build", "backup"]
                    if failure in ("backup", "incomplete") else ["tag", "build", "backup", "up"])
        assert observed == expected
        provisions = [i for i, a in enumerate(state["calls"]) if a[:1] == ["run"] and "provision" in a]
        if failure == "build":
            assert provisions == []
        else:
            build = next(i for i, a in enumerate(state["calls"]) if a[:1] == ["compose"] and "build" in a)
            assert len(provisions) == 1 and build < provisions[0]
            run = state["calls"][provisions[0]]
            # Exactly the overlay's model directory and volume the replacement runs with.
            assert run == ["run", "--rm", "-e", "PSEUDOLIFE_DAEMON_MODELS_DIR=/models",
                           "--mount", "type=volume,src=fixture-project_pseudolife_rust_models,dst=/models",
                           "--entrypoint", "pseudolife-daemon", "pseudolife-daemon-rust:checkout",
                           "provision", "models", "--if-missing"]
            create = next(i for i, a in enumerate(state["calls"]) if a[:2] == ["volume", "create"])
            assert create < provisions[0]
            assert state["calls"][create][-1] == "fixture-project_pseudolife_rust_models"
            assert "com.docker.compose.volume=pseudolife_rust_models" in state["calls"][create]
            later = [i for i, a in enumerate(state["calls"]) if a[:1] == ["fixture-backup"]
                     or a[:1] == ["exec"] and any("pg_dump" in s for s in a)]
            assert all(provisions[0] < i for i in later)
        selection = repo / "data/update-engine/docker-compose.engine.json"
        assert selection.exists() is (not failure)
        assert report["rollback"]["tag"] == "pseudolife-daemon-python:0.21.0-pre-fixture"
        assert report["rollback"]["state"] == "tagged"
        if failure in ("build", "provision", "backup", "incomplete"):
            assert "nothing was deployed" in result.stdout
            assert "the running image keeps its rollback tag pseudolife-daemon-python:0.21.0-pre-fixture" in result.stdout
            # Only a build that succeeded moved :checkout to an unvalidated image.
            assert ("pseudolife-daemon-rust:checkout now names the unvalidated build" in result.stdout) \
                is (failure != "build")
            assert "to roll back:" not in result.stdout
        else:
            assert Path(report["rollback"]["overlay"]).is_file()
            assert report["rollback"]["state"] == "tagged"
        if failure in ("health", "version", "schema", "read"):
            reason = {"health": '/health reported status "error" instead of "ok"',
                      "version": 'version "wrong" instead of "0.21.1"', "schema": "schema 59 instead of 58",
                      "read": "the authenticated bank read was refused"}[failure]
            text = _report_text(report)
            assert reason in text and "last of 1 checks" in text, text
        if failure in ("health", "version", "schema", "read"):
            retry = run_arm(repo, env, binary, args)
            assert retry.returncode == 2, retry.stdout + retry.stderr
            assert stages(json.loads(state_path.read_text())) == observed
            assert "rollback" in retry.stdout.lower()


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_same_engine_checkout_rerun_tags_the_running_image_before_rebuilding_it(tmp_path, native):
    """Docker's containerd image store no longer resolves an image's ID once
    a build moved the only tag naming it: a Rust-to-Rust rerun must tag the
    running image for rollback before it rebuilds :checkout."""
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "", "engine") as (repo, env, state_path):
        args = ["--checkout", str(repo), "--engine", "rust", "--no-test-login", "--no-cache-prune", "--json",
                "--health-retries", "1", "--health-delay-ms", "0"]
        first = run_arm(repo, env, binary, [*args, "--rollback-tag", "pre-first"])
        assert first.returncode == 0, first.stdout + first.stderr
        state = json.loads(state_path.read_text())
        running = state["running"]["Image"]
        assert running == state["images"]["pseudolife-daemon-rust:checkout"]
        before = len(state["calls"])
        second = run_arm(repo, env, binary, [*args, "--rollback-tag", "pre-second"])
        assert second.returncode == 0, second.stdout + second.stderr
        state = json.loads(state_path.read_text())
        calls = state["calls"][before:]
        assert stages({"calls": calls}) == ["tag", "build", "backup", "up"]
        tag = next(a for a in calls if a[:1] == ["tag"])
        assert tag == ["tag", running, "pseudolife-daemon-rust:0.21.1-pre-second"]
        assert state["images"]["pseudolife-daemon-rust:0.21.1-pre-second"] == running
        assert running not in state["orphaned"]
        assert state["running"]["Image"] != running


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_checkout_switch_back_to_python_leaves_the_rust_model_volume(tmp_path, native):
    """The Rust daemon's /models volume comes from the checkout overlay;
    switching back to Python may leave that cache, and is not refused as a
    dropped persistent mount."""
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "", "engine") as (repo, env, state_path):
        common = ["--checkout", str(repo), "--no-test-login", "--no-cache-prune", "--json",
                  "--health-retries", "1", "--health-delay-ms", "0"]
        first = run_arm(repo, env, binary, [*common, "--engine", "rust", "--rollback-tag", "pre-rust"])
        assert first.returncode == 0, first.stdout + first.stderr
        assert any(m["Destination"] == "/models" for m in json.loads(state_path.read_text())["running"]["Mounts"])
        back = run_arm(repo, env, binary, [*common, "--engine", "python", "--rollback-tag", "pre-python"])
        assert back.returncode == 0, back.stdout + back.stderr
        state = json.loads(state_path.read_text())
        assert not any(m["Destination"] == "/models" for m in state["running"]["Mounts"])
        assert state["running"]["Config"]["Labels"]["org.pseudolife.engine"] == "python"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("target", ["rust", "python"])
def test_missing_checkout_rust_overlay_is_refused_before_any_tag(tmp_path, native, target):
    """A Rust checkout without ops/docker-compose.rust-daemon.yml cannot be
    held to its model volume: both a rerun and a switch to Python refuse
    before the rollback tag, rather than tag and then fail (or report a
    dropped mount)."""
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "", "engine") as (repo, env, state_path):
        common = ["--checkout", str(repo), "--no-test-login", "--no-cache-prune", "--json",
                  "--health-retries", "1", "--health-delay-ms", "0"]
        first = run_arm(repo, env, binary, [*common, "--engine", "rust", "--rollback-tag", "pre-rust"])
        assert first.returncode == 0, first.stdout + first.stderr
        (repo / "ops/docker-compose.rust-daemon.yml").unlink()
        state = json.loads(state_path.read_text())
        before, images = len(state["calls"]), dict(state["images"])
        selection = repo / "data/update-engine/docker-compose.engine.json"
        accepted = selection.read_bytes()
        second = run_arm(repo, env, binary, [*common, "--engine", target, "--rollback-tag", "pre-second"])
        assert second.returncode == 2, second.stdout + second.stderr
        assert "cannot resolve the Compose project" in second.stdout and "nothing was changed" in second.stdout
        after = json.loads(state_path.read_text())
        assert stages({"calls": after["calls"][before:]}) == []
        assert after["images"] == images and selection.read_bytes() == accepted


@pytest.mark.parametrize("target", ["rust", "python"])
def test_native_checkout_still_refuses_an_undeclared_mount_beside_the_rust_model_volume(tmp_path, target):
    """The overlay supplies only the /models volume: a running Rust daemon's
    other mount that no Compose file declares is still a dropped mount, on a
    rerun and on a switch to Python, and nothing changes."""
    binary = _native_binary()
    if not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "", "engine") as (repo, env, state_path):
        common = ["--checkout", str(repo), "--no-test-login", "--no-cache-prune", "--json",
                  "--health-retries", "1", "--health-delay-ms", "0"]
        first = run_arm(repo, env, binary, [*common, "--engine", "rust", "--rollback-tag", "pre-rust"])
        assert first.returncode == 0, first.stdout + first.stderr
        state = json.loads(state_path.read_text())
        assert any(m["Destination"] == "/models" for m in state["running"]["Mounts"])
        state["running"]["Mounts"].append({"Destination": "/srv/x", "Source": str(tmp_path / "extra"),
                                           "Type": "bind", "RW": True})
        state_path.write_text(json.dumps(state))
        selection = repo / "data/update-engine/docker-compose.engine.json"
        accepted = selection.read_bytes()
        before = len(state["calls"])
        images = dict(state["images"])
        second = run_arm(repo, env, binary, [*common, "--engine", target, "--rollback-tag", "pre-second"])
        assert second.returncode == 2, second.stdout + second.stderr
        assert "the Compose configuration drops a daemon mount" in second.stdout
        assert "nothing was changed" in second.stdout
        after = json.loads(state_path.read_text())
        assert stages({"calls": after["calls"][before:]}) == []
        assert after["images"] == images
        assert selection.read_bytes() == accepted
        assert not (repo / "data/update-engine/docker-compose.python-accepted.json").exists()


@pytest.mark.parametrize("models", ["volume", "legacy"])
def test_rust_to_rust_rollback_keeps_the_model_directory_the_daemon_ran_with(tmp_path, models):
    """Both updaters print the same restore command for a failed Rust-to-Rust
    rerun: with the checkout overlay's /models volume when the running daemon
    uses it, without it for one that read /data/models."""
    import re
    binary = _native_binary()
    if not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
    files = {}
    for arm, native in (("python", None), ("native", binary)):
        with host(tmp_path / arm, "", "engine") as (repo, env, state_path):
            args = ["--checkout", str(repo), "--engine", "rust", "--no-test-login", "--no-cache-prune", "--json",
                    "--health-retries", "1", "--health-delay-ms", "0"]
            first = run_arm(repo, env, native, [*args, "--rollback-tag", "pre-first"])
            assert first.returncode == 0, first.stdout + first.stderr
            state = json.loads(state_path.read_text())
            assert "PSEUDOLIFE_DAEMON_MODELS_DIR=/models" in state["running"]["Config"]["Env"]
            assert any(m["Destination"] == "/models" for m in state["running"]["Mounts"])
            if models == "legacy":
                # A daemon an earlier checkout overlay created, on /data/models.
                state["running"]["Config"]["Env"].remove("PSEUDOLIFE_DAEMON_MODELS_DIR=/models")
                state["running"]["Mounts"] = [m for m in state["running"]["Mounts"] if m["Destination"] != "/models"]
            state["failure"] = "up"
            state_path.write_text(json.dumps(state))
            second = run_arm(repo, env, native, [*args, "--rollback-tag", "pre-second"])
            assert second.returncode == 1, second.stdout + second.stderr
            text = _report_text(json.loads(second.stdout))
            assert "to roll back:" in text
            restore = next(line for line in text.splitlines() if "docker compose" in line and " up " in line)
            # The Compose files of the recreate, not the selection copy after it.
            command = restore[restore.index("docker compose"):restore.index(" up ")]
            found = [re.sub(r"rollback-.*\.json$", "rollback.json", name)
                     for name in re.findall(r"[\w.\-]+\.(?:yml|json)", command)]
            files[arm] = found
            assert ("docker-compose.rust-daemon.yml" in found) is (models == "volume"), restore
            assert found[-1] == "docker-compose.rollback.json", restore
    assert files["python"] == files["native"]


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
        if failure in ("health", "version"):
            assert "--daemon-url http://127.0.0.1:9876" in result.stdout
            assert "image setting won" not in result.stdout
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
            assert stages(state) == ["tag", "build", "backup", "up"]
            assert not any("test ! -e /data/memory_state/weights.pt" in " ".join(a) for a in state["calls"])
        else:
            assert stages(state) == []
            assert "import" in result.stdout.lower()


def _labels(state_path):
    return json.loads(state_path.read_text())["running"]["Config"]["Labels"]


def _accepted(env):
    return Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/docker-compose.release-accepted.json"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_rust_activation_keeps_and_switches_back_on_the_same_bank(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        common = ["--daemon-only", "--json", "--health-retries", "1", "--health-delay-ms", "0"]
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", *common, "--rollback-tag", "pre-rust"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert stages(state) == ["pull", "backup", "tag", "up"]
        pull = next(a for a in state["calls"] if a[:1] == ["pull"])
        assert pull == ["pull", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"]
        provision = next(i for i, a in enumerate(state["calls"]) if a[:1] == ["run"] and "provision" in a)
        backup = next(i for i, a in enumerate(state["calls"]) if a[:1] == ["fixture-backup"])
        assert provision < backup
        assert "type=volume,src=fixture-state,dst=/data" in state["calls"][provision]
        assert "--if-missing" in state["calls"][provision]
        assert "PSEUDOLIFE_DAEMON_MODELS_DIR=/data/models" in state["calls"][provision]
        assert "--volumes-from" not in state["calls"][provision]
        up = next(a for a in state["calls"] if a[:1] == ["compose"] and "up" in a)
        assert any(v.endswith("docker-compose.rust-ghcr.yml") for v in up)
        assert "--no-build" in up and "--no-deps" in up
        tag = next(a for a in state["calls"] if a[:1] == ["tag"])
        assert tag[-1].startswith("ghcr.io/pseudogiant-xr/pseudolife-daemon:") and tag[-1].endswith("-pre-rust")
        labels = _labels(state_path)
        assert labels["org.pseudolife.engine"] == "rust" and labels["org.pseudolife.release"] == "0.21.1"
        first = labels["org.pseudolife.activation"]
        accepted = json.loads(_accepted(env).read_text())["services"][SERVICE]["labels"]
        assert accepted["org.pseudolife.activation"] == first
        report = json.loads(result.stdout)
        assert report["rollback"]["state"] == "tagged" and Path(report["rollback"]["overlay"]).is_file()

        # An unset engine keeps Rust and records a fresh accepted activation.
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--reinstall", *common, "--rollback-tag", "pre-keep"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert [a for a in state["calls"] if a[:1] == ["pull"]][-1][-1].endswith("pseudolife-daemon-rust:0.21.1")
        assert [a for a in state["calls"] if a[:1] == ["tag"]][-1][-1].startswith("ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:")
        labels = _labels(state_path)
        assert labels["org.pseudolife.engine"] == "rust" and labels["org.pseudolife.activation"] != first

        # Without --json, so the printed rollback commands can be read.
        plain = [arg for arg in common if arg != "--json"]
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "python", "--tag", "0.21.1", "--reinstall", *plain, "--rollback-tag", "pre-back"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert [a for a in state["calls"] if a[:1] == ["pull"]][-1][-1] == "ghcr.io/pseudogiant-xr/pseudolife-daemon:0.21.1"
        up = [a for a in state["calls"] if a[:1] == ["compose"] and "up" in a][-1]
        assert not any(v.endswith("docker-compose.rust-ghcr.yml") for v in up)
        assert _labels(state_path)["org.pseudolife.engine"] == "python"
        assert json.loads(_accepted(env).read_text())["services"][SERVICE]["labels"]["org.pseudolife.engine"] == "python"
        # Rolling the switch back restores Rust with its model volume overlay.
        restore = next(line for line in result.stdout.splitlines() if "--pull never" in line)
        assert "docker-compose.rust-ghcr.yml" in restore


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("failure", ["pull", "provision", "backup", "up", "health", "version", "read"])
def test_release_rust_activation_faults_never_commit_the_selection(tmp_path, native, failure):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, failure) as (repo, env, state_path):
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--health-delay-ms", "0", "--rollback-tag", "pre-fixture"])
        assert result.returncode == 1, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        expected = {"pull": ["pull"], "provision": ["pull"], "backup": ["pull", "backup"]}
        assert stages(state) == expected.get(failure, ["pull", "backup", "tag", "up"])
        assert not _accepted(env).exists()
        if failure == "pull":
            assert "--engine python" in result.stdout
        if failure in expected:
            assert not any(a[:1] == ["tag"] for a in state["calls"])
        else:
            assert json.loads(result.stdout)["rollback"]["state"] == "tagged"
            assert "rollback" in result.stdout.lower()
        if failure in ("health", "version", "read"):
            # The recreated container never passed its gate: a plain retry
            # must not treat it as an accepted Rust selection.
            retry = run_arm(repo, env, binary if native else None, ["--tag", "0.21.1", "--daemon-only", "--json"])
            assert retry.returncode == 2, retry.stdout + retry.stderr
            assert "rollback" in retry.stdout.lower()
            assert stages(json.loads(state_path.read_text())) == ["pull", "backup", "tag", "up"]


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("engine", ["", "rust"])
def test_release_refuses_to_recreate_without_project_files_or_an_env_file(tmp_path, native, engine):
    # The daemon's labels name no Compose file on this host and no env file:
    # recreating from the release's files would reset the Postgres password,
    # the volume names and the bearer. Both updaters refuse first, with the
    # same words (the G2 default's fallback warning repeats them: CLI-UPDATE
    # update-current-noop).
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["running"]["Config"]["Labels"] = {"com.docker.compose.project": "fixture-project",
                                                "com.docker.compose.service": SERVICE}
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"]
        if engine:
            args = ["--engine", engine, *args]
        result = run_arm(repo, env, binary if native else None, args)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "compose files are gone and no env file was found beside them" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_rust_models_go_to_their_own_volume_outside_the_backed_up_state(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        daemon = state["config"]["services"][SERVICE]
        daemon["environment"]["PSEUDOLIFE_DAEMON_MODELS_DIR"] = "/models"
        daemon["volumes"].append({"type": "volume", "source": "pseudolife_rust_models", "target": "/models"})
        state["config"]["volumes"]["pseudolife_rust_models"] = {"name": "fixture-project_pseudolife_rust_models"}
        state_path.write_text(json.dumps(state))
        # No --daemon-only: an engine change still moves no client.
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-models"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["clients"] is None
        calls = json.loads(state_path.read_text())["calls"]
        create = next(a for a in calls if a[:2] == ["volume", "create"])
        assert create[-1] == "fixture-project_pseudolife_rust_models"
        assert "com.docker.compose.project=fixture-project" in create
        assert "com.docker.compose.volume=pseudolife_rust_models" in create
        provision = next(a for a in calls if a[:1] == ["run"] and "provision" in a)
        assert "type=volume,src=fixture-project_pseudolife_rust_models,dst=/models" in provision
        assert "PSEUDOLIFE_DAEMON_MODELS_DIR=/models" in provision
        assert not any(v.endswith("dst=/data") for v in provision)


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("labelled", [False, True], ids=["bundled-overlay", "labelled-overlay"])
def test_release_rust_models_mount_does_not_block_a_rerun_or_a_switch_back(tmp_path, native, labelled):
    # Once the Rust daemon runs with the overlay's /models volume, a plain
    # Rust rerun keeps it and --engine python drops it on purpose: neither
    # may be refused as "the Compose configuration drops a daemon mount".
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["models_overlay"] = True
        state_path.write_text(json.dumps(state))
        common = ["--daemon-only", "--json", "--health-retries", "1", "--health-delay-ms", "0"]
        arm = binary if native else None
        first = run_arm(repo, env, arm, ["--engine", "rust", "--tag", "0.21.1", *common, "--rollback-tag", "to-rust"])
        assert first.returncode == 0, first.stdout + first.stderr
        state = json.loads(state_path.read_text())
        assert any(m["Destination"] == "/models" for m in state["running"]["Mounts"])
        if labelled:
            # Compose records the overlay it was created with: the switch-back
            # then checks against that file, not the bundled copy.
            up = next(a for a in state["calls"] if a[:1] == ["compose"] and "up" in a)
            overlay = next(v for v in up if v.endswith("docker-compose.rust-ghcr.yml"))
            labels = state["running"]["Config"]["Labels"]
            labels["com.docker.compose.project.config_files"] += "," + overlay
            state_path.write_text(json.dumps(state))
        rerun = run_arm(repo, env, arm, ["--engine", "rust", "--tag", "0.21.1", "--reinstall", *common,
                                         "--rollback-tag", "rerun"])
        assert rerun.returncode == 0, rerun.stdout + rerun.stderr
        assert "drops a daemon mount" not in rerun.stdout + rerun.stderr
        back = run_arm(repo, env, arm, ["--engine", "python", "--tag", "0.21.1", *common, "--rollback-tag", "to-python"])
        assert back.returncode == 0, back.stdout + back.stderr
        assert "drops a daemon mount" not in back.stdout + back.stderr
        state = json.loads(state_path.read_text())
        assert _labels(state_path)["org.pseudolife.engine"] == "python"
        assert not any(m["Destination"] == "/models" for m in state["running"]["Mounts"])


def test_release_native_switch_back_still_refuses_dropping_another_mount(tmp_path):
    # Near miss for the row above: only the Rust overlay's models volume may
    # go when leaving Rust. Native release mode checks every mount; the
    # Python release updater checks only /data, so this row is native-only.
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["models_overlay"] = True
        state_path.write_text(json.dumps(state))
        common = ["--daemon-only", "--json", "--health-retries", "1", "--health-delay-ms", "0"]
        first = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.21.1", *common, "--rollback-tag", "to-rust"])
        assert first.returncode == 0, first.stdout + first.stderr
        state = json.loads(state_path.read_text())
        state["running"]["Mounts"].append({"Type": "volume", "Name": "fixture-cache", "Destination": "/cache", "RW": True})
        before = len(state["calls"])
        state_path.write_text(json.dumps(state))
        back = run_arm(repo, env, binary, ["--engine", "python", "--tag", "0.21.1", *common, "--rollback-tag", "to-python"])
        assert back.returncode == 2, back.stdout + back.stderr
        assert "drops a daemon mount" in back.stdout
        state = json.loads(state_path.read_text())
        assert stages({"calls": state["calls"][before:]}) == []
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_engine_change_requires_a_backup(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--no-backup", "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "require a backup" in result.stdout + result.stderr
        assert json.loads(state_path.read_text())["calls"] == []


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("case", ["misplaced_models", "newer_bank"])
def test_release_rust_refuses_before_pulling_or_backing_up(tmp_path, native, case):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        if case == "misplaced_models":
            state["config"]["services"][SERVICE]["environment"]["PSEUDOLIFE_DAEMON_MODELS_DIR"] = "/models"
        else:
            # The running daemon reports v59; the image's probe says 58.
            state["running_schema"] = 59
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--json", "--health-retries", "1"])
        assert result.returncode == 2, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert stages(state) == ([] if case == "misplaced_models" else ["pull"])
        assert ("no persistent volume" if case == "misplaced_models" else "newer than") in result.stdout


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_rust_host_keeps_its_daemon_when_a_release_has_no_rust_image(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        common = ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"]
        first = run_arm(repo, env, binary if native else None, ["--engine", "rust", *common, "--rollback-tag", "pre-a"])
        assert first.returncode == 0, first.stdout + first.stderr
        state = json.loads(state_path.read_text())
        state["failure"] = "pull"
        before = len(state["calls"])
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None, [*common, "--reinstall", "--rollback-tag", "pre-b"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert "keeps its current Rust daemon" in result.stdout
        state = json.loads(state_path.read_text())
        assert stages({"calls": state["calls"][before:]}) == ["pull"]
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("case", ["unchosen", "unpublished", "chose-python", "chose-python-then-rust",
                                  "corrupt-choice", "binary-choice", "working-dir"])
def test_release_default_moves_only_an_unchosen_attended_host_to_rust(tmp_path, native, case):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = case != "unpublished"
        state_path.write_text(json.dumps(state))
        if case == "chose-python":
            # `--engine python` leaves an accepted python record: the host chose.
            accepted = _accepted(env)
            accepted.parent.mkdir(parents=True)
            accepted.write_text(json.dumps({"services": {SERVICE: {"image": "py", "labels": {
                "org.pseudolife.engine": "python", "org.pseudolife.activation": "chosen",
                "org.pseudolife.release": "0.21.0"}}}}))
        choice = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/engine-choice.json"
        if case == "chose-python-then-rust":
            choice.parent.mkdir(parents=True, exist_ok=True)
            choice.write_text('{"engine": "python"}\n')
        if case == "corrupt-choice":
            choice.parent.mkdir(parents=True, exist_ok=True)
            choice.write_text("{not json")
        if case == "binary-choice":
            # Not UTF-8 (Codex, round 3 delta): a warning, never a crash.
            choice.parent.mkdir(parents=True, exist_ok=True)
            choice.write_bytes(b"\xff\xfe{\x00")
        if case == "working-dir":
            state = json.loads(state_path.read_text())
            state["config"]["services"][SERVICE]["working_dir"] = "/data"
            state_path.write_text(json.dumps(state))
        args = ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1", "--rollback-tag", "pre-default"]
        if case == "chose-python-then-rust":
            # A named engine beats a recorded choice.
            args = ["--engine", "rust", *args]
        result =run_arm(repo, env, binary if native else None, args)
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        pulled = [a[-1] for a in state["calls"] if a[:1] == ["pull"]]
        if case in ("unchosen", "chose-python-then-rust", "working-dir"):
            assert pulled == ["ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"]
            assert _labels(state_path)["org.pseudolife.engine"] == "rust"
            assert ("default since G2" in result.stdout) == (case != "chose-python-then-rust")
            # The configuration check ran on the target image before the
            # models were provisioned and before the backup.
            calls = state["calls"]
            check = next(i for i, a in enumerate(calls) if a[:1] == ["run"] and "check-config" in a)
            provision = next(i for i, a in enumerate(calls) if a[:1] == ["run"] and "provision" in a)
            backup = next(i for i, a in enumerate(calls) if a[:1] == ["fixture-backup"])
            assert check < provision < backup
            # The daemon's own working directory, for relative model paths.
            assert (calls[check][4:6] == ["-w", "/data"]) == (case == "working-dir"), calls[check]
            assert "--network" in calls[check] and "none" in calls[check]
            assert "type=volume,src=fixture-state,dst=/data,readonly=true" in calls[check]
            assert not any("TOKEN" in v or "DATABASE_URL" in v for v in calls[check])
        else:
            assert pulled == ["ghcr.io/pseudogiant-xr/pseudolife-daemon:0.21.1"]
            assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"
            if case == "unpublished":
                assert "could not confirm a published rust daemon image" in result.stdout
            if case in ("corrupt-choice", "binary-choice"):
                assert "cannot read the engine choice" in result.stdout
                assert "default since G2" not in result.stdout


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_a_fallback_never_removes_an_image_a_docker_error_hid(tmp_path, native):
    # `docker image inspect` failed for another reason than "No such image":
    # the image may have been there before this run, so it stays.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "missing_state") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        state["image_inspect_error"] = True
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-inspect"])
        assert result.returncode == 0, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        assert any(a[:2] == ["image", "inspect"] and "-rust:" in a[-1] for a in calls)
        assert "not moving to the rust daemon this time" in result.stdout
        assert not any(a[:2] == ["image", "rm"] for a in calls), calls


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_a_fallback_after_provisioning_removes_the_model_volume_it_created(tmp_path, native):
    # The move provisioned the overlay's models volume, then the state backup
    # refused: falling back removes the volume and the image it added.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "missing_state") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        state["models_overlay"] = True
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-volume"])
        assert result.returncode == 0, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        created = [a[-1] for a in calls if a[:2] == ["volume", "create"]]
        assert created == ["fixture-project_pseudolife_rust_models"], created
        assert ["volume", "rm", "fixture-project_pseudolife_rust_models"] in calls
        assert ["image", "rm", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"] in calls
        # The check never mounted the volume before it existed, so Docker
        # never created it unlabelled.
        check = next(a for a in calls if a[:1] == ["run"] and "check-config" in a)
        assert not any("pseudolife_rust_models" in v for v in check)
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("flag", ["--unattended", "--allow-auth-change"])
def test_release_default_never_applies_to_unattended_or_auth_change_runs(tmp_path, native, flag):
    # Maintainer, 2026-11-07: only the scheduled --unattended run keeps the
    # engine; a run without a terminal does move an unchosen host.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        state["no_board_peer"] = True  # an idle board: the unattended run proceeds
        state_path.write_text(json.dumps(state))
        if flag == "--unattended":
            # Its own run shape: it reads the board with the bearer and takes no --daemon-only.
            env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
            args = [flag, "--tag", "0.21.1", "--json", "--health-retries", "1", "--rollback-tag", "pre-flag"]
        else:
            # The arguments --allow-auth-change requires, so the update itself runs.
            args = [flag, "--env-file", str(repo / "ops/.env"), "--tag", "0.21.1", "--reinstall", "--daemon-only",
                    "--json", "--health-retries", "1", "--rollback-tag", "pre-flag"]
        result = run_arm(repo, env, binary if native else None, args)
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        # The update ran, on the Python daemon, without asking about Rust.
        assert "up" in stages(state), stages(state)
        assert not any(a[:1] == ["pull"] and "-rust:" in a[-1] for a in state["calls"]), result.stdout
        assert not any(a[:2] == ["manifest", "inspect"] for a in state["calls"])
        assert "default since G2" not in result.stdout
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("case", ["unsupported", "uncheckable"])
def test_release_default_keeps_python_for_a_configuration_rust_refuses(tmp_path, native, case):
    # Codex P1 (round 3): the Rust daemon refuses a non-default reranker or an
    # embedding model it cannot load at startup, so the move checks the bank's
    # configuration against the target image before the backup.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        state["check_config"] = 3 if case == "unsupported" else 2
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-check"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        calls = state["calls"]
        check = next(i for i, a in enumerate(calls) if a[:1] == ["run"] and "check-config" in a)
        # No backup or tag before the fallback, and the pulled image is removed.
        assert not any(a[:1] in (["fixture-backup"], ["tag"]) for a in calls[:check])
        assert ["image", "rm", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"] in calls[check:]
        pulled = [a[-1] for a in calls if a[:1] == ["pull"]]
        assert pulled[-1] == "ghcr.io/pseudogiant-xr/pseudolife-daemon:0.21.1"
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"
        warning = next(line for line in result.stdout.splitlines() if "not moving to the rust daemon" in line)
        if case == "unsupported":
            # The daemon's own named error, its fix included.
            assert "memory.reranker.model_name" in warning and "keep the Python engine" in warning
        else:
            assert "could not check this bank's configuration" in warning


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("case", ["unsupported", "uncheckable"])
def test_explicit_rust_refuses_an_unsupported_configuration_before_the_backup(tmp_path, native, case):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["check_config"] = 3 if case == "unsupported" else 2
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-explicit"])
        state = json.loads(state_path.read_text())
        if case == "unsupported":
            assert result.returncode == 2, result.stdout + result.stderr
            assert "does not support this bank's configuration" in result.stdout
            assert "memory.reranker.model_name" in result.stdout
            assert "backup" not in stages(state)
            assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"
        else:
            # An image that cannot check (an older one) leaves the check to the gate.
            assert result.returncode == 0, result.stdout + result.stderr
            assert "could not check this bank's configuration" in result.stdout
            assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("case", ["after-switch", "nothing-to-do"])
def test_a_failed_python_choice_write_only_warns(tmp_path, native, case):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        if case == "nothing-to-do":
            state["target_version"] = "0.21.0"
        state_path.write_text(json.dumps(state))
        common = ["--daemon-only", "--json", "--health-retries", "1", "--health-delay-ms", "0"]
        arm = binary if native else None
        if case == "after-switch":
            first = run_arm(repo, env, arm, ["--engine", "rust", "--tag", "0.21.1", *common, "--rollback-tag", "to-rust"])
            assert first.returncode == 0, first.stdout + first.stderr
        # A non-empty directory where the choice file belongs: no write can replace it.
        blocker = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/engine-choice.json"
        blocker.mkdir(parents=True, exist_ok=True)
        (blocker / "keep").write_text("x")
        tag = "0.21.1" if case == "after-switch" else "0.21.0"
        back = run_arm(repo, env, arm, ["--engine", "python", "--tag", tag, *common, "--rollback-tag", "to-python"])
        assert back.returncode == 0, back.stdout + back.stderr
        assert "choice could not be recorded" in back.stdout


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("case", ["tokenless", "no-backup", "rust-pull-fails", "state-backup-refused"])
def test_release_default_never_blocks_an_ordinary_update(tmp_path, native, case):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    failure = {"state-backup-refused": "missing_state"}.get(case, "")
    with host(tmp_path, failure) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        state["rust_pull_fails"] = case == "rust-pull-fails"
        if case == "tokenless":
            # A --no-token install: the Rust move refuses before any change.
            state["config"]["services"][SERVICE]["environment"].pop("PSEUDOLIFE_MCP_TOKEN")
            state["running"]["Config"]["Env"] = [e for e in state["running"]["Config"]["Env"]
                                                 if not e.startswith("PSEUDOLIFE_MCP_TOKEN=")]
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1", "--rollback-tag", "pre-ordinary"]
        if case == "no-backup":
            args.append("--no-backup")
        result = run_arm(repo, env, binary if native else None, args)
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        pulled = [a[-1] for a in state["calls"] if a[:1] == ["pull"]]
        assert pulled[-1] == "ghcr.io/pseudogiant-xr/pseudolife-daemon:0.21.1", pulled
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"
        if case != "no-backup":
            assert "not moving to the rust daemon this time" in result.stdout
            # The refusal's whole first line, its one fix included.
            warning = next(line for line in result.stdout.splitlines() if "not moving to the rust daemon" in line)
            fix = {"tokenless": "set PSEUDOLIFE_MCP_TOKEN", "rust-pull-fails": "keeps its Python daemon",
                   "state-backup-refused": "state"}[case]
            assert fix in warning, warning
            assert "keeps its current Rust daemon" not in warning
            assert "next attended update tries again" in result.stdout
            removed = ["image", "rm", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"] in state["calls"]
            # Only a pull that succeeded leaves an image to remove.
            assert removed == (case == "state-backup-refused")


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("case", ["supported", "unsupported"])
def test_release_default_imports_legacy_counters_unless_rust_refuses_the_configuration(tmp_path, native, case):
    # With G2-K, a defaulted move on a bank whose counters still need the
    # Python daemon's import takes the import leg, then moves to Rust. The
    # configuration check runs first, so a configuration Rust refuses falls
    # back before the Python leg changes anything.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        if case == "unsupported":
            state["check_config"] = 3
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-default-import"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        calls = state["calls"]
        check = next(i for i, a in enumerate(calls) if a[:1] == ["run"] and "check-config" in a)
        assert not any(a[:1] == ["fixture-backup"] for a in calls[:check])
        if case == "supported":
            assert stages(state) == ["pull", "pull", "backup", "tag", "up", "pull", "backup", "tag", "up"]
            assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        else:
            # No Python leg: the ordinary update runs once, on Python.
            assert stages({"calls": calls[check:]}) == ["pull", "backup", "tag", "up"]
            assert ["image", "rm", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"] in calls
            assert "memory.reranker.model_name" in result.stdout
            assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("outcome", ["moved", "rust-leg-fails"])
def test_an_import_leg_update_moves_the_clients_once_after_the_rust_gate(tmp_path, native, outcome):
    # Codex (review of the merge): an ordinary update that defaults to Rust
    # through the import leg updated the clients in the Python leg, before
    # the Rust leg's gate, and again at the end.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        state["import_stuck"] = outcome == "rust-leg-fails"
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--json", "--health-retries", "1", "--rollback-tag", "pre-clients"])
        report = json.loads(result.stdout)
        client_steps = [s for s in report["steps"] if s.startswith("updating the client side")]
        if outcome == "moved":
            assert result.returncode == 0, result.stdout + result.stderr
            assert len(client_steps) == 1, report["steps"]
            # After the Rust leg's recreation, not between the legs.
            assert report["steps"].index(client_steps[0]) > max(
                i for i, s in enumerate(report["steps"]) if "pulling ghcr.io/pseudogiant-xr/pseudolife-daemon:" in s)
        else:
            assert result.returncode == 1, result.stdout + result.stderr
            assert client_steps == [], report["steps"]


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("failure", ["python-leg", "rust-leg"])
def test_a_failed_import_leg_of_a_defaulted_move_is_the_updates_error(tmp_path, native, failure):
    # Once the import leg starts, the host is changing: a failure in either
    # leg is the update's error in both updaters, never a fallback, and the
    # leg's own --engine python is not recorded as the operator's choice.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        if failure == "python-leg":
            state["python_pull_fails"] = True
        else:
            state["import_stuck"] = True
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-leg-failure"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert "not moving to the rust daemon this time" not in result.stdout
        choice = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/engine-choice.json"
        assert not choice.exists(), choice.read_text()
        calls = json.loads(state_path.read_text())["calls"]
        # No discard of a pulled image: the host is not falling back.
        assert not any(a[:2] == ["image", "rm"] for a in calls), calls
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_an_unreadable_choice_record_still_keeps_the_host_engine(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        state_path.write_text(json.dumps(state))
        # A directory where the file belongs: present, but unreadable as a choice.
        (Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/engine-choice.json").mkdir(parents=True)
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1", "--rollback-tag", "pre-unread"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert "default since G2" not in result.stdout
        assert "cannot read the engine choice" in result.stdout
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_an_explicit_python_choice_with_nothing_to_deploy_is_kept(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_published"] = True
        state["target_version"] = "0.21.0"  # already at the target: nothing to deploy
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "python", "--tag", "0.21.0", "--daemon-only", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        choice = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/engine-choice.json"
        assert json.loads(choice.read_text())["engine"] == "python"
        state = json.loads(state_path.read_text())
        state["target_version"] = "0.21.1"
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1", "--rollback-tag", "pre-kept"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert "default since G2" not in result.stdout
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("failure", ["pending_import", "old_schema"])
def test_release_rust_imports_legacy_counters_on_python_first(tmp_path, native, failure):
    # G2-K: the Rust daemon never reads memory_state/weights.pt. Update first
    # moves to the Python daemon at the target, whose startup imports it.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, failure) as (repo, env, state_path):
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-import"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        # The Rust image is pulled first, before the Python leg changes anything.
        assert stages(state) == ["pull", "pull", "backup", "tag", "up", "pull", "backup", "tag", "up"]
        pulled = [a[-1] for a in state["calls"] if a[:1] == ["pull"]]
        assert pulled == ["ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1",
                          "ghcr.io/pseudogiant-xr/pseudolife-daemon:0.21.1",
                          "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"]
        assert "moving to the Python daemon 0.21.1 first" in result.stdout
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        # The original image's rollback survives beside the second leg's.
        rollback = json.loads(result.stdout)["rollback"]
        assert rollback["state"] == "tagged"
        assert rollback["before_import"]["state"] == "tagged"
        assert rollback["before_import"]["tag"].endswith("-pre-import-pre-import")
        assert rollback["tag"] != rollback["before_import"]["tag"]


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("case", ["rust_host", "unreadable_schema"])
def test_release_rust_refuses_an_unimportable_bank_without_an_import_leg(tmp_path, native, case):
    # A Rust daemon never imports weights.pt, and an unreadable schema would
    # fail the Python daemon too: both refuse before changing anything.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import" if case == "rust_host" else case) as (repo, env, state_path):
        if case == "rust_host":
            state = json.loads(state_path.read_text())
            state["running"]["Config"]["Labels"]["org.pseudolife.engine"] = "rust"
            state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "nothing was changed" in result.stdout
        assert "moving to the Python daemon" not in result.stdout
        state = json.loads(state_path.read_text())
        assert "backup" not in stages(state) and "up" not in stages(state)


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_rust_stops_on_python_when_the_import_does_not_happen(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["import_stuck"] = True
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-stuck"])
        # The Python leg changed the host, so this is a failure, not a refusal.
        assert result.returncode == 1, result.stdout + result.stderr
        assert "did not import memory_state/weights.pt" in result.stdout
        assert "move memory_state/weights.pt aside" in result.stdout
        state = json.loads(state_path.read_text())
        assert stages(state) == ["pull", "pull", "backup", "tag", "up"]
        assert _labels(state_path)["org.pseudolife.engine"] == "python"


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_rust_import_leg_checks_the_disk_before_pulling(tmp_path, native):
    # The Rust image is pulled before the Python leg; the 8 GiB floor must
    # hold first, as it does before every other pull.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["low_disk"] = True
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "8 GiB" in result.stdout, result.stdout
        calls = json.loads(state_path.read_text())["calls"]
        assert not any(a[:1] == ["pull"] for a in calls), calls
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_rust_without_an_image_refuses_before_the_import_leg(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["rust_pull_fails"] = True
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert "nothing was changed" in result.stdout
        assert stages(json.loads(state_path.read_text())) == ["pull"]


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_rust_import_leg_runs_at_the_same_version_with_a_fixed_rollback_tag(tmp_path, native):
    # The Python daemon already runs the target: it still restarts to import,
    # and the two legs' rollback tags (same version, same suffix) stay apart.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["target_version"] = "0.21.0"
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.0", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "fixed"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert stages(state) == ["pull", "pull", "backup", "tag", "up", "pull", "backup", "tag", "up"]
        tags = [a[-1] for a in state["calls"] if a[:1] == ["tag"]]
        assert len(tags) == 2 and tags[0] != tags[1], tags
        assert tags[0].endswith("-fixed-pre-import") and tags[1].endswith("-fixed")
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@pytest.mark.parametrize("native_first", [True, False], ids=["native-first", "python-first"])
def test_release_rust_selection_is_shared_by_both_updaters(tmp_path, native_first):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        arms = [native_first, not native_first, native_first]
        rounds = [["--engine", "rust"], [], ["--engine", "python"]]
        for index, (native, extra) in enumerate(zip(arms, rounds)):
            result = run_arm(repo, env, binary if native else None,
                             [*extra, "--tag", "0.21.1", "--reinstall", "--daemon-only", "--json",
                              "--health-retries", "1", "--rollback-tag", f"pre-round-{index}"])
            assert result.returncode == 0, result.stdout + result.stderr
            assert _labels(state_path)["org.pseudolife.engine"] == ("python" if index == 2 else "rust")


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
        state["running"]["Config"]["Env"] = [entry for entry in state["running"]["Config"]["Env"]
                                             if not entry.startswith("PSEUDOLIFE_MCP_TOKEN=")]
        state_path.write_text(json.dumps(state))
        if mode == "checkout":
            (repo / "ops/.env").unlink()
            (repo / "ops/.env.example").write_text("# PSEUDOLIFE_MCP_TOKEN=\n")
        args = ["--tag", "0.21.1", "--daemon-only"] if mode == "release" else [
            "--checkout", str(repo), "--no-test-login", "--no-cache-prune"]
        result = run_arm(repo, env, binary, [*args, "--json", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert stages(json.loads(state_path.read_text()))[-2:] == ["tag", "up"]
        if mode == "checkout":
            assert not (repo / "ops/.env").exists()


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


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("layout", ["stack", "packaged", "old-archive"])
def test_release_keeps_a_stack_file_that_only_shares_a_shipped_name(tmp_path, native, layout):
    # A stack manager (Portainer: /data/compose/<id>/docker-compose.yml)
    # labels its own docker-compose.yml. Native release mode replaced every
    # file of a shipped name with the release archive's copy, losing that
    # stack's settings; only a packaged copy (a checkout's ops/) is replaced.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        labels = state["running"]["Config"]["Labels"]
        if layout == "stack":
            stack = tmp_path / "portainer/compose/12"
            stack.mkdir(parents=True)
            (stack / "docker-compose.yml").write_text("services:\n  pseudolife-daemon: {}\n")
            labels["com.docker.compose.project.config_files"] = str(stack / "docker-compose.yml")
            labels["com.docker.compose.project.working_dir"] = str(stack)
        if layout == "old-archive":
            # An archive an earlier run wrote under another home: still packaged.
            old = tmp_path / "old-home/.local/share/pseudolife-mcp/compose/0.21.0"
            old.mkdir(parents=True)
            for name in ("docker-compose.yml", "docker-compose.ghcr.yml"):
                (old / name).write_text("services:\n  pseudolife-daemon: {}\n")
            labels["com.docker.compose.project.config_files"] = ",".join(
                str(old / name) for name in ("docker-compose.yml", "docker-compose.ghcr.yml"))
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-stack"])
        assert result.returncode == 0, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        up = next(a for a in calls if a[:1] == ["compose"] and "up" in a)
        files = [up[i + 1] for i, v in enumerate(up[:-1]) if v == "-f"]
        bases = [v for v in files if Path(v).name == "docker-compose.yml"]
        ghcr = [v for v in files if Path(v).name == "docker-compose.ghcr.yml"]
        if layout == "stack":
            # The stack's own file, no second base beside it, and the image
            # overlay after it.
            assert bases == [str(stack / "docker-compose.yml")], files
            assert len(ghcr) == 1 and files.index(ghcr[0]) > files.index(bases[0]), files
        elif native:
            # A packaged copy is replaced by this release's verified archive.
            archive = Path(ghcr[0]).parent
            assert archive.name == "0.21.1" and archive.parent.name == "compose", files
            assert [Path(v) for v in bases] == [archive / "docker-compose.yml"], files
            assert files[0] == bases[0] and files.index(ghcr[0]) > files.index(bases[0]), files


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_refuses_when_a_labelled_override_beside_ops_is_missing(tmp_path, native):
    # Review (Opus): a missing override in a checkout's ops/ was exempt
    # because its directory looked packaged; only a shipped file's packaged
    # copy is the archive's to replace. The Python updater already refused.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        missing = repo / "ops/docker-compose.engine-local.yml"
        state["running"]["Config"]["Labels"]["com.docker.compose.project.config_files"] += "," + str(missing)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "docker-compose.engine-local.yml" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
def test_release_refuses_when_a_stack_file_is_missing_on_this_host(tmp_path, native):
    # Portainer records /data/compose/<id>/docker-compose.yml, a path inside
    # its own container. The release's files would replace the stack's
    # configuration, so both updaters refuse before changing anything.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        labels = state["running"]["Config"]["Labels"]
        stack = tmp_path / "portainer-only/compose/12"
        labels["com.docker.compose.project.config_files"] = str(stack / "docker-compose.yml")
        labels["com.docker.compose.project.working_dir"] = str(repo / "ops")
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "unavailable on this host" in result.stdout and "Nothing was changed" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("native", [False, True], ids=["python-oracle", "native"])
@pytest.mark.parametrize("engine", ["", "rust"])
def test_release_keeps_a_user_override_whose_name_looks_like_an_engine_overlay(tmp_path, native, engine):
    # A user's own Compose override named docker-compose.engine-*.yml beside
    # the project was dropped as if it were one of the updater's overlays,
    # losing its settings on recreation. Only the updater's own directory holds those.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        override = repo / "ops/docker-compose.engine-local.yml"
        override.write_text('services:\n  pseudolife-daemon:\n    environment:\n      FIXTURE_LOCAL: "1"\n')
        stale = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/docker-compose.engine-candidate-old.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("{}")
        state = json.loads(state_path.read_text())
        state["running"]["Config"]["Labels"]["com.docker.compose.project.config_files"] += (
            "," + str(override) + "," + str(stale))
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1", "--rollback-tag", "pre-own"]
        if engine:
            args = ["--engine", engine, *args]
        result = run_arm(repo, env, binary if native else None, args)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        up = next(a for a in calls if a[:1] == ["compose"] and "up" in a)
        assert str(override) in up, up
        # The updater's own stale candidate is still never reused.
        assert str(stale) not in up, up


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
            # The Rust engine builds and provisions before its backup.
            assert stages(json.loads(state_path.read_text())) == ["tag", "build", "backup"]
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
        state["running"]["Config"]["Env"] = [entry for entry in state["running"]["Config"]["Env"]
                                             if not entry.startswith("PSEUDOLIFE_MCP_TOKEN=")]
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
