"""Rehearse activating the Rust slice and rolling it back, on disposable state.

Two legs, each recorded step by step in one evidence JSON:

``launcher``: the activation mechanism the installers use for clients. A
disposable runtimes root gets runtime 000001 with the real Python shim
(this checkout installed without dependencies into a venv over the system
site-packages), the real launcher (``runtimes.ensure_launcher``) and a real
Claude Desktop registration (``ops/register_claude_desktop.py --config`` on a
disposable config). The registration's command is resolved and run; then a
Rust shim runtime 000002 is installed (marker written last), an incomplete
000003 beside it, and the registered command must reach 000002; a rollback
install of the older code as 000004 must be selected over the newer one;
removing 000004 must fall back to 000002. Nothing ships a Rust shim to users
today: this proves the mechanism, not a shipped path.

``bank``: one disposable bank, one disposable home, the same 1,024-dimension
ONNX fixture for both daemons (run.py's CI fixture). The Python daemon writes;
the Rust daemon reads those rows, writes its own and restarts; a call goes
through the registered client path (launcher -> Rust shim -> Rust daemon);
the Python daemon starts again and reads what Rust wrote. ``meta.
schema_version`` is read after every start. Then a newer stamp is written by
hand: the Rust daemon must refuse it, and the Python daemon is expected to
leave it alone (today it stamps it down: a recorded, failing expectation).

    python rust/daemon/harness/rehearse_activation.py \
        --rust-shim <pseudolife-stdio[.exe]> --rust-daemon <pseudolife-daemon-static[.exe]> \
        --out <evidence.json> [--leg launcher|bank|all]

Run it with a Python that has the daemon's dependencies plus the fixture's
(``onnx==1.22.0 onnxruntime==1.27.0 optimum==2.1.0 optimum-onnx==0.1.0``),
the bench PostgreSQL up and its test login present. Every database it
touches is one this process created (``pgdisposable``), and a daemon is
started only on that database: the live bank is never reachable from here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import w2e as h

REPO = h.w1a.REPO
sys.path.insert(0, str(REPO))
from pseudolife_memory import runtimes as rt  # noqa: E402
from pseudolife_memory.storage.schema import SCHEMA_META_VERSION  # noqa: E402

WINDOWS = os.name == "nt"
STOP = signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGTERM
# The Python daemon under a returning SIGTERM/SIGBREAK handler, so its
# registered exit save runs (w2e_shutdown.py, background_sessions.py).
PYTHON_SERVE = """
import signal
for sig in (signal.SIGTERM, getattr(signal, 'SIGBREAK', signal.SIGTERM)):
    signal.signal(sig, lambda *_: None)
from pseudolife_memory.daemon import run_daemon
run_daemon()
"""
CONFIG = "memory:\n  dream:\n    enabled: false\n  retrieval_log:\n    enabled: false\n"


class Failed(Exception):
    """A rehearsal step did not hold."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def disposable_dsn(dsn: str, owned: list[str]) -> str:
    """The DSN, only if it names a database this process created."""
    from psycopg.conninfo import conninfo_to_dict
    name = conninfo_to_dict(dsn).get("dbname") or ""
    if name not in owned or not name.startswith("pl_cf_"):
        raise Failed(f"refusing to start a daemon on {name!r}: not a disposable bank "
                     "this rehearsal created")
    return dsn


SCRATCH: Path | None = None   # set by main(); evidence text is scrubbed against it


def _spellings(path: Path) -> list[str]:
    """A path as Windows, forward-slash, Git Bash and JSON-escaped text spell it."""
    text = str(path)
    out = [text, text.replace("\\", "/"), text.replace("\\", "\\\\")]
    if len(text) > 2 and text[1] == ":":
        out.append("/" + text[0].lower() + text[2:].replace("\\", "/"))
    return out


