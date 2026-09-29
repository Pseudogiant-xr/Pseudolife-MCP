"""Start the Claude or Codex extractor shim from configuration, not from a
baked command line.

    python ops/shim_autostart.py run claude|codex [overrides] [--dry-run] [--foreground]
    python ops/shim_autostart.py config claude|codex --model X --port N ...   # write ops/.env
    python ops/shim_autostart.py show claude|codex [--json]                   # the resolved settings
    python ops/shim_autostart.py restart claude|codex [--dry-run] [--force]   # stop the shim, start it again

The scheduled task (Windows) and the systemd --user unit (Linux) that the
autostart installers register run ``run <kind>`` and nothing else, so the
model, prompt file, port, host, CLI path, interpreter and log file are
read from ``ops/.env`` at every start. Changing a default is then an edit
plus ``restart`` — no elevation; Task Scheduler needs an elevated shell
only to REGISTER a task, which stays a one-time install step. The
installers' flags (``-Model`` / ``--model`` and so on) keep working: they
are written into ``ops/.env`` by ``config``, and any flag given to ``run``
overrides the file for that start.

Settings, in rising precedence: the defaults below (the same values the
installers name), the ``PSEUDOLIFE_<KIND>_SHIM_*`` keys in ``ops/.env``,
then the flags. Standard library only; run from the checkout it sits in
(``--repo`` names another one, for the tests).

A registration from before the runner (the task or unit still carries
``--model`` and the rest on its command line) keeps starting those values
at logon: ``show`` says so, ``restart`` refuses rather than start the
ops/.env values under a task that brings its own back at the next logon
(``--force`` restarts from ops/.env anyway), and the fix is one more run
of the installer, elevated on Windows.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BLOCK_BEGIN = ("# >>> pseudolife shim autostart (managed by ops/shim_autostart.py; edit values here, "
               "then: python ops/shim_autostart.py restart claude|codex) >>>")
BLOCK_END = "# <<< pseudolife shim autostart <<<"

KINDS = {
    "claude": {
        "script": "evals/claude_shim.py",
        "prefix": "PSEUDOLIFE_CLAUDE_SHIM_",
        "task": "Pseudolife Claude Shim",
        "unit": "pseudolife-sonnet-shim.service",     # the unit's historical name; installs keep it
        "installer": "ops/install-shim-autostart",
        "defaults": {
            # claude-opus-5-5 since 2026-09-29: it clears the paired extraction-ladder
            # gate with no regression against claude-opus-5 (evals/results/
            # ladder-opus55-paired-verdict-threshold.json); the installers name the same.
            "model": "claude-opus-5-5",
            "port": "8082",
            # --system-prompt-file REPLACES the shipped prompt prefix: this file is
            # what the shim sends. v5 since 2026-09-07 (evals/results/
            # ladder-shimv5-paired-verdict-threshold.json).
            "prompt_file": "evals/prompts/sonnet_extractor_v5.md",
            "log": "~/.pseudolife-mcp/claude-shim.log",
        },
        "keys": ("model", "port", "prompt_file", "host", "cli", "python", "log"),
    },
    "codex": {
        "script": "evals/codex_shim.py",
        "prefix": "PSEUDOLIFE_CODEX_SHIM_",
        "task": "Pseudolife Codex Shim",
        "unit": "pseudolife-codex-shim.service",
        "installer": "ops/install-codex-shim-autostart",
        "defaults": {
            "model": "gpt-5.6-terra",
            "port": "8086",
            # 1800 s, not the shim's own 300 s: every /health refresh is a real
            # CLI call, metered spend on a free ChatGPT tier.
            "health_ttl": "1800",
            "log": "~/.pseudolife-mcp/codex-shim.log",
        },
        "keys": ("model", "port", "health_ttl", "host", "cli", "python", "log"),
    },
}
# The shim's own CLI lookup (PSEUDOLIFE_SHIM_CLAUDE_CLI / PSEUDOLIFE_SHIM_CODEX_CLI,
# then PATH) applies when no `cli` is set here: the runner passes --cli only
# for a value from ops/.env or a flag.

# Seconds `restart` waits for the new shim to accept a connection on its
# port: the Claude shim's health warm-up before it binds runs 10-30 s.
START_WAIT_SECONDS = 60.0
# Variables of the shell that runs `restart` that must not reach the shim
# and, through it, every CLI call: a Claude Code session's own identity
# (its session id, messaging socket, effort). The scheduled task starts the
# shim with the logon environment, which has none of these.
_SESSION_ENV_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")
_SESSION_ENV_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")


# ── ops/.env ────────────────────────────────────────────────────────────────

_KEY_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if value[0] == '"' else inner
    return value.split(" #", 1)[0].strip()


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not _KEY_SHAPE.fullmatch(key):
        return None
    return key, _unquote(value.strip())


def read_env(path: Path) -> dict:
    """``KEY=VALUE`` lines of an env file (``export`` prefix, quotes and
    comments allowed); the last assignment of a key wins."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    return read_env_lines(lines)


