"""Fault-injected updater acceptance against isolated subprocess Docker stubs.

The native arm accepts the standard PSEUDOLIFE_RUST_BINARY lane candidate
or the explicit PSEUDOLIFE_UPDATE_TEST_BINARY override. No real Docker
command can resolve. The tests marked ``only_frozen_python`` also run the
frozen Python updater; they are deleted with the package, and the native
side of what they test runs from the host state that updater leaves
(``fixtures/python-updater-hosts.json``).
"""
from contextlib import contextmanager, ExitStack
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

from .frozen_python import only_frozen_python

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "pseudolife-daemon"
CHECKOUT_SCHEMA_PLAN = "rust/daemon/src/storage/schema_plan.json"
FILES = ("docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.rust-daemon.yml",
         "docker-compose.rust-shadow.yml", "docker-compose.rust-ghcr.yml", "Dockerfile.extractor")
ARCHIVED_PYTHON = ("ghcr.io/pseudogiant-xr/pseudolife-daemon@sha256:"
                   "1613e1bd0cb563e9119f0444ca97dea659fa143a16931f4c512f4959243fdcc8")


def _native_binary():
    candidate = os.environ.get("PSEUDOLIFE_UPDATE_TEST_BINARY") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    return str(Path(candidate).resolve()) if candidate else None


@contextmanager
def _relocated_binary(source, destination):
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)
    try:
        yield destination
    finally:
        destination.unlink(missing_ok=True)


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
if a[:1]==["inspect"] and s.get("canonical_missing"):code=1;out="Error: No such container"
elif a[:1]==["inspect"]:
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
 else: out=json.dumps([s["running"]] if a[-1]!="fixture-pg" else [{"Name":"/fixture-pg","NetworkSettings":{"Networks":{"fixture":{"Aliases":["pseudolife-pg"],"IPAddress":s.get("pg_address","")}}}}])
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
elif a[:1]==["pull"]:
 rust="-rust:" in a[-1];archived="@sha256:" in a[-1]
 code=int(bool(s["failure"]=="pull" or rust and s.get("rust_pull_fails")
               or not rust and s.get("python_pull_fails")
               or not rust and not archived and s.get("rust_only_release")))
 if code==0 and (rust or archived):s["images"].setdefault(a[-1],"sha256:replacement")
elif a[:2]==["manifest","inspect"]:
 if "-rust:" in a[-1] and s.get("rust_manifest_denied"):code=1;out="unauthorized: authentication required"
 elif "-rust:" in a[-1]:code=0 if s.get("rust_published") else 1;out="" if s.get("rust_published") else "manifest unknown"
 elif s.get("python_manifest_denied"):code=1;out="unauthorized: authentication required"
 elif s.get("rust_only_release"):code=1;out="manifest unknown"
elif a[:1]==["run"] and any(m in a for m in ("--probe-access","--list-root-owned","--repair-ownership")):
 # The ownership helpers (cli::ownership), answering in hex as the script
 # does: an entry the row made unusable stays so until a repair of its root
 # changes it (only uid 0 entries).
 o=s.setdefault("ownership",{});bad=[e for e in o.get("unusable",[]) if e[1] not in o.get("repaired",[])]
 mode=next(m for m in ("--probe-access","--list-root-owned","--repair-ownership") if m in a)
 roots=a[a.index(mode)+1:];mine=[q for u,q in bad if u=="0" and any(q==r or q.startswith(r+"/") for r in roots)]
 if mode=="--probe-access":code=o.get("probe_code",0);fields=["probe-access","1"]+[f for u,q in bad for f in ("U",u,q)]
 elif mode=="--list-root-owned":code=0;fields=["list-root-owned","1"]+[f for q in mine for f in ("L","0",q)]
 else:
  o.setdefault("repaired",[]).extend(mine);code=o.get("repair_code",0)
  fields=["repair-ownership","1"]+[f for q in mine for f in ("C","0",q)]
 out="".join(f+"\0" for f in fields+["end"]).encode().hex()
elif a[:1]==["run"]:
 if "check-config" in a:
  code=s.get("check_config",0)
  out={3:"unsupported memory.reranker.model_name 'BAAI/bge-reranker-base': the native daemon supports only cross-encoder/ms-marco-MiniLM-L-6-v2; set memory.reranker.model_name to that model, or keep the Python engine for a different reranker",2:"storage resolution refused: the Rust daemon requires PSEUDOLIFE_MCP_DATABASE_URL"}.get(code,"the native daemon supports the configuration in /data/config.yaml")
 elif "provision" in a:
  code=s.get("provision_exit",int(s["failure"]=="provision"))
  if code and "provision_stderr" in s:
   out="starting model provision (stdout)"
   sys.stderr.write(s["provision_stderr"])
 else:
  schema=s.get("target_schema",58) if "--schema-version" in a else s.get("archive_schema",59) if any("@sha256:" in v for v in a) else 58
  out=str(schema) if "--schema-version" in a else f"SCHEMA_META_VERSION = {schema}\n"
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
  code=int(s["failure"]=="up" or bool(s.get("archived_python") and s.get("fail_rust_recreate")))
  if s.get("interrupted_recreate"):
   code=1
   s["canonical_missing"]=s["interrupted_recreate"]=="removed"
  if code==0:
   s["deployed"]=True
   built=""
   overlays=[a[i+1] for i,v in enumerate(a[:-1]) if v=="-f"]
   for path in overlays:
    try:
     service=json.loads(pathlib.Path(path).read_text())["services"]["pseudolife-daemon"]
     if "labels" in service:s["running"]["Config"]["Labels"]={**(s["running"]["Config"]["Labels"] or {}),**service["labels"]}
     if service.get("labels",{}).get("org.pseudolife.release")=="0.23.1":
      s["archived_python"]=True
      s["running"]["Config"]["Image"]=service["image"]
     elif service.get("labels",{}).get("org.pseudolife.engine")=="rust":s["archived_python"]=False
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
  if "cms_counters" in a[-1]:out="f" if s.get("counters_missing") or s["failure"] in ("pending_import", "old_schema") and not s.get("imported") else "t"
  elif "reference_chroma_import" in a[-1]:out="done" if s.get("chroma_imported") or s.get("imported") and not s.get("chroma_stuck") else ""
  else:out="56" if s["failure"]=="old_schema" and not s.get("imported") and not s.get("archived_python") else "unreadable" if s["failure"]=="unreadable_schema" else str(59 if s.get("archived_python") else s.get("running_schema",58))
 elif any("/data/chromadb/chroma.sqlite3" in v for v in a):
  if s.get("probe_failed"):code=1
  elif s.get("chroma_inaccessible"):out="inaccessible" if any("printf inaccessible" in v for v in a) else "absent"
  else:out="present" if s.get("chroma_pending") else "absent"
 elif any("test ! -e /data/memory_state/weights.pt" in v for v in a):code=int(s["failure"] in ("pending_import","imported_weights") and not s.get("imported"))
 elif any("test -e /data/memory_state/weights.pt" in v for v in a):out="present" if s["failure"] in ("pending_import","imported_weights") and not s.get("imported") else "absent"
 elif any("pg_dump" in v for v in a):code=int(s["failure"]=="backup")
 elif any("tar czf" in v for v in a):code=int(s["failure"]=="missing_state")
elif a[:1]==["cp"]:
 target=pathlib.Path(a[-1]);target.parent.mkdir(parents=True,exist_ok=True)
 if ".sql.gz" in a[-2]:target.write_bytes(gzip.compress(b"COPY x FROM stdin;\n\\.\n"+(b"" if s["failure"]=="incomplete" else b"-- PostgreSQL database dump complete\n")))
 else:
  with tarfile.open(target,"w:gz") as tar:
   member=tarfile.TarInfo("config.yaml");member.size=2;tar.addfile(member,io.BytesIO(b"{}"))