def scrub(value, *, scratch: Path):
    """The evidence with no machine path in it, for a public repository:
    the scratch root, the checkout and the home directory become
    ``<scratch>``, ``<repo>`` and ``<home>``, and the account name ``<user>``."""
    pairs = [(s, "<scratch>") for s in _spellings(scratch)]
    pairs += [(s, "<repo>") for s in _spellings(REPO)]
    pairs += [(s, "<home>") for s in _spellings(Path.home())]
    pairs.sort(key=lambda p: -len(p[0]))
    user = re.compile(re.escape(Path.home().name), re.IGNORECASE)

    def text(s: str) -> str:
        for old, new in pairs:
            s = re.sub(re.escape(old), new.replace("\\", "\\\\"), s, flags=re.IGNORECASE)
        return user.sub("<user>", s) if len(Path.home().name) >= 3 else s
    if isinstance(value, dict):
        return {k: scrub(v, scratch=scratch) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, scratch=scratch) for v in value]
    if isinstance(value, Path):
        return text(str(value))
    return text(value) if isinstance(value, str) else value


def tail(text: str, n: int) -> str:
    """The last `n` characters of `text`, scrubbed first, so a cut never
    leaves part of a machine path the scrub can no longer recognize."""
    return scrub(text or "", scratch=SCRATCH or Path.home() / ".nonexistent")[-n:]


def selected_runtime(version_output: str) -> str | None:
    """The runtime directory a shim's ``version`` reports it ran from: its
    second line, ``runtime <path> (...)``; None when it ran outside one."""
    for line in version_output.splitlines():
        if line.startswith("runtime "):
            rest = line[len("runtime "):]
            return rest.rsplit(" (", 1)[0].strip()
    return None


class Log:
    def __init__(self) -> None:
        self.steps: list[dict] = []

    def step(self, name: str, ok: bool, *, fatal: bool = True, **observed) -> None:
        """Record a step. A failed fatal step stops the rehearsal; a failed
        non-fatal one (a known blocker on a path the later steps do not
        need) is recorded, fails the result, and lets the rest run."""
        self.steps.append({"step": name, "ok": bool(ok), **observed})
        print(f"[{'ok' if ok else 'FAIL'}] {name}", flush=True)
        if not ok and fatal:
            raise Failed(f"{name}: " + scrub(json.dumps(observed, default=str),
                                             scratch=SCRATCH or Path.home())[:600])


# ---------------------------------------------------------------- launcher

def _marker(runtime: Path, source: str, commit: str) -> None:
    marker = {"version": "rehearsal", "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "source": source, "source_commit": commit,
              "base_interpreter": getattr(sys, "_base_executable", sys.executable)}
    (runtime / "runtime.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")


def _python_runtime(path: Path, log: Log, commit: str) -> None:
    """A real Python shim runtime: a venv over the system site-packages with
    this checkout installed without dependencies, marker written last."""
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(path)], check=True)
    py = path / ("Scripts/python.exe" if WINDOWS else "bin/python")
    proc = subprocess.run([str(py), "-m", "pip", "install", "-q", "--no-deps", str(REPO)],
                          capture_output=True, text=True)
    log.step("python runtime installed", proc.returncode == 0 and rt._console(path, WINDOWS).is_file(),
             runtime=str(path), pip_tail=tail(proc.stderr, 400))
    _marker(path, "rehearsal: this checkout's Python shim", commit)


def _rust_runtime(path: Path, shim: Path, commit: str, *, complete: bool = True) -> None:
    console = rt._console(path, WINDOWS)
    console.parent.mkdir(parents=True)
    shutil.copy2(shim, console)
    if complete:
        _marker(path, "rehearsal: Rust shim candidate", commit)


def _stop_tree(proc: subprocess.Popen) -> None:
    """Stop our own child and its children, by its PID (the launcher runs the
    selected console as a child)."""
    if proc.poll() is not None:
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()
    proc.wait(timeout=10)