def read_env_lines(lines: list[str]) -> dict:
    values: dict = {}
    for line in lines:
        parsed = _parse_line(line)
        if parsed:
            values[parsed[0]] = parsed[1]
    return values


def env_file(repo: Path) -> Path:
    return repo / "ops" / ".env"


def settings_from_env(kind: str, values: dict) -> dict:
    prefix = KINDS[kind]["prefix"]
    found = {}
    for key in KINDS[kind]["keys"]:
        value = values.get(prefix + key.upper())
        if value:
            found[key] = value
    return found


def _quote(value: str) -> str:
    """A value the runner, docker compose and a shell read the same way:
    bare when it is plain, single-quoted when it has no apostrophe, else
    double-quoted with backslash escapes (which ``read_env`` undoes)."""
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=~\\-]+", value):
        return value
    if "'" not in value:
        return "'" + value + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_config(repo: Path, kind: str, settings: dict) -> tuple[Path, list[str]]:
    """Write ``settings`` for ``kind`` into the autostart block of
    ``ops/.env`` (created from ``.env.example`` when absent): only the keys
    given change; the kind's other keys, the other kind's keys and every
    line outside the block stay. An uncommented ``PSEUDOLIFE_<KIND>_SHIM_*``
    line outside the block is moved into it (the block sits at the end of
    the file, where it would otherwise shadow that line): its value is
    kept unless the same key is given here. Returns the path and the
    keys that were moved."""
    path = env_file(repo)
    if not path.is_file():
        example = repo / "ops" / ".env.example"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(example.read_text(encoding="utf-8") if example.is_file() else "", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    block: list[str] = []
    kept: list[str] = []
    prefix = KINDS[kind]["prefix"]
    moved: dict = {}
    inside = False
    for line in lines:
        if line.strip() == BLOCK_BEGIN:
            inside = True
            continue
        if line.strip() == BLOCK_END:
            inside = False
            continue
        if inside:
            block.append(line)
            continue
        parsed = _parse_line(line)
        if parsed and parsed[0].startswith(prefix):
            moved[parsed[0]] = parsed[1]
            continue
        kept.append(line)
    existing = read_env_lines(block)
    for key, value in moved.items():
        existing[key] = value
    for key, value in settings.items():
        if value not in (None, ""):
            existing[prefix + key.upper()] = str(value)
    while kept and not kept[-1].strip():
        kept.pop()
    out = kept + ["", BLOCK_BEGIN]
    for key in sorted(existing):
        out.append(f"{key}={_quote(existing[key])}")
    out.append(BLOCK_END)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path, sorted(moved)


# ── resolution ──────────────────────────────────────────────────────────────

def default_python(repo: Path) -> str:
    for candidate in (repo / ".venv" / "Scripts" / "python.exe", repo / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def default_host() -> str:
    """127.0.0.1, except on Linux with a docker bridge: host-gateway routes
    container->host traffic to the bridge IP, where a loopback bind is
    invisible to the daemon container. Never 0.0.0.0 (the shim is
    unauthenticated)."""
    if sys.platform.startswith("linux") and shutil.which("ip"):
        try:
            out = subprocess.run(["ip", "-4", "addr", "show", "docker0"], capture_output=True, text=True,
                                 timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if match:
            return match.group(1)
    return "127.0.0.1"


def _expand(path: str) -> str:
    expanded = Path(os.path.expanduser(path))
    return str(expanded if expanded.is_absolute() else expanded.resolve())


def default_log(kind: str) -> str:
    return _expand(KINDS[kind]["defaults"]["log"])


def resolve(kind: str, repo: Path, overrides: dict | None = None, env_values: dict | None = None) -> dict:
    """The settings ``run`` uses: defaults, then ``ops/.env``, then the
    overrides. Paths are made absolute against the checkout; ``~`` in the
    log path expands; a bare interpreter name is looked up on PATH."""
    spec = KINDS[kind]
    settings = dict(spec["defaults"])
    settings.update(settings_from_env(kind, read_env(env_file(repo)) if env_values is None else env_values))
    settings.update({k: str(v) for k, v in (overrides or {}).items() if v not in (None, "")})
    settings.setdefault("python", default_python(repo))
    settings.setdefault("host", default_host())
    if not Path(settings["python"]).is_file():
        settings["python"] = shutil.which(settings["python"]) or settings["python"]
    if "prompt_file" in settings:
        prompt = Path(settings["prompt_file"])
        settings["prompt_file"] = str(prompt if prompt.is_absolute() else repo / prompt)
    settings["log"] = _expand(settings["log"])
    try:
        settings["port"] = str(int(settings["port"]))
    except ValueError:
        raise ValueError(f"{spec['prefix']}PORT must be a number, not {settings['port']!r}") from None
    return settings


def shim_argv(kind: str, repo: Path, settings: dict) -> list[str]:
    spec = KINDS[kind]
    argv = [settings["python"], str(repo / spec["script"]), "--host", settings["host"],
            "--port", settings["port"], "--model", settings["model"]]
    if kind == "claude":
        argv += ["--system-prompt-file", settings["prompt_file"]]
    else:
        argv += ["--health-ttl", settings["health_ttl"]]
    if settings.get("cli"):
        argv += ["--cli", settings["cli"]]
    return argv


def check(kind: str, repo: Path, settings: dict) -> list[str]:
    """What would stop the shim from starting, in words (empty = fine)."""
    problems = []
    if not (repo / KINDS[kind]["script"]).is_file():
        problems.append(f"shim script not found: {repo / KINDS[kind]['script']}")
    if kind == "claude" and not Path(settings["prompt_file"]).is_file():
        problems.append(f"prompt file not found: {settings['prompt_file']}")
    if not Path(settings["python"]).is_file() and not shutil.which(settings["python"]):
        problems.append(f"interpreter not found: {settings['python']}")
    if settings.get("cli") and not Path(settings["cli"]).is_file() and not shutil.which(settings["cli"]):
        problems.append(f"CLI not found: {settings['cli']}")
    return problems


# ── state: the log, the pid file, the registration ──────────────────────────

def _log(log: str, text: str) -> None:
    """One timestamped runner line in the shim's log. The scheduled task has
    no console anyone reads, so this is where a failed start shows."""
    try:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] shim_autostart: {text}\n")
    except OSError:
        pass


def state_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".pseudolife-mcp"


def pid_file(kind: str, repo: Path) -> Path:
    """Per checkout: a second checkout's shim on another port must not be
    what the production checkout's next ``restart`` stops."""
    import hashlib

    digest = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:8]
    return state_dir() / f"{kind}-shim-{digest}.pid"


def _write_pid(kind: str, repo: Path, pid: int, port: str) -> None:
    try:
        pid_file(kind, repo).parent.mkdir(parents=True, exist_ok=True)
        pid_file(kind, repo).write_text(f"{pid} {port}\n", encoding="utf-8")
    except OSError:
        pass


def previous_start(kind: str, repo: Path) -> tuple[int, str] | None:
    """The pid and port of the last detached start from this checkout,
    from the pid file."""
    try:
        parts = pid_file(kind, repo).read_text(encoding="utf-8").split()
        return int(parts[0]), str(int(parts[1]))
    except (OSError, ValueError, IndexError):
        return None


def child_environment(environ: dict | None = None) -> dict:
    """The environment the shim starts with: the caller's, minus a Claude
    Code session's own variables (see ``_SESSION_ENV_PREFIXES``)."""
    source = os.environ if environ is None else environ
    return {name: value for name, value in source.items()
            if name not in _SESSION_ENV_NAMES and not name.startswith(_SESSION_ENV_PREFIXES)}


def _registered_command(kind: str) -> str | None:
    """What the task or unit runs, as text, or ``None`` when there is no
    registration (or it cannot be read)."""
    spec = KINDS[kind]
    if os.name == "nt":
        command = ["schtasks", "/Query", "/TN", spec["task"], "/XML"]
    elif shutil.which("systemctl"):
        command = ["systemctl", "--user", "cat", spec["unit"]]
    else:
        return None
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout
    if os.name == "nt":
        # The task's action is a base64 -EncodedCommand; decode it so the
        # runner's name is visible.
        import base64
        for match in re.finditer(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", text):
            try:
                text += "\n" + base64.b64decode(match.group(1)).decode("utf-16-le", errors="replace")
            except (ValueError, UnicodeDecodeError):
                pass
    return text


def registration_carries_its_values(kind: str, text: str | None = None) -> bool:
    """Whether the registered task or unit predates this runner: its
    command line names the shim script and the values, not
    ``shim_autostart.py run``. ``text`` is the registration as
    :func:`_registered_command` reads it; read here when omitted. False
    when nothing is registered."""
    if text is None:
        text = _registered_command(kind)
    return text is not None and "shim_autostart.py" not in text


def registration_note(kind: str) -> str:
    """One line when the registered task or unit does not run this runner
    (an install from before it, whose command line still carries the
    values, or none at all), else ''."""
    spec = KINDS[kind]
    where = f"the scheduled task '{spec['task']}'" if os.name == "nt" else f"the unit {spec['unit']}"
    installer = spec["installer"] + (".ps1" if os.name == "nt" else ".sh")
    elevated = " (elevated)" if os.name == "nt" else ""
    text = _registered_command(kind)
    if text is None:
        if os.name != "nt" and not shutil.which("systemctl"):
            return ""
        return (f"{where} is not registered: the shim will not start at logon until "
                f"{installer} has run once{elevated}")
    if registration_carries_its_values(kind, text):
        return (f"{where} still carries the model and the rest on its command line: at logon it starts "
                f"those, not ops/.env. Run {installer} once{elevated} to switch it to ops/.env")
    if os.name == "nt" and re.search(r"<Enabled>\s*false\s*</Enabled>", text, re.IGNORECASE):
        return (f"{where} is disabled: it will not start the shim at logon "
                f"(Enable-ScheduledTask -TaskName '{spec['task']}' from an elevated PowerShell, "
                f"or run {installer} again)")
    return ""


# ── run / restart ───────────────────────────────────────────────────────────

def _creation_flags(breakaway: bool) -> int:
    """A hidden console the shim and its CLI children inherit, its own
    process group. Not DETACHED_PROCESS: Windows ignores CREATE_NO_WINDOW
    beside it, and a console program started from a process with no
    console gets a new, visible one (the .venv python.exe launcher starts
    the base interpreter that way), which is the blank Windows Terminal tab
    the task's spawner exists to avoid (2026-07-12)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if breakaway:
        flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    return flags


def start(kind: str, repo: Path, settings: dict, *, foreground: bool) -> int:
    argv = shim_argv(kind, repo, settings)
    log = settings["log"]
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    if foreground:
        # systemd: stay the unit's main process. The log from ops/.env takes
        # this process's output before the exec, so the unit's own
        # StandardOutput is only what runs before that.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.close(fd)
        except OSError:
            pass
        os.execvp(argv[0], argv)
    _log(log, f"starting {' '.join(argv)}")
    handle = open(log, "ab")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": handle, "stderr": subprocess.STDOUT, "cwd": str(repo),
                    "env": child_environment()}
    try:
        if os.name == "nt":
            # Out of any job object a harness put this runner in (which would
            # end the shim with the runner); a job that forbids breakaway
            # refuses, so try without.
            try:
                proc = subprocess.Popen(argv, creationflags=_creation_flags(True), **kwargs)
            except OSError:
                proc = subprocess.Popen(argv, creationflags=_creation_flags(False), **kwargs)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
    finally:
        handle.close()
    _write_pid(kind, repo, proc.pid, settings["port"])
    line = (f"started {KINDS[kind]['script']} (pid {proc.pid}, {settings['model']}, "
            f"{settings['host']}:{settings['port']}, log {log})")
    _log(log, line)
    print(line)
    return 0


def _windows_stop_command(kind: str, ports: list[str], pids: list[int]) -> list[str]:
    """Stop every process whose command line is this shim script on one of
    ``ports`` (the configured one and the last started one), or whose pid
    the pid file names and whose command line is still this script."""
    script = KINDS[kind]["script"].rsplit("/", 1)[-1].replace(".", r"\.")
    alternatives = "|".join(dict.fromkeys(ports))
    port_pattern = f"--port\\s+({alternatives})(\\s|$)"
    conditions = [f"($_.CommandLine -match '{port_pattern}')"]
    if pids:
        conditions.append(f"($_.ProcessId -in @({', '.join(str(p) for p in pids)}))")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -and "
            f"($_.CommandLine -match '{script}') -and ({' -or '.join(conditions)}) }} "
            f"| ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"]


def restart_plan(kind: str, settings: dict, repo: Path = ROOT) -> list[list[str]]:
    """The commands ``restart`` runs, in order. On Windows the shim is
    stopped and started here, not through the scheduler: running a task
    needs a right the task's owner does not always hold unelevated, and a
    task that is disabled or missing would leave no shim after the stop.
    The task still starts the shim at logon."""
    spec = KINDS[kind]
    previous = previous_start(kind, repo)
    ports = [settings["port"]] + ([previous[1]] if previous else [])
    if os.name == "nt":
        return [_windows_stop_command(kind, ports, [previous[0]] if previous else []), ["<start detached>"]]
    if shutil.which("systemctl"):
        return [["systemctl", "--user", "restart", spec["unit"]]]
    script = spec["script"].rsplit("/", 1)[-1]
    return [["pkill", "-f", f"{script}.*--port ({'|'.join(dict.fromkeys(ports))})( |$)"], ["<start detached>"]]


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                              timeout=30, check=False)
        return str(pid) in proc.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _port_open(host: str, port: str, deadline: float) -> bool:
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    while time.time() < deadline:
        try:
            with socket.create_connection((target, int(port)), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def restart(kind: str, repo: Path, settings: dict, *, dry_run: bool) -> int:
    plan = restart_plan(kind, settings, repo)
    if dry_run:
        for command in plan:
            print(" ".join(command))
        return 0
    for command in plan:
        if command == ["<start detached>"]:
            if start(kind, repo, settings, foreground=False) != 0:
                return 1
            continue
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0 and command[0] not in ("pkill",):
            print(f"{' '.join(command[:3])} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}",
                  file=sys.stderr)
            return 1
    if _port_open(settings["host"], settings["port"], time.time() + START_WAIT_SECONDS):
        print(f"restarted the {kind} shim ({settings['model']}, {settings['host']}:{settings['port']}); "
              f"it reads ops/.env at every start")
        started = previous_start(kind, repo)
        if os.name == "nt" and started and not _pid_alive(started[0]):
            print(f"note: the shim started by this restart (pid {started[0]}) has exited; what listens on "
                  f"{settings['host']}:{settings['port']} is another process. See {settings['log']}",
                  file=sys.stderr)
    else:
        print(f"the {kind} shim was started but is not yet listening on {settings['host']}:{settings['port']} "
              f"after {START_WAIT_SECONDS:.0f}s; see {settings['log']}", file=sys.stderr)
    note = registration_note(kind)
    if note:
        print(f"note: {note}", file=sys.stderr)
    return 0


# ── command line ────────────────────────────────────────────────────────────

def _add_settings(parser: argparse.ArgumentParser, kind_positional: bool = True) -> None:
    if kind_positional:
        parser.add_argument("kind", choices=sorted(KINDS))
    parser.add_argument("--model")
    parser.add_argument("--port", type=int)
    parser.add_argument("--prompt-file", dest="prompt_file", help="claude only")
    parser.add_argument("--health-ttl", dest="health_ttl", type=int, help="codex only")
    parser.add_argument("--host")
    parser.add_argument("--cli")
    parser.add_argument("--python")
    parser.add_argument("--log")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shim_autostart", description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=str(ROOT), help="the checkout (default: this one)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="start the shim from ops/.env plus any overrides")
    _add_settings(p_run)
    p_run.add_argument("--dry-run", action="store_true", help="print the resolved command line as JSON")
    p_run.add_argument("--foreground", action="store_true", help="exec the shim in this process (systemd)")
    p_config = sub.add_parser("config", help="write settings into the autostart block of ops/.env")
    _add_settings(p_config)
    p_show = sub.add_parser("show", help="the resolved settings")
    p_show.add_argument("kind", choices=sorted(KINDS))
    p_show.add_argument("--json", action="store_true")
    p_restart = sub.add_parser("restart", help="stop the running shim and start it again (no elevation)")
    p_restart.add_argument("kind", choices=sorted(KINDS))
    p_restart.add_argument("--dry-run", action="store_true")
    p_restart.add_argument("--force", action="store_true",
                           help="restart from ops/.env even when the registered task or unit still "
                                "carries its own values (those come back at the next logon)")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    kind = args.kind
    keys = KINDS[kind]["keys"]
    overrides = {k: getattr(args, k, None) for k in keys} if args.command in ("run", "config") else {}
    if args.command == "config":
        given = {k: v for k, v in overrides.items() if v not in (None, "")}
        path, moved = write_config(repo, kind, given)
        print(f"wrote {', '.join(sorted(given)) or 'nothing'} for the {kind} shim into {path}")
        if moved:
            print(f"moved {', '.join(moved)} into the autostart block (the block is read last)")
        return 0
    detached_run = args.command == "run" and not args.dry_run and not args.foreground
    try:
        settings = resolve(kind, repo, overrides)
    except ValueError as exc:
        print(f"cannot start the {kind} shim: {exc}", file=sys.stderr)
        if detached_run:
            _log(default_log(kind), f"cannot start the {kind} shim: {exc}")
        return 1
    if args.command == "show":
        if args.json:
            print(json.dumps({"settings": settings, "argv": shim_argv(kind, repo, settings),
                              "problems": check(kind, repo, settings),
                              "registration": registration_note(kind)}, indent=2))
        else:
            for key in keys:
                print(f"{key:<12} {settings.get(key, '')}")
            print("argv         " + " ".join(shim_argv(kind, repo, settings)))
            for problem in check(kind, repo, settings):
                print(f"problem      {problem}")
            note = registration_note(kind)
            if note:
                print(f"registration {note}")
        return 0
    problems = check(kind, repo, settings)
    if args.command == "run":
        if args.dry_run:
            print(json.dumps({"argv": shim_argv(kind, repo, settings), "log": settings["log"],
                              "problems": problems}, indent=2))
            return 0 if not problems else 1
        if problems:
            for problem in problems:
                print(f"cannot start the {kind} shim: {problem}", file=sys.stderr)
                if detached_run:
                    _log(settings["log"], f"cannot start the {kind} shim: {problem}")
            return 1
        try:
            return start(kind, repo, settings, foreground=args.foreground)
        except OSError as exc:
            print(f"cannot start the {kind} shim: {exc}", file=sys.stderr)
            _log(settings["log"], f"cannot start the {kind} shim: {exc}")
            return 1
    if args.command == "restart":
        if problems and not args.dry_run:
            for problem in problems:
                print(f"cannot start the {kind} shim: {problem}", file=sys.stderr)
            return 1
        # An install registered before this runner would be stopped and
        # started from ops/.env — the defaults, when it never wrote the
        # block — with its own values coming back at the next logon.
        if not args.dry_run and not args.force and registration_carries_its_values(kind):
            print(f"restart refused: {registration_note(kind)}. "
                  f"--force restarts from ops/.env anyway", file=sys.stderr)
            return 1
        return restart(kind, repo, settings, dry_run=args.dry_run)
    return 2


if __name__ == "__main__":
    sys.exit(main())