p.write_text(json.dumps(s))
# The helpers' NUL-framed answers carry no trailing newline, as Docker relays them.
sys.stdout.write(out if out.endswith("\0") else out+"\n")
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
    # The checkout's release version is its native client crate's; the
    # frozen Python updater these cases also run still knows a checkout by
    # pyproject.toml, so both carry the version, as every checkout does
    # while the wheel is published.
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.21.1"\n')
    (repo / "rust/shim").mkdir(parents=True)
    (repo / "rust/shim/Cargo.toml").write_text('[package]\nname = "pseudolife-stdio"\nversion = "0.21.1"\n')
    plan = repo / CHECKOUT_SCHEMA_PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"schema_version": 58}), encoding="utf-8")
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
    # The release publishes its Rust image: the Python daemon is retired, so
    # a release update moves this Python host to Rust (a case that models an
    # unpublished image sets rust_published False).
    state = {"calls": [], "deployed": False, "failure": failure, "images": {"pseudolife-daemon:0.21.0": "sha256:validated"},
             "rust_published": True,
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

        def do_CONNECT(self):
            # The unknown-target test uses this local proxy for GitHub discovery.
            (home / "release-discovery-refused").touch()
            self.send_error(503)

        def do_GET(self):
            current = json.loads(state_path.read_text())
            if self.path.startswith("/native/"):
                # PSEUDOLIFE_NATIVE_ARCHIVE_URL: a newer release's client
                # (_newer_release), each request logged.
                name = self.path.rsplit("/", 1)[-1]
                with (home / "native-requests.log").open("a", encoding="utf-8") as log:
                    log.write(name + "\n")
                served = current.get("native", {}).get(name)
                if served is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = Path(served).read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
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
                if current.get("archived_python") and current.get("bridge_health_offline"):
                    self.send_response(503)
                    self.end_headers()
                    return
                deployed = current["deployed"]
                bridge_wait = current.get("archived_python") and current.get("wait_for_bridge")
                waiting = bridge_wait or deployed and not current.get("archived_python") and current.get("wait_for_rust")
                if bridge_wait:
                    (home / "bridge-health-requested").touch()
                if deployed and not current.get("archived_python") and current.get("wait_for_rust"):
                    (home / "rust-health-requested").touch()
                body = json.dumps({"status": "starting" if waiting else "error" if deployed and failure == "health" or not deployed and failure == "stopped" else "ok",
                                   "version": "wrong" if deployed and failure == "version" else "0.23.1" if current.get("archived_python") else current.get("target_version", "0.21.1") if deployed else current.get("running_version", "0.21.0"),
                                   "schema": (59 if failure == "schema" or current.get("archived_python") else current.get("target_schema", 58)) if deployed else current.get("running_schema", 58),
                                   "auth": True, "persist_errors": 0,
                                   "updates": {"unattended_daemon": not current.get("unattended_daemon_off")}}).encode()
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
            request = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            current = json.loads(state_path.read_text())
            if self.path == "/api/coordination/register":
                payload = {"agent_id": "fixture-address", "credential": "fixture-key"}
            elif self.path == "/api/coordination/agents":
                # Backup completion brings a peer onto the board. Only the
                # callback inside deploy_release can observe this transition.
                active = not current.get("no_board_peer") and any(a[:1] == ["fixture-backup"] for a in current["calls"])
                payload = {"agents": [{"agent_id": "fixture-peer", "task": "working"}] if active else []}
            elif self.path == "/api/daemon-notice":
                with state_path.with_name("notices.jsonl").open("a", encoding="utf-8") as notices:
                    notices.write(json.dumps(json.loads(request)["text"]) + "\n")
                payload = {"recipients": 1}
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
               PSEUDOLIFE_NATIVE_ARCHIVE_URL=url + "/native",
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


# Since the Python freeze (14 November 2026) the transaction cases run the
# native CLI only: their python-oracle cases ran the frozen Python updater
# under the same assertions, so they could fail only on frozen code. The
# ``native`` parameter is kept so each case still reads as it did. The two
# interop tests (test_release_rust_selection_is_shared_by_both_updaters,
# test_release_updaters_can_follow_each_other) still run the Python updater
# on purpose, while the package is present: what they test is the two
# updaters acting on one host, which an install with the Python updater
# still meets. The native updater following a host the Python updater left,
# the upgrade every existing user makes, runs without the package from the
# captured host state (test_native_updater_follows_*).
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
        with ExitStack() as binaries:
            if os.name == "nt":
                # Keep the candidate beside our Docker stub: Rust checks its
                # executable directory and System32 before inherited PATH.
                destination = Path(env["PSEUDOLIFE_DOCKER"]).with_name(Path(binary).name)
                binary = binaries.enter_context(_relocated_binary(binary, destination))
            env["PSEUDOLIFE_TEST_PG_HOST_PORT"] = "127.0.0.1:5433"
            result = run_arm(repo, env, binary, ["--checkout", str(repo), "--json", "--no-cache-prune"])
        assert result.returncode == 0, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        assert ["port", "fixture-pg", "5432/tcp"] in calls
        assert not any("pseudolife-mcp-postgres" in call for call in calls)
        assert any(call[:3] == ["exec", "-i", "fixture-pg"] and any("psql" in arg for arg in call) for call in calls)
        if os.name == "nt":
            retained = Path(binary).stat().st_size if Path(binary).exists() else 0
            assert retained == 0, f"test-login call retained {retained} bytes of executable scratch"


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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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
    """The native updater prints the restore command for a failed Rust-to-Rust
    rerun: with the checkout overlay's /models volume when the running daemon
    uses it, without it for one that read /data/models. The file lists are the
    frozen Python updater's, recorded on 14 November 2026, when both agreed."""
    import re
    binary = _native_binary()
    if not binary:
        pytest.skip("native binary build is owned by the parent; set PSEUDOLIFE_RUST_BINARY")
    expected = {"volume": ["docker-compose.yml", "docker-compose.rust-daemon.yml", "docker-compose.rollback.json"],
                "legacy": ["docker-compose.yml", "docker-compose.rollback.json"]}
    for arm, native in (("native", binary),):
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
            assert found == expected[models], restore


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


def _rust_host(state, env, activation="fixture"):
    """An accepted release-mode Rust host. A release update keeps only Rust
    (the Python daemon is retired, maintainer 27 November 2026), so rows about
    update machinery rather than the engine start from one."""
    labels = {"org.pseudolife.engine": "rust", "org.pseudolife.activation": activation,
              "org.pseudolife.release": "0.21.0"}
    state["running"]["Config"]["Labels"].update(labels)
    accepted = _accepted(env)
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_text(json.dumps({"services": {SERVICE: {"labels": labels}}}))


@pytest.mark.parametrize("native", [True], ids=["native"])
def test_release_rust_activation_keeps_rust_and_refuses_a_switch_back(tmp_path, native):
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

        # The Python daemon is retired: release mode refuses the switch back
        # before it touches anything.
        before = len(json.loads(state_path.read_text())["calls"])
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "python", "--tag", "0.21.1", "--reinstall", *common, "--rollback-tag", "pre-back"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "--engine python is retired for release updates" in result.stdout + result.stderr
        assert json.loads(state_path.read_text())["calls"][before:] == []
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@pytest.mark.parametrize("native", [True], ids=["native"])
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
            assert "restore registry access or select a published Rust daemon" in result.stdout
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("labelled", [False, True], ids=["bundled-overlay", "labelled-overlay"])
def test_release_rust_models_mount_does_not_block_a_rerun(tmp_path, native, labelled):
    # Once the Rust daemon runs with the overlay's /models volume, a plain
    # Rust rerun keeps it: it may not be refused as "the Compose
    # configuration drops a daemon mount".
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
            # Compose records the overlay it was created with: the rerun
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
        state = json.loads(state_path.read_text())
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        assert any(m["Destination"] == "/models" for m in state["running"]["Mounts"])


def test_release_rust_rerun_still_refuses_dropping_another_mount(tmp_path):
    # Near miss for the row above: the rerun keeps the models volume, but a
    # mount the Compose configuration no longer declares is still refused.
    # Native release mode checks every mount; the Python release updater
    # checks only /data, so this row is native-only.
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
        back = run_arm(repo, env, binary, ["--tag", "0.21.1", "--reinstall", *common, "--rollback-tag", "rerun"])
        assert back.returncode == 2, back.stdout + back.stderr
        assert "drops a daemon mount" in back.stdout
        state = json.loads(state_path.read_text())
        assert stages({"calls": state["calls"][before:]}) == []
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("case", ["unchosen", "unpublished", "chose-python", "chose-python-then-rust",
                                  "corrupt-choice", "binary-choice", "working-dir"])
def test_release_moves_every_python_host_to_rust(tmp_path, native, case):
    # Maintainer, 27 November 2026: the Python daemon is retired, so a release
    # update moves a Python host to Rust whatever it recorded, and refuses a
    # release that publishes no Rust image instead of updating Python.
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
        result = run_arm(repo, env, binary if native else None, args)
        state = json.loads(state_path.read_text())
        pulled = [a[-1] for a in state["calls"] if a[:1] == ["pull"]]
        if case == "unpublished":
            assert result.returncode == 2, result.stdout + result.stderr
            assert "could not confirm a published rust daemon image" in result.stdout
            assert pulled == [] and "backup" not in stages(state)
            assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"
            return
        assert result.returncode == 0, result.stdout + result.stderr
        assert pulled == ["ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"]
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        moved = "moving this host from the retired python daemon to the rust daemon" in result.stdout
        assert moved == (case != "chose-python-then-rust")
        if case in ("corrupt-choice", "binary-choice"):
            assert "cannot read the engine choice" in result.stdout
            assert "no recorded choice keeps it" in result.stdout
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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
        assert result.returncode == 1, result.stdout + result.stderr
        calls = json.loads(state_path.read_text())["calls"]
        assert any(a[:2] == ["image", "inspect"] and "-rust:" in a[-1] for a in calls)
        assert "not moving to the rust daemon this time" in result.stdout
        assert "was left as it was and nothing was deployed" in result.stdout
        assert not any(a[:1] == ["pull"] and "-rust:" not in a[-1] for a in calls), calls
        assert not any(a[:2] == ["image", "rm"] for a in calls), calls


@pytest.mark.parametrize("native", [True], ids=["native"])
def test_a_fallback_after_provisioning_removes_the_model_volume_it_created(tmp_path, native):
    # The move provisioned the overlay's models volume, then the state backup
    # refused: the refusal removes the volume and the image it added.
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
        assert result.returncode == 1, result.stdout + result.stderr
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


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("flag", ["--unattended", "--allow-auth-change"])
def test_python_host_unattended_holds_and_only_the_bearer_handover_stays(tmp_path, native, flag):
    # The scheduled run cannot make the move (a legacy import or a refusal
    # needs someone there), so it holds; the bearer handover is the one run
    # that stays on Python, since a bearer-less host gets its bearer only so.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
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
        state = json.loads(state_path.read_text())
        assert not any(a[:1] == ["pull"] and "-rust:" in a[-1] for a in state["calls"]), result.stdout
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"
        if flag == "--unattended":
            assert result.returncode == 4, result.stdout + result.stderr
            assert not stages(state), stages(state)
            notice, = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
            assert "The Python daemon is retired" in notice and "pseudolife-mcp update" in notice, notice
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            assert "up" in stages(state), stages(state)
            assert not [a for a in state["calls"] if a[:2] == ["manifest", "inspect"]]
            assert "the bearer handover runs on the retired python daemon" in result.stdout


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("case", ["unsupported", "uncheckable"])
def test_release_refuses_a_configuration_rust_refuses_and_leaves_python(tmp_path, native, case):
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
        # Unsupported is a usage refusal; an image that cannot check is an error.
        assert result.returncode == (2 if case == "unsupported" else 1), result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        calls = state["calls"]
        # No backup or tag at all, and the pulled image is removed.
        assert not any(a[:1] in (["fixture-backup"], ["tag"]) for a in calls)
        check = next(i for i, a in enumerate(calls) if a[:1] == ["run"] and "check-config" in a)
        assert ["image", "rm", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"] in calls[check:]
        pulled = [a[-1] for a in calls if a[:1] == ["pull"]]
        assert pulled == ["ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"], pulled
        assert "was left as it was and nothing was deployed" in result.stdout
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"
        warning = next(line for line in result.stdout.splitlines() if "not moving to the rust daemon" in line)
        if case == "unsupported":
            # The daemon's own named error, its fix included.
            assert "memory.reranker.model_name" in warning and "keep the Python engine" in warning
        else:
            assert "could not check this bank's configuration" in warning


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("engine", ["python-host", "rust-host"])
def test_release_refuses_engine_python_before_any_change(tmp_path, native, engine):
    # Maintainer, 27 November 2026: no release update leaves or puts a host on
    # the retired Python daemon. A checkout deploy still takes --engine python.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        if engine == "rust-host":
            state = json.loads(state_path.read_text())
            _rust_host(state, env)
            state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "python", "--tag", "0.21.1", "--daemon-only", "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "--engine python is retired for release updates" in result.stdout + result.stderr
        assert "--checkout <repo>" in result.stdout + result.stderr
        assert json.loads(state_path.read_text())["calls"] == []
        assert not (Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/engine-choice.json").exists()


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("case", ["tokenless", "no-backup", "rust-pull-fails", "state-backup-refused"])
def test_a_refused_move_stops_the_update_and_leaves_python(tmp_path, native, case):
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
        expected = {"tokenless": 2, "no-backup": 2, "rust-pull-fails": 1, "state-backup-refused": 1}[case]
        assert result.returncode == expected, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        pulled = [a[-1] for a in state["calls"] if a[:1] == ["pull"]]
        # Never an in-place Python update: nothing tagged or recreated (the
        # state-backup refusal comes inside the backup, after the dump).
        assert "ghcr.io/pseudogiant-xr/pseudolife-daemon:0.21.1" not in pulled, pulled
        assert not any(a[:1] == ["tag"] for a in state["calls"]) and "up" not in stages(state)
        if case != "state-backup-refused":
            assert "backup" not in stages(state)
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"
        if case == "no-backup":
            assert result.returncode == 2
            assert "needs the bank and state backup; retry without --no-backup" in result.stdout + result.stderr
            assert pulled == []
            return
        # The refusal's whole first line, its one fix included.
        warning = next(line for line in result.stdout.splitlines() if "not moving to the rust daemon" in line)
        fix = {"tokenless": "set PSEUDOLIFE_MCP_TOKEN", "rust-pull-fails": "keeps its Python daemon",
               "state-backup-refused": "state"}[case]
        assert fix in warning, warning
        assert "was left as it was and nothing was deployed" in result.stdout
        if case == "tokenless":
            # The bearer comes only through the explicit handover.
            assert "--allow-auth-change" in result.stdout and "--tag 0.21.1" in result.stdout, result.stdout
            assert "pseudolife-mcp connect" in result.stdout, result.stdout
        removed = ["image", "rm", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"] in state["calls"]
        # Only a pull that succeeded leaves an image to remove.
        assert removed == (case == "state-backup-refused")


@pytest.mark.parametrize("case", ["old_schema", "pending_import", "chroma", "rust_host",
                                  "python_newer", "schema_newer", "target_too_old", "missing_target", "pin_unavailable",
                                  "wrong_pin_schema", "file_probe_failed", "no_backup", "bad_config",
                                  "config_probe_failed", "no_counter_row", "chroma_stuck"])
def test_release_legacy_import_uses_archived_image_and_stops_at_its_gate(tmp_path, case):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    failure = "old_schema" if case == "old_schema" else "pending_import"
    if case in ("chroma", "chroma_stuck"):
        failure = ""
    with host(tmp_path, failure) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, rust_published=True, rust_only_release=True)
        if case in ("rust_host", "no_backup"):
            labels = {"org.pseudolife.engine": "rust", "org.pseudolife.activation": "fixture",
                      "org.pseudolife.release": "0.21.0"}
            state["running"]["Config"]["Labels"].update(labels)
            accepted = _accepted(env)
            accepted.parent.mkdir(parents=True)
            accepted.write_text(json.dumps({"services": {SERVICE: {"labels": labels}}}))
        if case in ("chroma", "chroma_stuck"):
            state.update(chroma_pending=True, chroma_stuck=case == "chroma_stuck")
        if case == "python_newer":
            state["running_version"] = "0.24.0"
        if case == "schema_newer":
            state["running_schema"] = 60
        if case == "target_too_old":
            state["target_schema"] = 58
        if case == "missing_target":
            state["rust_pull_fails"] = True
        if case == "pin_unavailable":
            state["python_pull_fails"] = True
        if case == "wrong_pin_schema":
            state["archive_schema"] = 58
        if case == "file_probe_failed":
            state["probe_failed"] = True
        if case in ("bad_config", "config_probe_failed"):
            state["check_config"] = 3 if case == "bad_config" else 2
        if case == "no_counter_row":
            state["import_stuck"] = True
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.25.0", "--daemon-only", "--json", "--health-retries", "1"]
        if case == "config_probe_failed":
            args.extend(["--engine", "rust"])
        if case == "no_backup":
            args.append("--no-backup")
        result = run_arm(repo, env, binary, args)
        report = json.loads(result.stdout)
        state = json.loads(state_path.read_text())
        pulls = [a[-1] for a in state["calls"] if a[:1] == ["pull"]]
        rust_target = "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.25.0"
        completed_hops = stages(state).count("up")
        if case in ("schema_newer", "target_too_old", "missing_target", "pin_unavailable", "wrong_pin_schema",
                    "file_probe_failed", "no_backup", "bad_config", "config_probe_failed"):
            assert result.returncode in (1, 2), report
            assert completed_hops == 0 and "backup" not in stages(state) and "tag" not in stages(state), report
            assert all(image in (ARCHIVED_PYTHON, rust_target) for image in pulls), pulls
            causes = {"schema_newer": "newer", "target_too_old": "schema",
                      "missing_target": rust_target, "pin_unavailable": "0.23.1",
                      "wrong_pin_schema": "did not report schema v59", "file_probe_failed": "state volume for legacy files",
                      "no_backup": "legacy import requires a bank and state backup",
                      "bad_config": "memory.reranker.model_name", "config_probe_failed": "check this bank's configuration"}
            assert causes[case] in _report_text(report), report
        elif case in ("no_counter_row", "chroma_stuck"):
            assert result.returncode == 1 and completed_hops == 1, report
            assert report["rollback"]["restore_required"] is True, report
            assert ("cms_counters" if case == "no_counter_row" else "Chroma") in _report_text(report), report
        else:
            assert result.returncode == 0 and completed_hops == 2, report
            assert pulls == [rust_target, ARCHIVED_PYTHON], pulls
            assert report["target"] == "0.25.0", report
            assert report["rollback"]["before_import"]["restore_required"] is True, report
            assert report["rollback"]["before_import"]["image_id"] == "sha256:validated", report


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("case", ["supported", "unsupported"])
def test_release_default_imports_legacy_counters_unless_rust_refuses_the_configuration(tmp_path, native, case):
    # With G2-K, a defaulted move on a bank whose counters still need the
    # Python daemon's import takes the import leg, then moves to Rust. The
    # configuration check runs first, so a configuration Rust refuses stops
    # before the import and never tries a Python image at the selected tag.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(rust_published=True, rust_only_release=True, target_schema=59)
        if case == "unsupported":
            state["check_config"] = 3
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-default-import"])
        state = json.loads(state_path.read_text())
        calls = state["calls"]
        check = next(i for i, a in enumerate(calls) if a[:1] == ["run"] and "check-config" in a)
        assert not any(a[:1] == ["fixture-backup"] for a in calls[:check])
        if case == "supported":
            assert result.returncode == 0, result.stdout + result.stderr
            assert stages(state) == ["pull", "pull", "backup", "tag", "up", "backup", "tag", "up"]
            assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        else:
            assert result.returncode == 2, result.stdout + result.stderr
            assert stages(state) == ["pull"]
            assert ["image", "rm", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"] in calls
            assert "memory.reranker.model_name" in result.stdout
            assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"


def test_release_already_imported_bank_updates_once_and_rerun_is_a_noop(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "imported_weights") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, rust_only_release=True,
                     chroma_pending=True, chroma_imported=True)
        state_path.write_text(json.dumps(state))
        args = ["--engine", "rust", "--tag", "0.25.0", "--daemon-only", "--json", "--health-retries", "1"]
        result = run_arm(repo, env, binary, args)
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert stages(state) == ["pull", "backup", "tag", "up"]
        state["calls"].clear()
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, args)
        report = json.loads(result.stdout)
        assert result.returncode == 0, report
        assert len(report["steps"]) == 1 and "already runs 0.25.0" in report["steps"][0], report
        assert stages(json.loads(state_path.read_text())) == []


def test_release_cannot_skip_an_inaccessible_chroma_directory(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(chroma_inaccessible=True, target_schema=59, rust_published=True, rust_only_release=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "0.25.0", "--daemon-only", "--json",
                                             "--health-retries", "1"])
        assert result.returncode == 2 and "/data/chromadb" in result.stdout, result.stdout
        assert "grant the daemon account access" in result.stdout.lower()
        assert stages(json.loads(state_path.read_text())) == []


def test_release_refuses_an_archived_waypoint_with_no_original_recovery_record(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(running_version="0.23.1", running_schema=59, target_version="0.25.0",
                     target_schema=59, counters_missing=True, rust_only_release=True)
        labels = {"org.pseudolife.engine": "python", "org.pseudolife.activation": "fixture",
                  "org.pseudolife.release": "0.23.1"}
        state["running"]["Config"]["Labels"].update(labels)
        accepted = _accepted(env)
        accepted.parent.mkdir(parents=True)
        accepted.write_text(json.dumps({"services": {SERVICE: {"labels": labels, "image": ARCHIVED_PYTHON}}}))
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "0.25.0", "--daemon-only", "--json",
                                             "--health-retries", "1"])
        assert result.returncode == 2 and "without a verified pre-import recovery record" in result.stdout, result.stdout
        assert stages(json.loads(state_path.read_text())) == []


def test_unattended_posts_each_hold_reason_once_per_release(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, rust_only_release=True,
                     no_board_peer=True)
        state_path.write_text(json.dumps(state))
        for tag in ("0.25.0", "0.25.0", "0.26.0"):
            result = run_arm(repo, env, binary, ["--unattended", "--tag", tag, "--json",
                                                 "--health-retries", "1"])
            assert result.returncode == 4, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert not state["deployed"]
        assert not stages(state)
        assert not any("provision" in call for call in state["calls"])
        notices = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
        assert len(notices) == 2
        log = Path(env["PSEUDOLIFE_MCP_DATA_DIR"], "unattended-update.log").read_text(encoding="utf-8")
        for tag, notice in zip(("0.25.0", "0.26.0"), notices, strict=True):
            assert f"release {tag}" in notice and "The Python daemon is retired" in notice
            assert f"pseudolife-mcp update --engine rust --tag {tag}" in notice
            assert log.count(f"pseudolife-mcp update --engine rust --tag {tag}") == 1
        state.update(wait_for_bridge=True, target_version="0.25.0")
        state_path.write_text(json.dumps(state))
        interrupted = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.25.0",
                                                    "--json", "--health-retries", "1",
                                                    "--rollback-tag", "attended-fixture"])
        assert interrupted.returncode == 1 and "health gate" in interrupted.stdout, interrupted.stdout
        for _ in range(2):
            held = run_arm(repo, env, binary, ["--unattended", "--tag", "0.25.0", "--json"])
            assert held.returncode == 4, held.stdout
        after_bridge = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
        assert after_bridge[:2] == notices and len(after_bridge) == 3
        assert "release 0.25.0" in after_bridge[2] and "unfinished" in after_bridge[2]
        log = Path(env["PSEUDOLIFE_MCP_DATA_DIR"], "unattended-update.log").read_text(encoding="utf-8")
        assert log.count("Original pre-import recovery instructions (saved at ") == 1
        assert "attended-fixture-pre-import" in log
        original = json.loads(interrupted.stdout)["rollback"]
        state["running"]["Id"] = "restored-original"
        state["running"]["Config"]["Image"] = original["tag"]
        state_path.write_text(json.dumps(state))
        interrupted_again = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.25.0",
                                                          "--json", "--health-retries", "1",
                                                          "--rollback-tag", "second-attempt"])
        assert interrupted_again.returncode == 1 and "health gate" in interrupted_again.stdout, interrupted_again.stdout
        for _ in range(2):
            held = run_arm(repo, env, binary, ["--unattended", "--tag", "0.25.0", "--json"])
            assert held.returncode == 4, held.stdout
        assert len(state_path.with_name("notices.jsonl").read_text().splitlines()) == 3
        log = Path(env["PSEUDOLIFE_MCP_DATA_DIR"], "unattended-update.log").read_text(encoding="utf-8")
        assert log.count("Original pre-import recovery instructions (saved at ") == 2
        assert "second-attempt-pre-import" in log