def _run_registered(entry: dict, args: list[str], env: dict) -> subprocess.CompletedProcess:
    proc = subprocess.Popen([entry["command"], *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env={**env, **(entry.get("env") or {})})
    try:
        out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        _stop_tree(proc)
        out, err = proc.communicate()
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def launcher_leg(root: Path, shim: Path, shim_commit: str, log: Log, env: dict,
                 daemon_url: str, token_file: Path) -> dict:
    runtimes = root / "runtimes"
    launcher = root / "bin" / ("pseudolife-mcp.exe" if WINDOWS else "pseudolife-mcp")
    layout = rt.Layout(runtimes, launcher)
    env = {**env, "PSEUDOLIFE_SHIM_RUNTIMES": str(runtimes), "PSEUDOLIFE_SHIM_LAUNCHER": str(launcher)}
    older = runtimes / "000001"
    _python_runtime(older, log, shim_commit)
    result = rt.ensure_launcher(layout)
    log.step("launcher written by runtimes.ensure_launcher", launcher.is_file(), result=result,
             launcher=str(launcher))

    config = root / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"someone-else": {"command": "x"}}}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(REPO / "ops" / "register_claude_desktop.py"),
                           "--command", str(launcher), "--config", str(config),
                           "--daemon-url", daemon_url, "--token-file", str(token_file),
                           # The installer's way: the script writes the owner-only file.
                           "--token-from-env", "PL_REHEARSAL_TOKEN"],
                          capture_output=True, text=True, timeout=180,
                          env={**env, "PL_REHEARSAL_TOKEN": h.TOKEN})
    try:
        servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    except (OSError, ValueError, KeyError):
        servers = {}
    entry = servers.get("pseudolife-desktop") or {}
    foreign = servers.get("someone-else")
    log.step("client registered with the launcher as its command",
             proc.returncode == 0 and Path(entry.get("command", "")) == launcher and foreign is not None,
             exit=proc.returncode, entry=entry, out=tail(proc.stdout + proc.stderr, 400))

    def resolve(label: str, want: Path) -> dict:
        out = _run_registered(entry, ["version"], env)
        got = selected_runtime(out.stdout)
        console = rt._console(Path(got), WINDOWS) if got else None
        observed = {"want": str(want), "got": got, "exit": out.returncode,
                    "stdout": tail(out.stdout.strip(), 300),
                    # The console of the runtime that answered, not of the one wanted.
                    "answered_console_sha256": sha256(console) if console and console.is_file()
                    else None}
        log.step(label, out.returncode == 0 and got is not None
                 and Path(got).resolve() == want.resolve(), **observed)
        return observed

    first = resolve("registered command runs the only runtime (000001, Python)", older)
    newer = runtimes / "000002"
    _rust_runtime(newer, shim, shim_commit)
    _rust_runtime(runtimes / "000003", shim, shim_commit, complete=False)
    activated = resolve("after installing the Rust shim as 000002 (and an incomplete 000003), "
                        "the registered command runs 000002", newer)
    # A rollback is a fresh install of the older release, as `update` does,
    # never a copy: a copied venv's console still runs its source's interpreter.
    rollback = runtimes / "000004"
    _python_runtime(rollback, log, shim_commit)
    rolled_back = resolve("a rollback install of the older code as 000004 is selected over the newer "
                          "Rust runtime", rollback)
    shutil.rmtree(rollback)
    reverted = resolve("removing 000004 falls back to the newest complete runtime, 000002", newer)
    return {"launcher": str(launcher), "entry": entry, "first": first, "activated": activated,
            "rolled_back": rolled_back, "reverted": reverted, "layout": layout, "env": env}


def stdio_call(entry: dict, env: dict, tool: str, arguments: dict, timeout: float = 120) -> dict:
    """One tool call through the registered command, over MCP stdio, the way
    a client makes it: initialize and wait for the answer, then the call and
    wait for its answer, and only then close stdin (the shim treats EOF as
    the session ending and cancels what is in flight)."""
    import queue
    import threading
    proc = subprocess.Popen([entry["command"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env={**env, **(entry.get("env") or {})})
    lines: queue.Queue = queue.Queue()
    err: list[str] = []
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()
    threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def answer(request_id: int) -> dict | None:
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                return message
        return None

    result = {"isError": True, "value": None}
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                         "clientInfo": {"name": "rehearsal", "version": "1"}}})
        init = answer(1)
        result["initialize"] = "answered" if init and "result" in init else (init or "no answer")
        if init and "result" in init:
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}})
            reply = answer(2) or {}
            body = reply.get("result") or {}
            value = body.get("structuredContent")
            if value is None and body.get("content"):
                try:
                    value = json.loads(body["content"][0]["text"])
                except (ValueError, KeyError, IndexError, TypeError):
                    value = body["content"][0].get("text")
            result.update(isError=bool(not reply or body.get("isError") or "error" in reply),
                          value=value, error=reply.get("error"))
    except (BrokenPipeError, OSError) as exc:
        result["transport_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _stop_tree(proc)
    result["exit"] = proc.returncode
    result["stderr"] = tail("".join(err), 400)
    return result


# -------------------------------------------------------------------- bank

def _meta(dsn: str) -> dict:
    import psycopg
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT key, value FROM meta WHERE key IN "
                            "('schema_version', 'writer_lease_epoch')").fetchall()
    return {k: v for k, v in rows}


