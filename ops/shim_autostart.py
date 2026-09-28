"""Start the Claude or Codex extractor shim from configuration, not from a
baked command line.

    python ops/shim_autostart.py run claude|codex [overrides] [--dry-run] [--foreground]
    python ops/shim_autostart.py config claude|codex --model X --port N ...   # write ops/.env
    python ops/shim_autostart.py show claude|codex [--json]                   # the resolved settings
    python ops/shim_autostart.py restart claude|codex [--dry-run]             # stop the shim, start it again

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
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
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
        "cli_name": "claude",
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
        "cli_name": "codex",
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


# ── ops/.env ────────────────────────────────────────────────────────────────

def read_env(path: Path) -> dict:
    """``KEY=VALUE`` lines of an env file (``export`` prefix, quotes and
    comments allowed); the last assignment of a key wins."""
    values: dict = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
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


def write_config(repo: Path, kind: str, settings: dict) -> Path:
    """Write ``settings`` for ``kind`` into the autostart block of
    ``ops/.env`` (created from ``.env.example`` when absent), keeping the
    other kind's keys and every line outside the block as they are."""
    path = env_file(repo)
    if not path.is_file():
        example = repo / "ops" / ".env.example"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(example.read_text(encoding="utf-8") if example.is_file() else "", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    block: list[str] = []
    kept: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == BLOCK_BEGIN:
            inside = True
            continue
        if line.strip() == BLOCK_END:
            inside = False
            continue
        (block if inside else kept).append(line)
    existing = read_env_lines(block)
    prefix = KINDS[kind]["prefix"]
    for key in KINDS[kind]["keys"]:
        existing.pop(prefix + key.upper(), None)
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
    return path


def read_env_lines(lines: list[str]) -> dict:
    values: dict = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _quote(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_./:@%+=~\\-]+", value) else '"' + value.replace('"', '\\"') + '"'


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


def resolve(kind: str, repo: Path, overrides: dict | None = None, env_values: dict | None = None) -> dict:
    """The settings ``run`` uses: defaults, then ``ops/.env``, then the
    overrides. Paths are made absolute against the checkout; ``~`` in the
    log path expands."""
    spec = KINDS[kind]
    settings = dict(spec["defaults"])
    settings.update(settings_from_env(kind, read_env(env_file(repo)) if env_values is None else env_values))
    settings.update({k: str(v) for k, v in (overrides or {}).items() if v not in (None, "")})
    settings.setdefault("python", default_python(repo))
    settings.setdefault("host", default_host())
    if "cli" not in settings:
        found = shutil.which(spec["cli_name"])
        if found:
            settings["cli"] = found
    if "prompt_file" in settings:
        prompt = Path(settings["prompt_file"])
        settings["prompt_file"] = str(prompt if prompt.is_absolute() else repo / prompt)
    settings["log"] = str(Path(os.path.expanduser(settings["log"])).resolve()
                          if not Path(os.path.expanduser(settings["log"])).is_absolute()
                          else Path(os.path.expanduser(settings["log"])))
    settings["port"] = str(int(settings["port"]))
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


# ── run / restart ───────────────────────────────────────────────────────────

def start(kind: str, repo: Path, settings: dict, *, foreground: bool) -> int:
    argv = shim_argv(kind, repo, settings)
    log = Path(settings["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    if foreground:
        # systemd: stay the unit's main process; the unit appends stdout/stderr to the log.
        sys.stdout.flush()
        os.execv(argv[0], argv)
    handle = open(log, "ab")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": handle, "stderr": subprocess.STDOUT, "cwd": str(repo)}
    if os.name == "nt":
        # No console at all (a hidden console still gets a Windows Terminal
        # tab pinned to it), its own process group, detached from this one.
        kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    handle.close()
    print(f"started {KINDS[kind]['script']} (pid {proc.pid}, {settings['model']}, {settings['host']}:{settings['port']}, log {log})")
    return 0


def _windows_stop_command(kind: str, port: str) -> list[str]:
    script = KINDS[kind]["script"].rsplit("/", 1)[-1].replace(".", r"\.")
    pattern = f"{script}.*--port\\s+{port}(\\s|$)"
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -and ($_.CommandLine -match '{pattern}') }} "
            f"| ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"]


def restart_plan(kind: str, settings: dict) -> list[list[str]]:
    """The commands ``restart`` runs, in order."""
    spec = KINDS[kind]
    if os.name == "nt":
        return [_windows_stop_command(kind, settings["port"]),
                ["schtasks", "/Run", "/TN", spec["task"]]]
    if shutil.which("systemctl"):
        return [["systemctl", "--user", "restart", spec["unit"]]]
    script = spec["script"].rsplit("/", 1)[-1]
    return [["pkill", "-f", f"{script}.*--port {settings['port']}"], ["<start detached>"]]


def restart(kind: str, repo: Path, settings: dict, *, dry_run: bool) -> int:
    plan = restart_plan(kind, settings)
    if dry_run:
        for command in plan:
            print(" ".join(command))
        return 0
    for command in plan:
        if command == ["<start detached>"]:
            return start(kind, repo, settings, foreground=False)
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0 and command[0] not in ("pkill",):
            print(f"{' '.join(command[:3])} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}",
                  file=sys.stderr)
            return 1
    print(f"restarted the {kind} shim ({settings['model']}, port {settings['port']}); it reads ops/.env at every start")
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
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    kind = args.kind
    keys = KINDS[kind]["keys"]
    overrides = {k: getattr(args, k, None) for k in keys} if args.command in ("run", "config") else {}
    if args.command == "config":
        given = {k: v for k, v in overrides.items() if v not in (None, "")}
        path = write_config(repo, kind, given)
        print(f"wrote {', '.join(sorted(given)) or 'nothing'} for the {kind} shim into {path}")
        return 0
    settings = resolve(kind, repo, overrides)
    if args.command == "show":
        if args.json:
            print(json.dumps({"settings": settings, "argv": shim_argv(kind, repo, settings),
                              "problems": check(kind, repo, settings)}, indent=2))
        else:
            for key in keys:
                print(f"{key:<12} {settings.get(key, '')}")
            print("argv         " + " ".join(shim_argv(kind, repo, settings)))
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
            return 1
        return start(kind, repo, settings, foreground=args.foreground)
    if args.command == "restart":
        if problems and not args.dry_run:
            for problem in problems:
                print(f"cannot start the {kind} shim: {problem}", file=sys.stderr)
            return 1
        return restart(kind, repo, settings, dry_run=args.dry_run)
    return 2


if __name__ == "__main__":
    sys.exit(main())