def test_unattended_posts_again_when_release_discovery_is_unavailable(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, wait_for_bridge=True)
        state_path.write_text(json.dumps(state))
        first = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.25.0", "--json",
                                            "--health-retries", "1"])
        assert first.returncode == 1 and "health gate" in first.stdout, first.stdout
        env["HTTPS_PROXY"] = env["https_proxy"] = env["PSEUDOLIFE_MCP_DAEMON_URL"]
        for _ in range(2):
            held = run_arm(repo, env, binary, ["--unattended", "--json"])
            assert held.returncode == 4, held.stdout + held.stderr
            assert json.loads(held.stdout).get("target") is None
        assert state_path.with_name("release-discovery-refused").is_file()
        notices = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
        assert len(notices) == 2
        assert all("unfinished" in notice and "pseudolife-mcp update --engine rust`" in notice for notice in notices)
        state_dir = Path(env["PSEUDOLIFE_MCP_DATA_DIR"], "unattended-attended-releases")
        assert not state_dir.exists() or not list(state_dir.iterdir())


@pytest.mark.parametrize("manifest_denied", [False, True])
def test_unattended_python_host_holds_even_where_the_release_publishes_python(tmp_path, manifest_denied):
    # A published (or unreadable) Python image no longer lets the scheduled
    # run update a Python host in place: it holds for one attended move.
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, no_board_peer=True,
                     python_manifest_denied=manifest_denied)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--unattended", "--tag", "0.25.0", "--json",
                                             "--health-retries", "1"])
        assert result.returncode == 4, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert not stages(state)
        assert not any(call[:1] == ["pull"] for call in state["calls"])
        notice, = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
        assert "The Python daemon is retired" in notice, notice


def test_unattended_rust_host_holds_when_the_bank_needs_the_python_bridge(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        state = json.loads(state_path.read_text())
        labels = state["running"]["Config"]["Labels"]
        labels.update({"org.pseudolife.engine": "rust", "org.pseudolife.release": "0.21.0",
                       "org.pseudolife.activation": "a" * 32})
        accepted = _accepted(env)
        accepted.parent.mkdir(parents=True)
        accepted.write_text(json.dumps({"services": {SERVICE: {"labels": labels}}}))
        state.update(target_version="0.25.0", target_schema=59, no_board_peer=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--unattended", "--tag", "0.25.0", "--json"])
        assert result.returncode == 4, result.stdout + result.stderr
        assert not stages(json.loads(state_path.read_text()))
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        notice, = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
        assert "legacy import" in notice
        assert "pseudolife-mcp update --engine rust --tag 0.25.0" in notice


def test_explicit_engine_cannot_override_unattended_engine_policy(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        result = run_arm(repo, env, binary, ["--unattended", "--engine", "rust", "--tag", "0.25.0", "--json"])
        assert result.returncode == 2 and "--engine cannot be combined with --unattended" in (
            result.stdout + result.stderr
        )
        assert not stages(json.loads(state_path.read_text()))


@pytest.mark.parametrize(("interruption", "unattended"), [
    ("kill", False), ("kill", True), ("timeout", False),
])
def test_interrupted_bridge_keeps_original_recovery_on_rerun(tmp_path, interruption, unattended):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, rust_only_release=True,
                     no_board_peer=True, import_stuck=True, wait_for_bridge=True)
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.25.0", "--daemon-only", "--json"]
        first = [*args, "--engine", "rust", "--rollback-tag", "original-fixture"]
        if interruption == "kill":
            with subprocess.Popen([binary, "update", *first, "--health-retries", "50"],
                                  cwd=repo, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) as child:
                try:
                    _await_file(state_path.with_name("bridge-health-requested"), child)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=15)
        else:
            failed = run_arm(repo, env, binary, [*first, "--health-retries", "1"])
            assert failed.returncode == 1 and "health gate" in failed.stdout, failed.stdout

        original = list((Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "backups/pre-import").glob("*.sql.gz"))
        assert len(original) == 1
        state = json.loads(state_path.read_text())
        assert state["archived_python"] and not state.get("imported")
        state.update(calls=[], wait_for_bridge=False)
        state_path.write_text(json.dumps(state))
        retry = ["--tag", "0.25.0", "--json", "--health-retries", "1",
                 "--rollback-tag", "retry-fixture"]
        if unattended:
            state["bridge_health_offline"] = True
            state_path.write_text(json.dumps(state))
            for _ in range(2):
                held = run_arm(repo, env, binary, [*retry, "--unattended"])
                assert held.returncode == 4, held.stdout + held.stderr
            notices = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
            assert len(notices) == 1 and "unfinished" in notices[0]
            assert "pseudolife-mcp update --engine rust --tag 0.25.0" in notices[0]
            log = Path(env["PSEUDOLIFE_MCP_DATA_DIR"], "unattended-update.log").read_text(encoding="utf-8")
            assert "original-fixture-pre-import" in log and original[0].name in log
            state = json.loads(state_path.read_text())
            assert state["archived_python"] and not stages(state)
            state["bridge_health_offline"] = False
            state_path.write_text(json.dumps(state))
        retry.append("--daemon-only")
        pending = run_arm(repo, env, binary, retry)
        assert pending.returncode != 0
        assert "original-fixture-pre-import" in pending.stdout, pending.stdout
        assert original[0].name in pending.stdout, pending.stdout
        assert "backup" not in stages(json.loads(state_path.read_text()))

        state = json.loads(state_path.read_text())
        state.update(calls=[], imported=True, fail_rust_recreate=True)
        state_path.write_text(json.dumps(state))
        failed = run_arm(repo, env, binary, retry)
        assert failed.returncode != 0
        assert "original-fixture-pre-import" in failed.stdout, failed.stdout
        assert original[0].name in failed.stdout, failed.stdout
        assert "retry-fixture" in failed.stdout and "to roll back to the imported bank" in failed.stdout
        assert stages(json.loads(state_path.read_text())).count("backup") == 1
        assert list(original[0].parent.glob("*.sql.gz")) == original

        state = json.loads(state_path.read_text())
        state.update(calls=[], fail_rust_recreate=False, provision_exit=47)
        state_path.write_text(json.dumps(state))
        failed_again = run_arm(repo, env, binary, retry)
        assert failed_again.returncode != 0
        assert "original-fixture-pre-import" in failed_again.stdout, failed_again.stdout
        assert "to roll back to the imported bank" in failed_again.stdout, failed_again.stdout
        assert "backup" not in stages(json.loads(state_path.read_text()))


def test_resume_refuses_a_different_bank_at_the_same_bridge(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, rust_only_release=True,
                     wait_for_bridge=True)
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.25.0", "--daemon-only", "--json"]
        result = run_arm(repo, env, binary, [*args, "--engine", "rust", "--health-retries", "1"])
        assert result.returncode == 1 and "health gate" in result.stdout, result.stdout
        state = json.loads(state_path.read_text())
        state.update(calls=[], wait_for_bridge=False)
        different = "postgresql://fixture:fixture@pseudolife-pg:5432/different"
        settings = state["running"]["Config"]["Env"]
        settings[0] = "PSEUDOLIFE_MCP_DATABASE_URL=" + different
        state["config"]["services"][SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = different
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, [*args, "--health-retries", "1"])
        assert "cannot verify the legacy import recovery record" in result.stdout, result.stdout
        assert "backup" not in stages(json.loads(state_path.read_text()))


@pytest.mark.parametrize("cut", ["original still running", "removed"])
def test_interrupted_compose_replacement_keeps_pre_import_recovery(tmp_path, cut):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, rust_published=True,
                     rust_only_release=True, interrupted_recreate=cut)
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.25.0", "--daemon-only", "--json", "--health-retries", "1"]
        first = run_arm(repo, env, binary, [*args, "--engine", "rust", "--rollback-tag", "original-fixture"])
        assert first.returncode == 1 and "daemon recreate failed" in first.stdout, first.stdout
        point = json.loads(first.stdout)["rollback"]
        assert point["tag"].endswith("original-fixture-pre-import")
        state = json.loads(state_path.read_text())
        state.update(calls=[], interrupted_recreate=None)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, [*args, "--rollback-tag", "retry-fixture"])
        assert result.returncode != 0, result.stdout
        assert Path(point["backup"]["dump"]).name in result.stdout, result.stdout
        if cut == "original still running":
            report = json.loads(result.stdout)
            steps = "\n".join(report["steps"])
            assert "The original daemon is still running" in steps
            assert "pending-import" in steps and "then rerun the update" in steps
            assert "--pull never" in steps and "pseudolife-mcp restore" not in steps
            assert report["rollback"]["restore_required"] is False
        assert "backup" not in stages(json.loads(state_path.read_text()))