def _fixture_env(home: Path) -> dict:
    import onnxruntime
    import yaml
    from embedding_fixture import create
    model = home / "model"
    create(model, dimension=1024)
    path = home / "data" / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config["embedding"] = {"model_name": str(model), "backend": "onnx", "device": "cpu",
                           "cpu_dtype": "fp32", "query_prefix": "", "max_seq_length": 32}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    capi = Path(onnxruntime.__file__).parent / "capi"
    library = capi / "onnxruntime.dll" if WINDOWS else next(
        iter(sorted(capi.glob("libonnxruntime.so.*")) + sorted(capi.glob("libonnxruntime*.dylib"))))
    return {"ORT_DYLIB_PATH": str(library), "PSEUDOLIFE_DAEMON_ONNX_DIR": str(model),
            "PSEUDOLIFE_DAEMON_ORT_THREADS": "1", "OMP_NUM_THREADS": "1",
            "TOKENIZERS_PARALLELISM": "false"}


class Bank:
    """One disposable bank and home that both daemons open in turn, on one port."""

    def __init__(self, root: Path, rust_daemon: Path, owned: list[str], port: int) -> None:
        name = h.pg.name("pl_cf_w2e_rehearsal")
        owned.append(name)
        self.dsn = disposable_dsn(h.pg.create(name), owned)
        self.owned = owned
        self.home = h.daemons.make_home(root, "home", CONFIG)
        fixture = _fixture_env(self.home)
        side = h.side_env({"env": fixture}, self.dsn)
        self.env = h.daemons.base_env(self.home, side)
        self.rust_env = h.daemons.base_env(self.home, h.w1a.rust_env(side))
        self.rust_daemon = rust_daemon
        self.port = port
        self.url = f"http://127.0.0.1:{self.port}"
        self.running = None

    def start(self, kind: str, *, refused: bool = False):
        disposable_dsn(self.dsn, self.owned)
        if kind == "python":
            d = h.daemons.python_daemon(self.home, self.port, self.env)
            d.argv = [sys.executable, "-c", PYTHON_SERVE]
        else:
            d = h.daemons.rust_daemon(self.rust_daemon, self.home, self.port, self.rust_env)
        d.log = self.home / f"daemon-{kind}-{len(list(self.home.glob('daemon-*.log')))}.log"
        self.running = d
        if refused:
            # A refusing daemon keeps answering /health without becoming ready.
            d.start(120, stages=())
        else:
            d.start(300)
        # The port was chosen before the launcher leg; only our own process,
        # announcing it, may answer on it.
        listening = f"listening on 127.0.0.1:{self.port}"
        if d.proc.poll() is not None or listening not in d.log.read_text(errors="replace"):
            raise Failed(f"the {kind} daemon this run started is not the one on port "
                         f"{self.port}; another process may hold it")
        if not refused:
            h.w1a.wait_settled([self.port], token=h.TOKEN)
        return d

    def stop(self) -> int | None:
        d, self.running = self.running, None
        if d is None or d.proc is None:
            return None
        code = d.proc.poll()
        if code is None:
            d.proc.send_signal(STOP)
            try:
                code = d.proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                code = None
        d.stop()
        return code

    def mcp(self):
        m = h.Mcp(self.port)
        m.init()
        return m


def _value(r: dict):
    return r.get("value") if isinstance(r, dict) else None


def _fact(m, entity: str, attribute: str):
    v = _value(m.call("memory_fact_get", {"entity": entity, "attribute": attribute}))
    record = (v or {}).get("record") if isinstance(v, dict) else None
    return record.get("value") if isinstance(record, dict) else None


def _found(m, query: str, text: str) -> bool:
    v = _value(m.call("memory_search", {"query": query, "top_k": 8}))
    entries = (v or {}).get("entries", []) if isinstance(v, dict) else []
    return any(text in (e.get("text") or "") for e in entries)