@pytest.mark.parametrize("held_lock", [False, True], ids=["unverified-rust", "busy-update"])
def test_unattended_does_not_accept_a_matching_version_after_unverified_rust_recreate(tmp_path, held_lock):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, rust_only_release=True,
                     no_board_peer=True, wait_for_rust=True)
        state_path.write_text(json.dumps(state))
        args = ["--tag", "0.25.0", "--json", "--health-retries", "1"]
        if held_lock:
            with subprocess.Popen([binary, "update", "--tag", "0.25.0", "--json", "--engine", "rust",
                                   "--health-retries", "50", "--rollback-tag", "original-fixture"],
                                  cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                try:
                    _await_file(state_path.with_name("rust-health-requested"), child)
                    before = stages(json.loads(state_path.read_text()))
                    held = run_arm(repo, env, binary, [*args, "--unattended"])
                    assert held.returncode == 4, held.stdout + held.stderr
                    assert "held off: another pseudolife-mcp update is running" in held.stdout
                    assert not state_path.with_name("notices.jsonl").exists()
                    assert stages(json.loads(state_path.read_text())) == before
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=15)
            return
        first = run_arm(repo, env, binary, [*args, "--engine", "rust", "--rollback-tag", "original-fixture"])
        assert first.returncode == 1 and "health gate" in first.stdout, first.stdout
        original = json.loads(first.stdout)["rollback"]["before_import"]["backup"]["dump"]
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        state = json.loads(state_path.read_text())
        state.update(calls=[], wait_for_rust=False)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, [*args, "--unattended"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert Path(original).name in result.stdout, result.stdout
        assert "to roll back to the imported bank" in result.stdout, result.stdout
        assert "backup" not in stages(json.loads(state_path.read_text()))


@pytest.mark.parametrize("mode", ["attended", "unattended", "checkout"])
def test_rollback_then_update_retires_the_pending_import_pointer(tmp_path, mode):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        state = json.loads(state_path.read_text())
        original = state["running"]
        state.update(target_version="0.25.0", target_schema=59, wait_for_bridge=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.25.0", "--json",
                                             "--health-retries", "1", "--rollback-tag", "original"])
        assert result.returncode == 1 and "health gate" in result.stdout, result.stdout
        rollback = json.loads(result.stdout)["rollback"]
        pointer = _accepted(env).parent / "pending-import"
        original_record = pointer.parent / f"import-{pointer.read_text()}.json"
        assert original_record.is_file()
        state = json.loads(state_path.read_text())
        original["Id"] = "restored-daemon"
        original["Config"]["Image"] = rollback["tag"]
        state.update(running=original, archived_python=False, deployed=False,
                     wait_for_bridge=False, imported=True, running_schema=59, calls=[])
        if mode == "attended":
            # An attended update that refuses (no Rust image for the target)
            # after the pointer check: the Python host is not updated in place.
            state["rust_published"] = False
            args = ["--tag", "0.21.0", "--json"]
        elif mode == "unattended":
            # Compose recreated the daemon from the base image after rollback.
            original.update(Id="replaced-daemon", Image="sha256:later-release")
            original["Config"]["Image"] = "pseudolife-daemon:0.21.0"
            args = ["--unattended", "--tag", "0.21.0", "--json"]
        else:
            # A validated checkout can select the same image under a new activation.
            original["Config"]["Image"] = "pseudolife-daemon-rust:checkout"
            state["target_version"] = "0.21.1"
            (repo / CHECKOUT_SCHEMA_PLAN).write_text(json.dumps({"schema_version": 59}), encoding="utf-8")
            labels = original["Config"]["Labels"]
            labels.update({"org.pseudolife.engine": "rust", "org.pseudolife.checkout": str(repo),
                           "org.pseudolife.activation": "c" * 32})
            selection = repo / "data/update-engine/docker-compose.engine.json"
            selection.parent.mkdir(parents=True)
            selection.write_text(json.dumps({"services": {SERVICE: {"labels": labels}}}))
            args = ["--json", "--daemon-only", "--health-retries", "1"]
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, args)
        assert "cannot verify the legacy import recovery record" not in result.stdout, result.stdout
        assert not pointer.exists()
        assert original_record.is_file()
        assert Path(rollback["backup"]["dump"]).is_file()
        if mode != "checkout":
            # Unattended: a Python host already at the newest release holds
            # for an attended move instead of reporting nothing to do.
            assert result.returncode == (4 if mode == "unattended" else 2), result.stdout
            assert "backup" not in stages(json.loads(state_path.read_text()))
        else:
            assert result.returncode == 0, result.stdout
            assert json.loads(result.stdout)["mode"] == "checkout", result.stdout
        state = json.loads(state_path.read_text())
        state["canonical_missing"] = True
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.21.0", "--json"])
        assert result.returncode == 2 and "--engine requires a Docker host" in result.stdout, result.stdout


def test_bridge_resume_matches_the_bank_after_a_password_change(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59, wait_for_bridge=True)
        state_path.write_text(json.dumps(state))
        args = ["--engine", "rust", "--tag", "0.25.0", "--json", "--health-retries", "1"]
        first = run_arm(repo, env, binary, [*args, "--rollback-tag", "original"])
        assert first.returncode == 1 and "health gate" in first.stdout, first.stdout
        state = json.loads(state_path.read_text())
        new_url = "postgresql://fixture:rotated-fixture@pseudolife-pg:5432/fixture"
        state["running"]["Config"]["Env"][0] = "PSEUDOLIFE_MCP_DATABASE_URL=" + new_url
        state["config"]["services"][SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = new_url
        state.update(calls=[], wait_for_bridge=False)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, args)
        report = json.loads(result.stdout)
        assert result.returncode == 0, result.stdout
        assert report["rollback"]["before_import"] == json.loads(first.stdout)["rollback"]


@pytest.mark.parametrize("different_bank", [False, True])
def test_prior_accepted_source_does_not_retire_an_unfinished_import(tmp_path, different_bank):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        original = state["running"]
        labels = original["Config"]["Labels"]
        labels.update({"org.pseudolife.engine": "rust", "org.pseudolife.release": "0.21.0",
                       "org.pseudolife.activation": "a" * 32})
        accepted = _accepted(env)
        accepted.parent.mkdir(parents=True)
        accepted.write_text(json.dumps({"services": {SERVICE: {"labels": labels}}}))
        state.update(target_version="0.25.0", target_schema=59, wait_for_bridge=True)
        state_path.write_text(json.dumps(state))
        first = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.25.0", "--json",
                                            "--health-retries", "1", "--rollback-tag", "original"])
        assert first.returncode == 1 and "health gate" in first.stdout, first.stdout
        state = json.loads(state_path.read_text())
        original["Id"] = "replacement-with-prior-activation"
        state.update(running=original, archived_python=False, deployed=False, wait_for_bridge=False, calls=[])
        if different_bank:
            new_url = "postgresql://fixture:fixture@pseudolife-pg:5432/different"
            original["Config"]["Env"][0] = "PSEUDOLIFE_MCP_DATABASE_URL=" + new_url
            state["config"]["services"][SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = new_url
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "0.21.0", "--daemon-only", "--json"])
        assert result.returncode != 0, result.stdout
        assert "pending-import" in result.stdout, result.stdout
        assert (accepted.parent / "pending-import").is_file()
        assert "backup" not in stages(json.loads(state_path.read_text()))


def test_bridge_resume_refuses_a_changed_effective_database_host(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        def set_host(state, addr):
            url = f"postgresql://fixture:fixture@pseudolife-pg:5432/fixture?hostaddr={addr}"
            state["running"]["Config"]["Env"][0] = "PSEUDOLIFE_MCP_DATABASE_URL=" + url
            state["config"]["services"][SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = url
            state["pg_address"] = addr

        state = json.loads(state_path.read_text())
        set_host(state, "192.0.2.10")
        state.update(target_version="0.25.0", target_schema=59, wait_for_bridge=True)
        state_path.write_text(json.dumps(state))
        args = ["--engine", "rust", "--tag", "0.25.0", "--json", "--health-retries", "1"]
        first = run_arm(repo, env, binary, [*args, "--rollback-tag", "original"])
        assert first.returncode == 1 and "health gate" in first.stdout, first.stdout
        state = json.loads(state_path.read_text())
        set_host(state, "192.0.2.11")
        state.update(calls=[], wait_for_bridge=False)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, args)
        assert "cannot verify the legacy import recovery record" in result.stdout, result.stdout
        assert "backup" not in stages(json.loads(state_path.read_text()))


def test_schema_56_without_weights_needs_no_counter_row_after_import(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "old_schema") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_version="0.25.0", target_schema=59,
                     counters_missing=True, rust_only_release=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--engine", "rust", "--tag", "0.25.0", "--json",
                                             "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


def test_resume_cannot_force_the_original_rollback_tag_onto_the_bridge(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(running_version="0.23.1", running_schema=59, target_version="0.25.0",
                     target_schema=59, wait_for_bridge=True)
        state_path.write_text(json.dumps(state))
        args = ["--engine", "rust", "--tag", "0.25.0", "--daemon-only", "--json",
                "--health-retries", "1"]
        first = run_arm(repo, env, binary, [*args, "--rollback-tag", "original"])
        assert first.returncode == 1 and "health gate" in first.stdout, first.stdout
        point = json.loads(first.stdout)["rollback"]
        state = json.loads(state_path.read_text())
        assert state["images"][point["tag"]] == point["image_id"]
        state.update(calls=[], wait_for_bridge=False)
        state_path.write_text(json.dumps(state))
        resumed = run_arm(repo, env, binary, [*args, "--rollback-tag", "original-pre-import",
                                               "--force-rollback-tag"])
        assert resumed.returncode != 0, resumed.stdout
        assert "reserved for the original pre-import rollback" in resumed.stdout, resumed.stdout
        assert json.loads(state_path.read_text())["images"][point["tag"]] == point["image_id"]


@pytest.mark.parametrize("native", [True], ids=["native"])
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
        state.update(rust_published=True, rust_only_release=True, target_schema=59)
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
                i for i, s in enumerate(report["steps"]) if "recreating the daemon container" in s)
        else:
            assert result.returncode == 1, result.stdout + result.stderr
            assert client_steps == [], report["steps"]


@pytest.mark.parametrize("native", [True], ids=["native"])
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
        state.update(rust_published=True, rust_only_release=True, target_schema=59)
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
        assert bool(json.loads(result.stdout)["rollback"]) == (failure == "rust-leg")
        assert (_labels(state_path) or {}).get("org.pseudolife.engine", "python") == "python"


@pytest.mark.parametrize("native", [True], ids=["native"])
def test_an_unreadable_choice_record_no_longer_keeps_python(tmp_path, native):
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
        assert "cannot read the engine choice" in result.stdout
        assert "no recorded choice keeps it" in result.stdout
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@pytest.mark.parametrize("native", [True], ids=["native"])
def test_a_recorded_python_choice_no_longer_keeps_python(tmp_path, native):
    # An earlier --engine python left this record; the retirement overrides it
    # and says so.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        choice = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/engine-choice.json"
        choice.parent.mkdir(parents=True, exist_ok=True)
        choice.write_text('{"engine": "python"}\n')
        result = run_arm(repo, env, binary if native else None,
                         ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1", "--rollback-tag", "pre-kept"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert "recorded a Python daemon choice" in result.stdout and "no longer applies" in result.stdout
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("failure", ["pending_import", "old_schema"])
def test_release_rust_imports_legacy_counters_on_python_first(tmp_path, native, failure):
    # G2-K: the Rust daemon never reads memory_state/weights.pt. Update first
    # moves to the archived Python daemon, whose startup imports it.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, failure) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(rust_only_release=True, target_schema=59, list_images=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--keep-rollbacks", "0"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        # The Rust image is pulled first, before the Python leg changes anything.
        assert stages(state) == ["pull", "pull", "backup", "tag", "up", "backup", "tag", "up"]
        pulled = [a[-1] for a in state["calls"] if a[:1] == ["pull"]]
        assert pulled == ["ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1",
                          ARCHIVED_PYTHON]
        assert "archived Python daemon 0.23.1" in result.stdout
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        # The original image's rollback survives beside the second leg's.
        rollback = json.loads(result.stdout)["rollback"]
        assert rollback["state"] == "tagged"
        assert rollback["before_import"]["state"] == "tagged"
        assert rollback["before_import"]["tag"].endswith("-pre-import")
        assert rollback["tag"] != rollback["before_import"]["tag"]
        assert rollback["before_import"]["tag"] in state["images"] and rollback["tag"] in state["images"]
        for slot in ("dump", "state_archive"):
            original = Path(rollback["before_import"]["backup"][slot])
            assert original.is_file() and original != Path(rollback["backup"][slot])
            assert original.parent.name == "pre-import"


@pytest.mark.parametrize("native", [True], ids=["native"])
def test_release_rust_refuses_an_unreadable_schema_without_an_import_leg(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "unreadable_schema") as (repo, env, state_path):
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "nothing was deployed" in result.stdout
        assert "schema version is unreadable" in result.stdout
        state = json.loads(state_path.read_text())
        assert "backup" not in stages(state) and "up" not in stages(state)


@pytest.mark.parametrize("native", [True], ids=["native"])
def test_release_rust_stops_on_python_when_the_import_does_not_happen(tmp_path, native):
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(import_stuck=True, target_schema=59)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "pre-stuck"])
        # The Python leg changed the host, so this is a failure, not a refusal.
        assert result.returncode == 1, result.stdout + result.stderr
        assert "did not finish importing the bank's schema and CMS counters" in result.stdout
        assert json.loads(result.stdout)["rollback"]["restore_required"] is True
        assert "pseudolife-mcp restore --container " in result.stdout and "fixture-pg" in result.stdout
        state = json.loads(state_path.read_text())
        assert stages(state) == ["pull", "pull", "backup", "tag", "up"]
        assert _labels(state_path)["org.pseudolife.engine"] == "python"



@pytest.mark.parametrize(("detail", "diagnostic"), [
    ("could not download https://example.invalid/model?token=fixture-secret (DNS error)", "DNS error"),
    ("could not write '/home/private-operator/model file': No space left on device", "No space left on device"),
    ("model checksum mismatch for /home/private-operator/model: expected fixture-secret", "model checksum mismatch"),
])
def test_failed_provision_before_import_keeps_the_original_daemon(tmp_path, detail, diagnostic):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_schema=59, target_version="0.24.0", provision_exit=47)
        state["provision_stderr"] = "read-only file system\n" + "x" * 8192 + "\n" + detail + "\n"
        state_path.write_text(json.dumps(state))
        args = ["--engine", "rust", "--tag", "0.24.0", "--daemon-only", "--json", "--health-retries", "1",
                "--rollback-tag", "pre-stuck"]
        result = run_arm(repo, env, binary, args)
        assert result.returncode == 1, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert not report["rollback"]
        message = "\n".join(report["steps"])
        assert "nothing was deployed" in message.lower()
        assert "exit 47" in message and diagnostic in message
        assert "starting model provision (stdout)" not in message
        assert "fixture-secret" not in message and "private-operator" not in message
        assert "read-only file system" not in message

        state = json.loads(state_path.read_text())
        assert not state["deployed"]
        assert not {"backup", "tag", "up"}.intersection(stages(state))
        state.update(calls=[], provision_exit=0)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, args)
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert stages(state) == ["pull", "pull", "backup", "tag", "up", "backup", "tag", "up"]


def test_failed_rust_recreate_after_import_reports_attempt_and_original_rollback(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_schema=59, fail_rust_recreate=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary,
                         ["--engine", "rust", "--tag", "0.21.1", "--daemon-only", "--json",
                          "--health-retries", "1", "--rollback-tag", "pre-stuck"])
        assert result.returncode == 1, result.stdout + result.stderr
        report = json.loads(result.stdout)
        message = "\n".join(report["steps"])
        assert "The bank import completed on the Python 0.23.1 bridge at schema v59." in message
        assert "The Rust replacement was attempted and its health could not be verified." in message
        assert "The pre-import backups and rollback point are available." in message
        assert "restore the host and bank from before the import" in message
        assert "pseudolife-mcp restore --container " in message
        assert "rerun the same update to finish" not in message


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
def test_release_rust_import_uses_distinct_rollback_tags_when_target_already_runs_on_python(tmp_path, native):
    # The Python daemon already runs the selected release, but the archived
    # daemon still imports the bank before the Rust image starts.
    binary = _native_binary()
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "pending_import") as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(target_version="0.21.0", target_schema=59)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary if native else None,
                         ["--engine", "rust", "--tag", "0.21.0", "--daemon-only", "--json", "--health-retries", "1",
                          "--rollback-tag", "fixed"])
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert stages(state) == ["pull", "pull", "backup", "tag", "up", "backup", "tag", "up"]
        tags = [a[-1] for a in state["calls"] if a[:1] == ["tag"]]
        assert len(tags) == 2 and tags[0] != tags[1], tags
        assert tags[0].endswith("-fixed-pre-import") and tags[1].endswith("-fixed")
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


@only_frozen_python
@pytest.mark.parametrize("native_first", [True, False], ids=["native-first", "python-first"])
def test_release_rust_selection_is_shared_by_both_updaters(tmp_path, native_first):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        arms = [native_first, not native_first, native_first]
        rounds = [["--engine", "rust"], [], []]
        for index, (native, extra) in enumerate(zip(arms, rounds)):
            result = run_arm(repo, env, binary if native else None,
                             [*extra, "--tag", "0.21.1", "--reinstall", "--daemon-only", "--json",
                              "--health-retries", "1", "--rollback-tag", f"pre-round-{index}"])
            assert result.returncode == 0, result.stdout + result.stderr
            assert _labels(state_path)["org.pseudolife.engine"] == "rust"


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


@only_frozen_python
@pytest.mark.parametrize("native_first", [True, False])
def test_release_updaters_can_follow_each_other(tmp_path, native_first):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        for index, native in enumerate([native_first, not native_first]):
            args = ["--tag", "0.21.1", "--daemon-only", "--reinstall", "--json",
                    "--health-retries", "1", "--rollback-tag", f"pre-round-{index}"]
            result = run_arm(repo, env, binary if native else None, args)
            assert result.returncode == 0, result.stdout + result.stderr
            # Both move a Python host whose release publishes Rust (the frozen
            # updater by its G2 default, the native one because Python is
            # retired), and both keep Rust after that.
            labels = json.loads(state_path.read_text())["running"]["Config"]["Labels"]
            assert labels["org.pseudolife.engine"] == "rust", labels
            for backup in (repo / "data/backups").glob("pseudolife_*"):
                backup.unlink()


PYTHON_UPDATER_HOSTS = json.loads(
    (Path(__file__).with_name("fixtures") / "python-updater-hosts.json").read_text(encoding="utf-8"))["hosts"]


def _as_python_updater_left_it(state_path, round_name, platform=None):
    """Make the fixture host what the frozen Python updater left after one
    release round (``round_name``, captured in python-updater-hosts.json):
    its changed fixture state and the files it wrote under the data
    directory, byte for byte. The calls it made are not replayed; the next
    updater starts a fresh call log."""
    platform = platform or ("windows" if os.name == "nt" else "linux")
    captured = PYTHON_UPDATER_HOSTS[platform][round_name]
    home = state_path.parent
    spelled = json.dumps(str(home))[1:-1]
    state = json.loads(state_path.read_text())
    state.update(json.loads(json.dumps(captured["state"]).replace("{HOME}", spelled)))
    state_path.write_text(json.dumps(state))
    for relative, body in captured["files"].items():
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Inside a JSON file the home is spelled as a JSON string's content.
        # Bytes, not text: the captured bodies keep the line endings the
        # updater wrote (CRLF on Windows), which a text write would double.
        target.write_bytes(body.replace("{HOME}", spelled if relative.endswith(".json") else str(home))
                           .encode("utf-8"))
    return captured


@pytest.mark.parametrize("platform", ["linux", "windows"])
@pytest.mark.parametrize("round_name", ["engine-rust", "engine-rust-then-plain", "plain"])
def test_python_updater_host_files_are_written_byte_for_byte(tmp_path, platform, round_name):
    home = tmp_path / "home"
    home.mkdir()
    state_path = home / "fixture-state.json"
    state_path.write_text(json.dumps({"calls": []}))
    captured = _as_python_updater_left_it(state_path, round_name, platform)
    spelled = json.dumps(str(home))[1:-1]
    # A plain round writes no data files (its update lock is left out of the capture).
    assert bool(captured["files"]) is (round_name != "plain")
    for relative, body in captured["files"].items():
        expected = body.replace("{HOME}", spelled if relative.endswith(".json") else str(home))
        assert (home / relative).read_bytes() == expected.encode("utf-8"), relative
    if platform == "windows" and captured["files"]:
        assert any("\r\n" in body for body in captured["files"].values())
    assert not any("\r\r\n" in body for body in captured["files"].values())


def test_python_updater_host_capture_is_the_round_it_names():
    # Both platforms: each runs the replay cases with its own capture.
    assert sorted(PYTHON_UPDATER_HOSTS) == ["linux", "windows"]
    for rounds in PYTHON_UPDATER_HOSTS.values():
        assert sorted(rounds) == ["engine-rust", "engine-rust-then-plain", "plain"]
        rust, plain = rounds["engine-rust"], rounds["plain"]
        kept = rounds["engine-rust-then-plain"]
        assert "--engine" not in kept["args"]
        assert kept["state"]["running"]["Config"]["Labels"]["org.pseudolife.engine"] == "rust"
        assert rust["args"][:2] == ["--engine", "rust"] and "--engine" not in plain["args"]
        assert rust["state"]["running"]["Config"]["Labels"]["org.pseudolife.engine"] == "rust"
        assert "org.pseudolife.engine" not in plain["state"]["running"]["Config"]["Labels"]
        assert rust["state"]["deployed"] is plain["state"]["deployed"] is True


def test_native_updater_follows_the_python_updaters_rust_selection(tmp_path):
    """A plain native update keeps the Rust engine the Python updater chose."""
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        _as_python_updater_left_it(state_path, "engine-rust")
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--reinstall", "--daemon-only", "--json",
                                             "--health-retries", "1", "--rollback-tag", "pre-round-1"])
        assert result.returncode == 0, result.stdout + result.stderr
        # A real reinstall over the Python updater's host: backup, rollback
        # tag and a recreated daemon, in this run's own call log.
        assert stages(json.loads(state_path.read_text())) == ["pull", "backup", "tag", "up"]
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


def test_native_updater_keeps_the_rust_a_python_update_kept(tmp_path):
    """A plain native update keeps the Rust engine a plain Python update kept
    (after its own --engine rust round)."""
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        _as_python_updater_left_it(state_path, "engine-rust-then-plain")
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--reinstall",
                                             "--daemon-only", "--json", "--health-retries", "1",
                                             "--rollback-tag", "pre-round-2"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert stages(json.loads(state_path.read_text())) == ["pull", "backup", "tag", "up"]
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


def test_native_updater_moves_the_python_host_a_plain_python_update_left(tmp_path):
    """The native updater completes over a host a plain Python update left,
    and moves it to Rust: the Python daemon is retired."""
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        _as_python_updater_left_it(state_path, "plain")
        args = ["--tag", "0.21.1", "--daemon-only", "--reinstall", "--json",
                "--health-retries", "1", "--rollback-tag", "pre-round-1"]
        result = run_arm(repo, env, binary, args)
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        assert stages(state) == ["pull", "backup", "tag", "up"]
        assert state["running"]["Config"]["Labels"]["org.pseudolife.engine"] == "rust"


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
        if mode == "release":
            # The explicit move names the same way out.
            explicit = run_arm(repo, env, binary, ["--engine", "rust", *args, "--json", "--health-retries", "1"])
            assert explicit.returncode == 2, explicit.stdout + explicit.stderr
            assert "--allow-auth-change" in explicit.stdout, explicit.stdout
            # The move to Rust needs a bearer, which only the explicit handover
            # adds; the Python daemon is left as it was.
            assert result.returncode == 2, result.stdout + result.stderr
            assert "the engine health gate needs bearer auth" in result.stdout
            assert "--allow-auth-change" in result.stdout
            assert not stages(json.loads(state_path.read_text()))
            return
        assert result.returncode == 0, result.stdout + result.stderr
        assert stages(json.loads(state_path.read_text()))[-2:] == ["tag", "up"]
        assert not (repo / "ops/.env").exists()


def test_plain_release_refuses_to_move_an_unhealthy_python_daemon(tmp_path):
    # The move reads the running bank first, so an unhealthy Python daemon is
    # left as it was (it used to be updated in place on Python).
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path, "stopped") as (repo, env, state_path):
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "does not answer healthy" in result.stdout and "docker start pseudolife-mcp-postgres" in result.stdout
        assert "was left as it was and nothing was deployed" in result.stdout
        assert "backup" not in stages(json.loads(state_path.read_text()))


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


@pytest.mark.parametrize("native", [True], ids=["native"])
@pytest.mark.parametrize("container", [None, "second-project-daemon", "missing-daemon"])
def test_maintainer_setup_explicit_container_scope(tmp_path, native, container):
    binary = _native_binary() if native else None
    if native and not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        command = [binary, "maintainer", "setup", "--check"]
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
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


@pytest.mark.parametrize("native", [True], ids=["native"])
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


def test_a_tokenless_python_activation_is_told_the_bearer_handover(tmp_path):
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
        # An accepted Python activation no longer keeps Python, and the move
        # needs the bearer it lacks: the refusal names the handover.
        assert result.returncode == 2, result.stdout + result.stderr
        assert "the engine health gate needs bearer auth" in result.stdout
        assert "--allow-auth-change" in result.stdout
        assert _labels(state_path)["org.pseudolife.engine"] == "python"


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


# The ownership order rule (cli::ownership, update/ownership.rs): probe
# first; repair the model cache if it needs it; a state volume the daemon
# account cannot use is backed up through the old container first and only
# then repaired; with no container able to read it, repaired then backed up.
def ownership_stages(state):
    """Every stage, with the ownership helpers named by what they do."""
    result = []
    for args in state["calls"]:
        if args[:1] == ["run"] and "--probe-access" in args:
            result.append("probe")
        elif args[:1] == ["run"] and "--list-root-owned" in args:
            roots = args[args.index("--list-root-owned") + 1:]
            result.append("list " + " ".join(roots))
        elif args[:1] == ["run"] and "--repair-ownership" in args:
            roots = args[args.index("--repair-ownership") + 1:]
            result.append("repair " + " ".join(roots))
        elif args[:1] == ["run"] and "check-config" in args:
            result.append("check-config")
        elif args[:1] == ["run"] and "provision" in args:
            result.append("provision")
        else:
            result.extend(stages({"calls": [args]}))
    return result


def ownership_host(tmp_path, mode, unusable=(), user="", running=True, models=False, failure=""):
    context = host(tmp_path, failure, mode)
    repo, env, state_path = context.__enter__()
    state = json.loads(state_path.read_text())
    state["ownership"] = {"unusable": [list(entry) for entry in unusable]}
    state["running"]["Config"]["User"] = user
    state["running"]["State"]["Running"] = running
    if models:
        state["running"]["Mounts"].append({"Type": "volume", "Name": "fixture-models", "Destination": "/models", "RW": True})
        state["running"]["Config"]["Env"].append("PSEUDOLIFE_DAEMON_MODELS_DIR=/models")
        # The project declares the same cache, so the mount identity holds.
        daemon = state["config"]["services"][SERVICE]
        daemon["volumes"].append({"type": "volume", "source": "models", "target": "/models"})
        daemon["environment"]["PSEUDOLIFE_DAEMON_MODELS_DIR"] = "/models"
        state["config"]["volumes"]["models"] = {"name": "fixture-models"}
        state["volume_inspections"]["fixture-models"] = [{"Name": "fixture-models"}]
    state_path.write_text(json.dumps(state))
    return context, repo, env, state_path


def ownership_args(mode, repo):
    common = ["--json", "--no-cache-prune", "--health-retries", "1", "--health-delay-ms", "0"]
    if mode == "release":
        return [*common, "--tag", "0.21.1", "--daemon-only"]
    if mode == "engine":
        # A checkout's engine selection: update::engine::deploy.
        return [*common, "--checkout", str(repo), "--no-test-login", "--engine", "rust"]
    return [*common, "--checkout", str(repo), "--no-test-login"]