def bank_leg(root: Path, rust_daemon: Path, log: Log, owned: list[str], client, port: int) -> dict:
    bank = Bank(root, rust_daemon, owned, port)
    seen = {"metas": []}
    py_text = "memory cursor written by the Python daemon before activation"
    rust_text = "memory cache written by the Rust daemon after activation"
    try:
        bank.start("python")
        seen["metas"].append(("python start 1", _meta(bank.dsn)))
        m = bank.mcp()
        stored = m.call("memory_store", {"text": py_text, "source": "rehearsal"})
        fset = m.call("memory_fact_set", {"entity": "rehearsal", "attribute": "written by",
                                          "value": "python"})
        log.step("Python daemon writes an entry and a fact", not stored["isError"] and not fset["isError"],
                 store=_value(stored), fact_set=_value(fset))
        code = bank.stop()
        log.step("Python stop is clean", code == 0, exit=code)

        bank.start("rust")
        seen["metas"].append(("rust start 1", _meta(bank.dsn)))
        m = bank.mcp()
        log.step("Rust daemon reads Python's fact", _fact(m, "rehearsal", "written by") == "python")
        log.step("Rust daemon finds Python's entry", _found(m, "memory cursor", py_text))
        stored = m.call("memory_store", {"text": rust_text, "source": "rehearsal"})
        fset = m.call("memory_fact_set", {"entity": "rehearsal", "attribute": "written by",
                                          "value": "rust"})
        fnew = m.call("memory_fact_set", {"entity": "rehearsal rust", "attribute": "slot",
                                          "value": "rust only"})
        log.step("Rust daemon writes an entry, supersedes the fact and sets a new one",
                 not any(r["isError"] for r in (stored, fset, fnew)),
                 store=_value(stored), fact_set=_value(fset), new=_value(fnew))
        code = bank.stop()
        log.step("Rust stop is clean", code == 0, exit=code)

        bank.start("rust")
        seen["metas"].append(("rust start 2", _meta(bank.dsn)))
        m = bank.mcp()
        log.step("after a restart the Rust daemon still reads its own writes",
                 _fact(m, "rehearsal", "written by") == "rust"
                 and _fact(m, "rehearsal rust", "slot") == "rust only"
                 and _found(m, "memory cache", rust_text))
        if client is None:
            log.steps.append({"step": "client path (launcher -> Rust shim -> Rust daemon)",
                              "ok": True, "skipped": "--leg bank registers no client"})
        else:
            via = stdio_call(client["entry"], client["env"], "memory_fact_get",
                             {"entity": "rehearsal rust", "attribute": "slot"})
            record = (via.get("value") or {}).get("record") if isinstance(via.get("value"), dict) else None
            log.step("a call through the registered client path (launcher -> Rust shim -> Rust "
                     "daemon) reads the Rust-written fact",
                     not via["isError"] and isinstance(record, dict) and record.get("value") == "rust only",
                     fatal=False, result=via,
                     daemon_version=h.w1a.call(bank.port, "GET", "/health").get("json", {}).get("version"))
        code = bank.stop()
        log.step("Rust stop is clean (2)", code == 0, exit=code)

        bank.start("python")
        seen["metas"].append(("python start 2 (rollback)", _meta(bank.dsn)))
        m = bank.mcp()
        log.step("after rollback the Python daemon reads the fact Rust superseded",
                 _fact(m, "rehearsal", "written by") == "rust")
        log.step("after rollback the Python daemon reads the fact Rust created",
                 _fact(m, "rehearsal rust", "slot") == "rust only")
        log.step("after rollback the Python daemon finds the entry Rust stored",
                 _found(m, "memory cache", rust_text))
        log.step("after rollback the Python daemon still finds its own entry",
                 _found(m, "memory cursor", py_text))
        code = bank.stop()
        log.step("Python stop is clean (2)", code == 0, exit=code)

        # A bank stamped by a newer build: what each side does with it.
        import psycopg
        newer = SCHEMA_META_VERSION + 1
        # The way ensure_schema writes it: a JSON number.
        with psycopg.connect(bank.dsn, autocommit=True) as conn:
            conn.execute("UPDATE meta SET value = %s::jsonb WHERE key = 'schema_version'",
                         (str(newer),))
        rust_refusal = bank.start("rust", refused=True)
        deadline, log_text, health = time.monotonic() + 60, "", {}
        while time.monotonic() < deadline:
            log_text = rust_refusal.log.read_text(errors="replace")
            health = h.w1a.call(bank.port, "GET", "/health").get("json") or {}
            if "is newer than this build" in log_text or rust_refusal.proc.poll() is not None:
                break
            time.sleep(1)
        exited = rust_refusal.proc.poll()
        bank.stop()
        after_rust = _meta(bank.dsn).get("schema_version")
        log.step("the Rust daemon refuses a bank stamped by a newer build",
                 "is newer than this build" in log_text and health.get("ready") is not True
                 and str(after_rust) == str(newer),
                 exited=exited, health={k: health.get(k) for k in ("status", "ready", "pending", "db")},
                 stamp_after=after_rust, log_tail=globals()["tail"](log_text, 500))
        bank.start("python")
        stamped = _meta(bank.dsn).get("schema_version")
        seen["newer_bank"] = {"stamped_before": str(newer), "after_rust_refusal": after_rust,
                              "after_python_start": stamped}
        # Expected, not met today: Python's ensure_schema upserts its own
        # version and refuses no newer bank, so a rollback stamps it down.
        log.step("the Python daemon leaves a newer build's stamp alone", str(stamped) == str(newer),
                 fatal=False, stamped_before=str(newer), after_python_start=stamped)
        bank.stop()
        return seen
    finally:
        bank.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rust-shim", type=Path, required=True)
    parser.add_argument("--rust-daemon", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--leg", choices=("launcher", "bank", "all"), default="all")
    args = parser.parse_args(argv)
    for key in ("PSEUDOLIFE_MCP_DATABASE_URL",):
        if os.environ.get(key):
            print(f"rehearsal: unset {key}; this run creates its own disposable bank",
                  file=sys.stderr)
            return 2
    global SCRATCH
    root = h.run_root("rehearse-activation")
    SCRATCH = root
    log = Log()
    owned: list[str] = []
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    evidence = {"started": dt.datetime.now(dt.timezone.utc).isoformat(), "checkout": commit,
                "platform": f"{platform.system()} {platform.release()} ({platform.version()})",
                "driver_sha256": sha256(Path(__file__).resolve()),
                "tree_changes": subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                                               capture_output=True, text=True).stdout.splitlines(),
                "python": sys.version.split()[0], "schema_meta_version": SCHEMA_META_VERSION,
                "rust_shim_sha256": sha256(args.rust_shim),
                "rust_daemon_sha256": sha256(args.rust_daemon), "leg": args.leg}
    ok = False
    try:
        client = None
        token_file = root / "credentials" / "token"
        if args.leg in ("launcher", "all"):
            # The daemon URL is known before the bank leg starts its daemon.
            port_hint = h.daemons.free_port()
            home = root / "client-home"
            env = h.daemons.base_env(home, {
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
                "APPDATA": str(home / "AppData" / "Roaming"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "PSEUDOLIFE_RELEASE_CHECK": "0"})  # unattended client updates are off by default
            (root / "client-home").mkdir(exist_ok=True)
            leg = launcher_leg(root, args.rust_shim.resolve(), commit, log, env,
                               f"http://127.0.0.1:{port_hint}", token_file)
            client = {"entry": leg["entry"], "env": leg["env"], "port": port_hint}
            evidence["launcher"] = {k: v for k, v in leg.items() if k not in ("layout", "env")}
        if args.leg in ("bank", "all"):
            # One port for the bank's daemons: the one the client was registered with.
            port = client["port"] if client else h.daemons.free_port()
            evidence["bank"] = bank_leg(root, args.rust_daemon.resolve(), log, owned, client, port)
        ok = all(s["ok"] for s in log.steps)
    except Failed as exc:
        evidence["failure"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - a crash is a failed rehearsal, recorded
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        evidence["steps"] = log.steps
        evidence["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        evidence["result"] = "pass" if ok else "fail"
        for name in owned:
            try:
                h.pg.drop(name)
            except Exception as exc:  # noqa: BLE001 - report, never mask the result
                evidence.setdefault("cleanup_errors", []).append(f"{name}: {exc}")
        evidence = scrub(json.loads(json.dumps(evidence, default=str)), scratch=root)
        args.out.write_text(json.dumps(evidence, indent=1) + "\n", encoding="utf-8")
    print(f"rehearsal {evidence['result']}: {len(log.steps)} steps, evidence {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