def root_helpers(state):
    return [call for call in state["calls"] if "--cap-add" in call or "0:0" in call]


@pytest.mark.parametrize("mode", ["release", "checkout", "engine"])
def test_a_healthy_host_probes_before_any_change_and_runs_no_root_container_for_ownership(tmp_path, mode):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    context, repo, env, state_path = ownership_host(tmp_path, mode)
    try:
        result = run_arm(repo, env, binary, ownership_args(mode, repo))
        assert result.returncode == 0, result.stdout + result.stderr
        state = json.loads(state_path.read_text())
        seen = ownership_stages(state)
        # Only the image pull (which changes no volume) may come first.
        assert [s for s in seen if s != "pull"][0] == "probe", seen
        assert seen.count("probe") == 1 and not any(s.startswith("repair") for s in seen), seen
        assert root_helpers(state) == []
        probe = next(call for call in state["calls"] if "--probe-access" in call)
        assert probe[probe.index("--user") + 1] == "10001:10001"
        assert probe[probe.index("--cap-drop") + 1] == "ALL"
        assert "--read-only" in probe and probe[probe.index("--network") + 1] == "none"
        assert not (Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "ownership-repair").exists()
    finally:
        context.__exit__(None, None, None)


def test_a_model_cache_is_listed_repaired_and_reprobed_and_the_state_is_untouched(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    context, repo, env, state_path = ownership_host(tmp_path, "release", unusable=[("0", "/models/m")], models=True)
    try:
        result = run_arm(repo, env, binary, ownership_args("release", repo))
        assert result.returncode == 0, result.stdout + result.stderr
        seen = ownership_stages(json.loads(state_path.read_text()))
        # The pull and the schema check change nothing and come first; the
        # undo list is written before the change, which is reprobed.
        start = seen.index("probe")
        assert seen.index("pull") < start, seen
        assert seen[start:start + 4] == ["probe", "list /models", "repair /models", "probe"], seen
        assert seen.index("repair /models") < seen.index("backup"), seen
        assert "repair /data" not in seen and "list /data" not in seen
        logs = list((Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "ownership-repair").iterdir())
        assert [log.name.endswith("-models.log") for log in logs] == [True]
        assert logs[0].read_text().endswith("0:0\t/models/m\n")
        assert "model volume" in result.stdout and str(logs[0]).replace("\\", "\\\\") in result.stdout
    finally:
        context.__exit__(None, None, None)


@pytest.mark.parametrize("mode", ["release", "checkout", "engine"])
def test_an_unusable_state_volume_is_backed_up_through_the_old_container_before_its_repair(tmp_path, mode):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    context, repo, env, state_path = ownership_host(tmp_path, mode, unusable=[("0", "/data/old")])
    try:
        result = run_arm(repo, env, binary, ownership_args(mode, repo))
        assert result.returncode == 0, result.stdout + result.stderr
        seen = ownership_stages(json.loads(state_path.read_text()))
        assert seen.count("backup") == 1, seen
        assert seen.index("probe") < seen.index("backup") < seen.index("list /data") < seen.index("repair /data") < seen.index("up"), seen
        # The repair is followed by its own probe, which found nothing left.
        assert seen[seen.index("repair /data") + 1] == "probe", seen
        log = next((Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "ownership-repair").iterdir())
        assert log.read_text().endswith("0:0\t/data/old\n")
        assert "state volume" in result.stdout
    finally:
        context.__exit__(None, None, None)


@pytest.mark.parametrize("user,running,first", [("10001:10001", True, "repair /data"), ("", False, "backup"),
                                                  ("10001:10001", False, "backup")])
def test_state_only_a_running_daemon_account_container_holds_is_repaired_before_the_backup(tmp_path, user, running, first):
    """A running container that already is the daemon account cannot archive
    root's files: repair, then back up. A stopped one is archived through
    the uid-0 read-only state helper first, as on a root-run host."""
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    context, repo, env, state_path = ownership_host(tmp_path, "checkout", unusable=[("0", "/data/old")], user=user, running=running)
    try:
        result = run_arm(repo, env, binary, ownership_args("checkout", repo))
        assert result.returncode == 0, result.stdout + result.stderr
        seen = ownership_stages(json.loads(state_path.read_text()))
        assert "repair /data" in seen and "backup" in seen, seen
        other = "backup" if first == "repair /data" else "repair /data"
        assert seen.index(first) < seen.index(other), seen
    finally:
        context.__exit__(None, None, None)


@pytest.mark.parametrize("mode", ["release", "checkout"])
def test_a_second_failed_probe_refuses_with_the_path_and_changes_nothing_more(tmp_path, mode):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    # uid 999 owns it: a repair of uid-0 entries cannot help.
    context, repo, env, state_path = ownership_host(tmp_path, mode, unusable=[("999", "/data/theirs")])
    try:
        result = run_arm(repo, env, binary, ownership_args(mode, repo))
        assert result.returncode != 0, result.stdout + result.stderr
        assert "/data/theirs" in result.stdout and "Give it to 10001:10001" in result.stdout
        assert "nothing was deployed" in result.stdout
        seen = ownership_stages(json.loads(state_path.read_text()))
        # Nothing root-owned to list: no chown helper, only the probe again.
        after = seen[seen.index("list /data") + 1:]
        assert after == ["probe"], seen
        assert "repair /data" not in seen
        assert seen.index("backup") < seen.index("list /data"), seen
    finally:
        context.__exit__(None, None, None)


def test_no_backup_with_an_unusable_state_volume_refuses_before_any_repair(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    context, repo, env, state_path = ownership_host(tmp_path, "release", unusable=[("0", "/data/old"), ("0", "/models/m")], models=True)
    try:
        # A Rust host: on a Python one --no-backup is refused before this guard.
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, [*ownership_args("release", repo), "--no-backup"])
        assert result.returncode != 0, result.stdout + result.stderr
        assert "--no-backup" in result.stdout and "/data/old" in result.stdout
        state = json.loads(state_path.read_text())
        assert ownership_stages(state) == ["pull", "probe"]
        assert root_helpers(state) == []
    finally:
        context.__exit__(None, None, None)


def test_a_state_archive_that_cannot_be_made_stops_before_the_repair(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    context, repo, env, state_path = ownership_host(tmp_path, "checkout", unusable=[("0", "/data/old")], failure="missing_state")
    try:
        for name in ("backup.sh", "backup.ps1"):
            (repo / "ops" / name).unlink(missing_ok=True)
        result = run_arm(repo, env, binary, ownership_args("checkout", repo))
        assert result.returncode != 0, result.stdout + result.stderr
        assert "ownership was not repaired" in result.stdout or "state volume backup" in result.stdout
        state = json.loads(state_path.read_text())
        assert not any(s.startswith("repair") for s in ownership_stages(state))
        assert root_helpers(state) == []
    finally:
        context.__exit__(None, None, None)


@pytest.mark.parametrize("code", [127, 70, 1])
def test_a_probe_that_cannot_run_refuses_before_anything_changes(tmp_path, code):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to the built native CLI")
    context, repo, env, state_path = ownership_host(tmp_path, "release")
    try:
        state = json.loads(state_path.read_text())
        state["ownership"]["probe_code"] = code
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ownership_args("release", repo))
        assert result.returncode != 0, result.stdout + result.stderr
        assert "cannot check whether the daemon account can use its volumes" in result.stdout
        state = json.loads(state_path.read_text())
        assert ownership_stages(state) == ["pull", "probe"]
        # The failed helper is removed by its name.
        probe = next(call for call in state["calls"] if "--probe-access" in call)
        assert ["rm", "-f", "-v", probe[probe.index("--name") + 1]] in state["calls"]
    finally:
        context.__exit__(None, None, None)
TUNNEL_OWNER = "PseudolifeTunnelOwner:" + "3c3b30db37fcc1368d48917c2405a0b06b94add3faba142553cb8649e81527d7"


def _python_client_with_tunnel_service(env):
    """A machine on the Python client: its stable launcher is the Python
    selector, Claude Code runs it, and `tunnel service install` registered a
    systemd user unit for profile `dot` (issue #1632). Returns the launcher
    and the unit."""
    home = Path(env["HOME"])
    launcher = Path(env["PSEUDOLIFE_SHIM_LAUNCHER"])
    launcher.write_text("#!/bin/sh\n# Python selector fixture\nexit 0\n")
    launcher.chmod(0o755)
    (home / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"pseudolife-memory": {"type": "stdio", "command": str(launcher), "args": []}}}))
    unit = home / ".config/systemd/user/pseudolife-tunnel-dot.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f"# {TUNNEL_OWNER}\n[Unit]\nDescription=Optional Pseudolife MCP tunnel (dot)\n[Service]\n"
                    f"ExecStart=\"{launcher}\" \"tunnel\" \"run\" \"--profile\" \"dot\"\nRestart=on-failure\n"
                    "RestartSec=5\n[Install]\nWantedBy=default.target\n")
    # The host chose the Python daemon, so an update to the running release
    # has nothing to do.
    choice = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/engine-choice.json"
    choice.parent.mkdir(parents=True, exist_ok=True)
    choice.write_text('{"engine": "python"}\n')
    return launcher, unit


@pytest.mark.skipif(sys.platform != "linux", reason="the systemd user unit is the Linux registration")
@pytest.mark.parametrize("clients_only", [False, True], ids=["release", "clients-only"])
def test_a_registered_tunnel_service_keeps_the_python_client_and_changes_nothing(tmp_path, clients_only):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        launcher, unit = _python_client_with_tunnel_service(env)
        before = {p: p.read_bytes() for p in (launcher, unit, Path(env["HOME"]) / ".claude.json")}
        args = ["--tag", "0.21.1", "--health-retries", "1", "--rollback-tag", "pre-tunnel"]
        if clients_only:
            args = ["--clients-only", *args]
        result = run_arm(repo, env, binary, args)
        assert result.returncode == 2, result.stdout + result.stderr
        out = result.stdout + result.stderr
        assert "this machine stays on the Python client" in out, out
        assert "pseudolife-mcp tunnel run" in out, out
        assert "The native client has no tunnel service command to manage that registration after the move." in out, out
        assert ("cannot run " "a tunnel yet") not in out, out
        assert f"systemd user unit {unit}" in out, out
        assert "Nothing was changed" in out, out
        assert stages(json.loads(state_path.read_text())) == [], "no pull, backup or recreate"
        assert {p: p.read_bytes() for p in before} == before, "launcher, unit and registration are byte-identical"
        assert not Path(env["PSEUDOLIFE_SHIM_RUNTIMES"]).exists(), "no runtime was installed"


@pytest.mark.skipif(sys.platform != "linux", reason="the systemd user unit is the Linux registration")
def test_an_update_with_nothing_to_do_is_not_refused_for_a_tunnel_service(tmp_path):
    # The running daemon already is the release and nothing else changes:
    # the client steps never run, so the launcher would not move (Codex).
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        _python_client_with_tunnel_service(env)
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "0.21.0", "--health-retries", "1"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert "nothing to do" in result.stdout, result.stdout
        assert "stays on the Python client" not in result.stdout + result.stderr


@contextmanager
def _client_machine(tmp_path):
    """A disposable client machine: no Docker, a loopback daemon that
    answers only /health with release 0.21.1 (a client update reads the
    daemon's release first), and every path the native client reads under
    its own home."""
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "version": "0.21.1"}).encode()
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "client-home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PSEUDOLIFE_", "XDG_", "CLAUDE_", "CODEX_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), PATH=os.environ.get("PATH", ""),
               CODEX_HOME=str(home / "codex"),
               PSEUDOLIFE_DOCKER=str(home / "no-docker-here"),
               PSEUDOLIFE_MCP_DATA_DIR=str(home / "data"),
               PSEUDOLIFE_MCP_DAEMON_URL=f"http://127.0.0.1:{server.server_address[1]}",
               # Never the real release: past the guard a download is a 404.
               PSEUDOLIFE_RELEASE_BASE_URL=f"http://127.0.0.1:{server.server_address[1]}",
               PSEUDOLIFE_RELEASE_API_BASE_URL=f"http://127.0.0.1:{server.server_address[1]}/releases/tags",
               PSEUDOLIFE_SHIM_RUNTIMES=str(home / "runtimes"),
               PSEUDOLIFE_SHIM_LAUNCHER=str(home / "bin/pseudolife-mcp"))
    Path(env["PSEUDOLIFE_SHIM_LAUNCHER"]).parent.mkdir(parents=True)
    try:
        yield env
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="the systemd user unit is the Linux registration")
def test_a_client_machine_with_a_tunnel_service_stays_on_the_python_client(tmp_path):
    # The update bridge's usual target: a client machine on the Python client.
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with _client_machine(tmp_path) as env:
        launcher, unit = _python_client_with_tunnel_service(env)
        before = launcher.read_bytes()
        result = subprocess.run([binary, "update", "--clients-only", "--tag", "0.21.1"], env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 2, result.stdout + result.stderr
        out = result.stdout + result.stderr
        assert "this machine stays on the Python client" in out, out
        assert "rerun the same command with PSEUDOLIFE_SHIM_ENGINE=python in front of it" in out, out
        assert "without PSEUDOLIFE_SHIM_ENGINE=native" not in out, out
        assert f"pseudolife-mcp tunnel service remove --profile dot), then rerun it. Registered: systemd user unit {unit}" in out, out
        assert launcher.read_bytes() == before
        assert not Path(env["PSEUDOLIFE_SHIM_RUNTIMES"]).exists()
        # The same machine with the unit removed moves on past the guard.
        unit.unlink()
        moved = subprocess.run([binary, "update", "--clients-only", "--tag", "0.21.1"], env=env, cwd=tmp_path,
                               capture_output=True, text=True, timeout=60)
        assert "stays on the Python client" not in moved.stdout + moved.stderr, moved.stdout + moved.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="the systemd user unit is the Linux registration")
def test_runtime_install_refuses_the_move_before_reading_its_archive(tmp_path):
    # What both native installers run; they keep the Python client on exit 3.
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with _client_machine(tmp_path) as env:
        launcher, _unit = _python_client_with_tunnel_service(env)
        (Path(env["HOME"]) / ".claude.json").unlink()   # the installer moves with no registration
        before = launcher.read_bytes()
        probe = subprocess.run([binary, "runtime", "install", "--check"], env=env, cwd=tmp_path,
                               capture_output=True, text=True, timeout=60)
        assert probe.returncode == 3, probe.stdout + probe.stderr
        assert "The native client has no tunnel service command to manage that registration after the move." in probe.stderr
        missing = tmp_path / "never-read"
        result = subprocess.run([binary, "runtime", "install", "--archive", str(missing / "a.tar.gz"),
                                 "--sha256sums", str(missing / "SHA256SUMS"), "--version", "0.21.1"],
                                env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert result.returncode == 3, result.stdout + result.stderr
        assert "runtime install refused: this machine stays on the Python client" in result.stderr
        assert "rerun the installer with PSEUDOLIFE_SHIM_ENGINE=python in front of it" in result.stderr
        assert launcher.read_bytes() == before
        assert not Path(env["PSEUDOLIFE_SHIM_RUNTIMES"]).exists()


@pytest.mark.skipif(sys.platform != "linux", reason="the systemd user unit is the Linux registration")
def test_the_unit_under_home_is_found_when_userprofile_points_elsewhere(tmp_path):
    # A WSL session may export a Windows USERPROFILE beside HOME; the Python
    # client wrote the unit under HOME, so the guard reads it there.
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with _client_machine(tmp_path) as env:
        launcher, unit = _python_client_with_tunnel_service(env)
        elsewhere = tmp_path / "windows-profile"
        elsewhere.mkdir()
        env = {**env, "USERPROFILE": str(elsewhere),
               # The layout follows its own override, as the installers set it.
               "PSEUDOLIFE_SHIM_LAUNCHER": str(launcher)}
        # The registration is read where runtime::home() looks: USERPROFILE.
        (elsewhere / ".claude.json").write_text((Path(env["HOME"]) / ".claude.json").read_text())
        result = subprocess.run([binary, "update", "--clients-only", "--tag", "0.21.1"], env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 2, result.stdout + result.stderr
        assert f"Registered: systemd user unit {unit}" in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="the systemd user unit is the Linux registration")
def test_an_unattended_update_with_a_tunnel_service_updates_the_daemon_alone(tmp_path):
    # The daemon is not left behind every night for a client-side service:
    # the scheduled run deploys exactly as --daemon-only would, leaves the
    # client on Python, and says why in its output, its log and its notice.
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        launcher, unit = _python_client_with_tunnel_service(env)
        before = launcher.read_bytes()
        state = json.loads(state_path.read_text())
        state["no_board_peer"] = True  # an idle board: the unattended run proceeds
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        env["PSEUDOLIFE_SHIM_ENGINE"] = "native"
        result = run_arm(repo, env, binary, ["--unattended", "--tag", "0.21.1", "--json", "--health-retries", "1",
                                             "--rollback-tag", "pre-tunnel"])
        out = result.stdout + result.stderr
        assert result.returncode == 0, out
        observed = stages(json.loads(state_path.read_text()))
        assert "tag" in observed and observed[-1] == "up", observed
        assert launcher.read_bytes() == before, "the client was not moved"
        assert not Path(env["PSEUDOLIFE_SHIM_RUNTIMES"]).exists(), "no shim runtime was installed"
        assert "updating the client side" not in out, out
        assert '"shim"' not in result.stdout and '"plugin"' not in result.stdout, result.stdout
        warning = "this machine stays on the Python client: a tunnel is registered to start by itself"
        assert warning in out, out
        assert "This unattended run updates the daemon alone" in out, out
        assert "clients not moved: a tunnel autostart service keeps this machine on the Python client" in out, out
        assert "Start a new session to run on the new shim runtime" not in out, out
        log = (Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "unattended-update.log").read_text()
        assert warning in log and f"systemd user unit {unit}" in log, log


def test_unattended_python_host_at_the_newest_release_holds(tmp_path):
    # Nothing newer to deploy is not "nothing to do" on a Python host: the
    # Python daemon is retired, so the scheduled run asks for the attended move.
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        state = json.loads(state_path.read_text())
        state.update(target_version="0.21.0", no_board_peer=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--unattended", "--tag", "0.21.0", "--json"])
        assert result.returncode == 4, result.stdout + result.stderr
        assert not stages(json.loads(state_path.read_text()))
        notice, = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
        assert "The Python daemon is retired" in notice, notice
        # The near miss: a Rust host at the newest release has nothing to do.
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--unattended", "--tag", "0.21.0", "--json"])
        assert result.returncode == 3, result.stdout + result.stderr


def test_an_inconclusive_rust_manifest_probe_leaves_the_move_to_the_pull(tmp_path):
    # `docker manifest inspect` that cannot ask (a registry proxy only the
    # engine has) is not "not published": the move goes on, and the pull
    # decides. A conclusive "manifest unknown" is refused (the unpublished row).
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state.update(rust_published=False, rust_manifest_denied=True)
        state_path.write_text(json.dumps(state))
        result = run_arm(repo, env, binary, ["--tag", "0.21.1", "--daemon-only", "--json", "--health-retries", "1",
                                             "--rollback-tag", "pre-probe"])
        assert result.returncode == 0, result.stdout + result.stderr
        pulled = [a[-1] for a in json.loads(state_path.read_text())["calls"] if a[:1] == ["pull"]]
        assert pulled == ["ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.1"], pulled
        assert _labels(state_path)["org.pseudolife.engine"] == "rust"


# --- The full hand-off (full-update-v1) --------------------------------------
# A release-mode update whose target is newer than the running updater runs
# the whole update with the target's own updater. The built binary cannot be
# both releases, so these rows serve a "newer release" whose client is a
# native stand-in (a compiled execv wrapper on POSIX, the distlib launcher on
# Windows) running STAND_IN, which records what it was handed and answers as
# the newer code; the rows that need the real binary as the child start it
# with the hand-off markers themselves.
NEWER = "9.0.0"
FULL_PROTOCOLS = ("client-handoff-v1", "full-update-v1")
STAND_IN_C = r'''#include <unistd.h>
int main(int argc, char **argv) {
    char *args[argc + 2];
    args[0] = PYTHON;
    args[1] = SCRIPT;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    args[argc + 1] = 0;
    execv(PYTHON, args);
    return 127;
}
'''
STAND_IN = r'''import json, os, pathlib, signal, sys, time
cfg = json.loads(pathlib.Path(__file__).with_suffix(".json").read_text())
args = sys.argv[1:]
if args == ["update", "--native-capability"]:
    print("pseudolife-mcp native-update-v1")
    sys.exit(0)
if args == ["update", "--client-handoff-protocols"]:
    print(cfg["version"])
    print(" ".join(cfg["protocols"]))
    sys.exit(0)
record = {"argv": args, "pid": os.getpid(),
          "handoff": os.environ.get("PSEUDOLIFE_UPDATE_HANDOFF"),
          "report_env": os.environ.get("PSEUDOLIFE_UPDATE_HANDOFF_REPORT"),
          "stdin_tty": bool(sys.stdin) and sys.stdin.isatty()}
data = pathlib.Path(os.environ["PSEUDOLIFE_MCP_DATA_DIR"])
pid_file = data / "update.lock.pid"
record["lock_pid"] = pid_file.read_text().strip() if pid_file.exists() else None
with open(data / "update.lock", "a+b") as lock:
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        record["lock_held"] = False
    except OSError:
        record["lock_held"] = True
out = pathlib.Path(cfg["record"])
deliveries = None
def save(**more):
    if deliveries is not None:
        # One byte per signal the kernel delivered, even when Python ran one
        # handler for two: how often this process was signalled.
        try:
            more["deliveries"] = len(os.read(deliveries, 64))
        except BlockingIOError:
            more["deliveries"] = 0
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**record, **more}) + "\n")
def stop(signum, frame):
    # The child owns its cleanup: it outlasts the 5 s a killing parent allows.
    time.sleep(cfg.get("cleanup", 0))
    save(signal=signum, cleaned=True)
    if cfg.get("die_by_signal"):
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    sys.exit(128 + signum)
if cfg.get("wait_for"):
    deliveries, wakeup = os.pipe()
    os.set_blocking(deliveries, False)
    os.set_blocking(wakeup, False)
    signal.set_wakeup_fd(wakeup, warn_on_full_buffer=False)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    pathlib.Path(cfg["wait_for"] + ".ready").write_text(str(os.getpid()))
    deadline = time.monotonic() + 60
    while not os.path.exists(cfg["wait_for"]) and time.monotonic() < deadline:
        time.sleep(0.02)
if cfg.get("prompt"):
    print(cfg["prompt"], flush=True)
    record["answer"] = sys.stdin.readline().strip()
save()
print(f"fixture {cfg['version']} updater: the newer code ran the daemon step", flush=True)
if cfg.get("report") is not None and record["report_env"]:
    pathlib.Path(record["report_env"]).write_text(json.dumps(cfg["report"]))
sys.exit(cfg.get("exit", 0))
'''


def _platform_target():
    import platform
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform.startswith("linux"):
        return "aarch64-unknown-linux-gnu" if arm else "x86_64-unknown-linux-gnu"
    if sys.platform == "darwin":
        return "aarch64-apple-darwin" if arm else "x86_64-apple-darwin"
    return "x86_64-pc-windows-msvc"


def _stand_in(directory):
    if os.name == "nt":
        return _program(directory / "stand-in", STAND_IN)
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not compiler:
        pytest.skip("a C compiler builds the native stand-in")
    source = directory / "stand-in.py"
    source.write_text(STAND_IN, encoding="utf-8")
    (directory / "stand-in.c").write_text(STAND_IN_C)
    binary = directory / "stand-in"
    subprocess.run([compiler, "-o", str(binary), f'-DPYTHON="{sys.executable}"', f'-DSCRIPT="{source}"',
                    str(directory / "stand-in.c")], check=True, capture_output=True, timeout=120)
    return binary


def _newer_release(env, state_path, version=NEWER, protocols=FULL_PROTOCOLS, code=0, report="default",
                   archive="ok", **behaviour):
    """Publish `version` with a stand-in client at PSEUDOLIFE_NATIVE_ARCHIVE_URL."""
    from rust.ci import release_assets
    home = Path(env["HOME"])
    work = home / f"release-{version}"
    work.mkdir()
    binary = _stand_in(work)
    if report == "default":
        report = {"mode": "release", "tier": "docker", "target": version, "ok": code == 0, "exit_code": code,
                  "steps": [f"fixture {version} updater: the newer code ran the daemon step"],
                  "from_the_newer_release": {"kept": True}}
    (work / "stand-in.json").write_text(json.dumps(
        {"version": version, "protocols": list(protocols), "exit": code, "report": report,
         "record": str(home / "child-runs.jsonl"), **behaviour}))
    if archive == "not-native":
        binary = work / "notes.txt"
        binary.write_text("not an executable\n")
    packed = release_assets.package(binary, version, _platform_target(), work / "assets")
    digest = hashlib.sha256(packed.read_bytes()).hexdigest()
    if archive == "tampered":
        digest = hashlib.sha256(b"another archive").hexdigest()
    sums = work / "assets/SHA256SUMS"
    sums.write_text(f"{digest}  {packed.name}\n")
    state = json.loads(state_path.read_text())
    state["native"] = {"SHA256SUMS": str(sums)}
    if archive != "missing":
        state["native"][packed.name] = str(packed)
    state_path.write_text(json.dumps(state))
    env["PSEUDOLIFE_NATIVE_ARCHIVE_URL"] = env["PSEUDOLIFE_MCP_DAEMON_URL"] + "/native"
    return work


def _child_runs(env):
    path = Path(env["HOME"]) / "child-runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _native_requests(env):
    path = Path(env["HOME"]) / "native-requests.log"
    return path.read_text().split() if path.exists() else []


def _client_untouched(env):
    """No runtime installed, no launcher written, no staging folder left."""
    runtimes = Path(env["PSEUDOLIFE_SHIM_RUNTIMES"])
    entries = sorted(p.name for p in runtimes.iterdir()) if runtimes.exists() else []
    assert entries in ([], [".staged"]), entries
    if (runtimes / ".staged").exists():
        assert not list((runtimes / ".staged").iterdir()), "a staging folder outlived the run"
    assert not Path(env["PSEUDOLIFE_SHIM_LAUNCHER"]).exists()


def _only_probes(state_path):
    """The parent's own Docker calls before a hand-off are read-only probes
    (tier, labels); anything else means this run's code went on."""
    calls = json.loads(state_path.read_text())["calls"]
    others = [c for c in calls if c[:2] != ["inspect", "-f"]]
    assert not others, others


def _version(binary):
    return subprocess.run([binary, "version"], capture_output=True, text=True, timeout=30).stdout.split()[1]


@contextmanager
def _held_update_lock(env):
    """The update lock as an updater that handed off holds it, with its pid."""
    data = Path(env["PSEUDOLIFE_MCP_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    with open(data / "update.lock", "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (data / "update.lock.pid").write_text(f"{os.getpid()}\n")
        try:
            yield str(os.getpid())
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


@pytest.mark.parametrize("shape", ["plain", "json", "daemon-only", "unattended", "result-file"])
def test_a_newer_release_runs_the_whole_update_with_its_own_updater(tmp_path, shape):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state["no_board_peer"] = True
        state_path.write_text(json.dumps(state))
        code = {"json": 5, "result-file": 3}.get(shape, 0)
        _newer_release(env, state_path, code=code)
        args = {"plain": [], "json": ["--json"], "daemon-only": ["--daemon-only"],
                "unattended": ["--unattended", "--json"],
                "result-file": ["--unattended", "--result-file", str(tmp_path / "result")]}[shape]
        if shape in ("unattended", "result-file"):
            env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        result = run_arm(repo, env, binary, ["--tag", NEWER, *args])
        assert result.returncode == code, result.stdout + result.stderr
        run, = _child_runs(env)
        # The user's arguments, the target pinned, never the result file.
        assert run["argv"] == ["update", "--tag", NEWER, *[a for a in args if a not in
                                                          ("--result-file", str(tmp_path / "result"))]], run
        # It borrows the lock this process holds (the pid it was given is the
        # holder's), so it is a hand-off child and never hands off again.
        assert run["lock_held"] and run["handoff"] == run["lock_pid"] and run["handoff"].isdigit(), run
        # This run made no daemon decision of its own, before or after the child.
        _only_probes(state_path)
        _client_untouched(env)
        if "--json" in args:
            report = json.loads(result.stdout)   # one document: the child's, this run's steps first
            assert report["steps"][0].startswith(f"release {NEWER} is newer than this updater"), report["steps"]
            assert f"fixture {NEWER} updater: the newer code ran the daemon step" in report["steps"]
            assert report["from_the_newer_release"] == {"kept": True}
            assert report["exit_code"] == code
        elif shape == "result-file":
            assert (tmp_path / "result").read_text() == f"{code}\n"
        else:
            assert f"==> release {NEWER} is newer than this updater" in result.stdout
            assert f"fixture {NEWER} updater: the newer code ran the daemon step" in result.stdout
        if shape in ("unattended", "result-file"):
            log = (Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "unattended-update.log").read_text()
            assert f"release {NEWER} is newer than this updater" in log, log


@pytest.mark.parametrize("case", ["same-version", "older", "no-protocol"])
def test_no_full_hand_off_to_the_same_version_an_older_one_or_a_client_without_the_protocol(tmp_path, case):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        version = {"same-version": _version(binary), "older": "0.21.1", "no-protocol": NEWER}[case]
        _newer_release(env, state_path, version=version,
                       protocols=("client-handoff-v1",) if case == "no-protocol" else FULL_PROTOCOLS)
        result = run_arm(repo, env, binary, ["--tag", version, "--daemon-only", "--json", "--health-retries", "1"])
        report = json.loads(result.stdout)
        assert _child_runs(env) == [], result.stdout
        assert not any("is newer than this updater" in s for s in report["steps"]), report["steps"]
        if case == "no-protocol":
            assert report["steps"][0] == (f"release {NEWER}'s client predates running the whole update itself; this "
                                          f"update continues with {_version(binary)}'s code"), report["steps"]
            # This run's own release deploy took the target (only it sets it).
            assert report["target"] == NEWER, report
        else:
            # Nothing is fetched for a target that is not newer.
            assert _native_requests(env) == [], result.stdout
        _client_untouched(env)


@pytest.mark.parametrize("archive", ["tampered", "missing", "not-native"])
def test_a_client_that_cannot_be_fetched_or_checked_leaves_the_update_to_this_code(tmp_path, archive):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        _newer_release(env, state_path, archive=archive)
        result = run_arm(repo, env, binary, ["--tag", NEWER, "--daemon-only", "--json", "--health-retries", "1"])
        report = json.loads(result.stdout)
        notes = [s for s in report["steps"] if f"release {NEWER}'s client" in s]
        assert len(notes) == 1 and notes[0] == report["steps"][0], report["steps"]
        assert notes[0].startswith(f"could not fetch release {NEWER}'s client"), notes
        assert notes[0].endswith(f"this update continues with {_version(binary)}'s code"), notes
        assert "\n" not in notes[0]
        assert {"tampered": "checksum mismatch", "missing": "returned 404",
                "not-native": "no native executable"}[archive] in notes[0], notes
        assert _child_runs(env) == []
        # The update went on, on this code: its release deploy took the target.
        assert report["target"] == NEWER, report
        _client_untouched(env)


@pytest.mark.parametrize("report", [True, False], ids=["its-report", "no-report"])
def test_a_failing_child_is_the_result_and_nothing_runs_after_it(tmp_path, report):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        _newer_release(env, state_path, code=1, report="default" if report else None)
        result = run_arm(repo, env, binary, ["--tag", NEWER, "--daemon-only", "--json"])
        assert result.returncode == 1, result.stdout + result.stderr
        assert len(_child_runs(env)) == 1
        document = json.loads(result.stdout)
        assert document["ok"] is False and document["exit_code"] == 1
        if not report:
            assert any(f"release {NEWER}'s updater ended with exit 1 without writing its report" in s
                       for s in document["steps"]), document["steps"]
        # This run's own code never followed the child's failure.
        _only_probes(state_path)
        _client_untouched(env)


def test_a_run_handed_the_update_never_hands_off_again(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        _newer_release(env, state_path)
        # The marker alone, with the lock free: only it can stop a hand-off
        # here (the lock borrow is the unattended row's).
        env["PSEUDOLIFE_UPDATE_HANDOFF"] = str(os.getpid())
        result = run_arm(repo, env, binary, ["--tag", NEWER, "--daemon-only", "--json", "--health-retries", "1"])
        report = json.loads(result.stdout)
        assert _native_requests(env) == [], result.stdout
        assert _child_runs(env) == []
        assert not any("is newer than this updater" in s for s in report["steps"]), report["steps"]
        # It ran on its own code: its release deploy took the target.
        assert report["target"] == NEWER, report


@pytest.mark.parametrize("marked", [True, False], ids=["handed", "near-miss-unmarked"])
def test_an_unattended_child_borrows_the_lock_and_still_holds_an_engine_change(tmp_path, marked):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        state["no_board_peer"] = True
        state_path.write_text(json.dumps(state))
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        with _held_update_lock(env) as holder:
            if marked:
                env["PSEUDOLIFE_UPDATE_HANDOFF"] = holder
            result = run_arm(repo, env, binary, ["--unattended", "--tag", "0.21.1", "--json", "--health-retries", "1"])
        assert result.returncode == 4, result.stdout + result.stderr
        busy = "another pseudolife-mcp update is running" in result.stdout
        assert busy != marked, result.stdout
        state = json.loads(state_path.read_text())
        assert not stages(state), stages(state)
        if marked:
            # Past the lock, the child's own engine rule holds the Python host.
            notice, = [json.loads(line) for line in state_path.with_name("notices.jsonl").read_text().splitlines()]
            assert "The Python daemon is retired" in notice, notice


def test_clients_only_keeps_the_client_hand_off(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        _newer_release(env, state_path)
        result = run_arm(repo, env, binary, ["--tag", NEWER, "--clients-only", "--json"])
        assert "is newer than this updater" not in result.stdout
        assert not [run for run in _child_runs(env) if "--clients-only" not in run["argv"]], _child_runs(env)
        assert stages(json.loads(state_path.read_text())) == []


@pytest.mark.parametrize("attended", [False, True], ids=["unattended", "attended"])
def test_a_daemon_already_at_the_target_hands_off_only_an_attended_update(tmp_path, attended):
    # The daemon runs the target and this client is older: an unattended run
    # has no daemon step to hand over (and a daemon-only host would fetch the
    # client every night for nothing), so it reports nothing to do on this
    # code; an attended run still gets the target's own logic.
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state.update(running_version=NEWER, no_board_peer=True)
        state_path.write_text(json.dumps(state))
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        _newer_release(env, state_path)
        args = ["--tag", NEWER, "--json"] if attended else ["--unattended", "--tag", NEWER, "--json"]
        result = run_arm(repo, env, binary, args)
        if attended:
            assert result.returncode == 0, result.stdout + result.stderr
            run, = _child_runs(env)
            assert run["argv"] == ["update", *args], run
        else:
            assert result.returncode == 3, result.stdout + result.stderr
            assert _child_runs(env) == [] and _native_requests(env) == []
            assert "nothing to do" in result.stdout
        assert stages(json.loads(state_path.read_text())) == []


def _terminal_session(argv, env, cwd):
    """Start argv as the foreground process group of a new terminal session,
    the way a shell starts it: a background group that reads the terminal is
    stopped (SIGTTIN)."""
    import pty
    master, slave = pty.openpty()
    bootstrap = ("import fcntl, os, signal, sys, termios; os.setsid(); "
                 "fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
                 "[signal.signal(s, signal.SIG_DFL) for s in "
                 "(signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGTSTP)]; "
                 "os.execv(sys.argv[1], sys.argv[1:])")
    proc = subprocess.Popen([sys.executable, "-c", bootstrap, *argv], env=env, cwd=cwd,
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    return proc, master


def _read_until(master, text, timeout=30):
    import select
    seen = b""
    deadline = time.monotonic() + timeout
    while text.encode() not in seen and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                seen += os.read(master, 4096)
            except OSError:
                break
    assert text.encode() in seen, seen.decode(errors="replace")
    return seen


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX terminal job control")
def test_the_target_updater_can_ask_at_the_terminal(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        _newer_release(env, state_path, prompt="Set them up now? [y/N]")
        proc, master = _terminal_session([binary, "update", "--tag", NEWER, "--daemon-only"], env, repo)
        try:
            _read_until(master, "Set them up now? [y/N]")
            os.write(master, b"y\n")
            assert proc.wait(timeout=30) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=15)
            os.close(master)
        run, = _child_runs(env)
        assert run["stdin_tty"] and run["answer"] == "y", run


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX signals")
@pytest.mark.parametrize("signal_name", ["ctrl-c", "SIGINT", "SIGTERM"])
def test_an_interrupted_update_waits_for_the_target_updaters_own_cleanup(tmp_path, signal_name):
    # Ctrl-C at the terminal reaches the whole foreground group, so the child
    # has it already and gets it once; a signal a process sends to this pid
    # alone is forwarded. Either way this process waits for the child's
    # cleanup (longer than the 5 s a killing parent allows) and exits with
    # the child's code.
    import signal
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        gate = tmp_path / "release-child"
        # On Ctrl-C the child dies by the signal itself (this process reports
        # 128 + it); on SIGTERM it exits with that code.
        _newer_release(env, state_path, wait_for=str(gate), cleanup=7, die_by_signal=signal_name == "ctrl-c")
        proc, master = _terminal_session([binary, "update", "--tag", NEWER, "--daemon-only"], env, repo)
        number = signal.SIGINT if signal_name == "ctrl-c" else getattr(signal, signal_name)
        try:
            _await_file(Path(str(gate) + ".ready"), proc)
            if signal_name == "ctrl-c":
                os.write(master, b"\x03")    # the terminal's line discipline
            else:
                os.kill(proc.pid, number)     # this process alone
            assert proc.wait(timeout=40) == 128 + number
        finally:
            gate.touch()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=15)
            os.close(master)
        run, = _child_runs(env)
        # It was signalled once: the terminal's Ctrl-C is not passed on again.
        assert run["signal"] == number and run["cleaned"] and run["deliveries"] == 1, run
        _client_untouched(env)


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX open-file locks")
def test_the_update_lock_outlives_a_killed_parent_while_the_target_updater_runs(tmp_path):
    # Windows kills the child with its parent (the job object); on Unix the
    # child keeps the lock's open file, so no second update can start.
    import fcntl
    import signal
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        gate = tmp_path / "release-child"
        _newer_release(env, state_path, wait_for=str(gate))
        proc = subprocess.Popen([binary, "update", "--tag", NEWER, "--daemon-only", "--json"], env=env, cwd=repo,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        child = None
        try:
            _await_file(Path(str(gate) + ".ready"), proc)
            child = int(Path(str(gate) + ".ready").read_text())
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=15)
            with open(Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update.lock", "a+b") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = run_arm(repo, env, binary, ["--tag", "0.21.1", "--daemon-only", "--json"])
            assert second.returncode == 2, second.stdout + second.stderr
            assert "another pseudolife-mcp update is running" in second.stdout, second.stdout
        finally:
            gate.touch()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=15)
            if child:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and Path(f"/proc/{child}").exists():
                    time.sleep(0.05)


def test_an_unattended_run_with_the_daemon_knob_off_fetches_nothing(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state.update(unattended_daemon_off=True, no_board_peer=True)
        state_path.write_text(json.dumps(state))
        env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-bearer"
        _newer_release(env, state_path)
        result = run_arm(repo, env, binary, ["--unattended", "--tag", NEWER, "--json"])
        assert result.returncode == 4, result.stdout + result.stderr
        assert "updates.unattended_daemon is off" in result.stdout, result.stdout
        assert _native_requests(env) == [] and _child_runs(env) == []


def test_a_pending_legacy_import_gets_no_full_hand_off(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        _rust_host(state, env)
        state_path.write_text(json.dumps(state))
        _newer_release(env, state_path)
        # The import pointer its own code reads (an unreadable one counts).
        pointer = Path(env["PSEUDOLIFE_MCP_DATA_DIR"]) / "update-engine/pending-import"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text("not a recovery record\n")
        result = run_arm(repo, env, binary, ["--tag", NEWER, "--daemon-only", "--json"])
        assert _native_requests(env) == [] and _child_runs(env) == [], result.stdout
        assert "is newer than this updater" not in result.stdout


def test_a_host_on_a_checkout_engine_gets_no_full_hand_off(tmp_path):
    binary = _native_binary()
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY")
    with host(tmp_path) as (repo, env, state_path):
        state = json.loads(state_path.read_text())
        labels = state["running"]["Config"]["Labels"]
        labels.update({"org.pseudolife.engine": "rust", "org.pseudolife.checkout": str(repo),
                       "org.pseudolife.activation": "accepted-fixture"})
        state_path.write_text(json.dumps(state))
        accepted = repo / "data/update-engine/docker-compose.engine.json"
        accepted.parent.mkdir(parents=True)
        accepted.write_text(json.dumps({"services": {SERVICE: {"labels": labels}}}))
        _newer_release(env, state_path)
        result = run_arm(repo, env, binary, ["--tag", NEWER, "--json"])
        assert result.returncode == 2, result.stdout + result.stderr
        assert "this host selected a checkout engine" in result.stdout, result.stdout
        assert _native_requests(env) == [] and _child_runs(env) == []
