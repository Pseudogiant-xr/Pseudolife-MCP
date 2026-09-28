"""``pseudolife-mcp update`` — one implementation of the daemon and client update.

Updating used to mean running scripts by hand from a checkout: pull, then
``ops/update.ps1 -All``, then the shim, the plugin cache and Codex's hook
copy each in turn. This module is the one place the sequence lives; the
checkout scripts (``ops/update.ps1``, ``ops/update.sh``) are thin wrappers
that call it with ``--checkout``, and ``pseudolife-mcp update`` runs it
from an installed package with no checkout at all.

Docker tier, release mode (no checkout)::

    pseudolife-mcp update                # the newest release on PyPI
    pseudolife-mcp update --tag 0.15.1   # a pinned release
    pseudolife-mcp update --check        # report whether a newer release exists (exit 0) or not (exit 3)

pulls the pinned GHCR daemon image, backs the bank up (the checkout's
backup script when the container's compose project still has one, else
this module's own pg_dump + state tar), tags the running image for
rollback, recreates ONLY the daemon container from the compose files the
running container was created with (plus the GHCR overlay), waits for
``/health`` at the new version, then moves the client side
(:mod:`pseudolife_memory.client_updates`): a new shim runtime from PyPI
beside the running one, the plugin cache from the marketplace, and the
Codex hook step, which is printed because it needs consent.

Docker tier, checkout mode (``--checkout <repo>``; what the wrappers pass)
rebuilds the daemon image from the tree instead: the clean-tree guard and
build stamp, the checkout's backup and retention scripts, the rollback-tag
guard against an unvalidated build, ``docker compose up -d --no-deps
--build``, health, then the clients from the checkout with ``--all``.

Pip / lite tier (no daemon container) upgrades the package in the
interpreter that holds it (pip, or pipx when it is pipx's venv) and
restarts nothing: it says what to restart.

Every external command goes through :func:`run_cli` (``docker`` is named
by ``PSEUDOLIFE_DOCKER`` for the tests) and every HTTP read through
:func:`fetch_json`, so the whole sequence runs against fakes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pseudolife_memory import __version__

DAEMON_CONTAINER = "pseudolife-mcp-daemon"
PG_CONTAINER = "pseudolife-mcp-postgres"
DAEMON_SERVICE = "pseudolife-daemon"
GHCR_IMAGE = "ghcr.io/pseudogiant-xr/pseudolife-daemon"
DEFAULT_DAEMON_URL = "http://127.0.0.1:8765"
PYPI_JSON = "https://pypi.org/pypi/pseudolife-mcp/json"
BUNDLED_COMPOSE = Path(__file__).resolve().parent / "compose"
_DUMP_MARKER = "PostgreSQL database dump complete"


# ── seams ───────────────────────────────────────────────────────────────────

def run_cli(argv, *, timeout: int = 3600, cwd: str | None = None,
            env: dict | None = None, stream: bool = False) -> tuple[int, str]:
    """Run a command and return ``(returncode, combined output)``. With
    ``stream`` the child writes to this process's stdout/stderr (a build
    is long and its progress belongs on the terminal) and the output
    returned is empty."""
    try:
        if stream:
            proc = subprocess.run([str(a) for a in argv], timeout=timeout, check=False,
                                  stdin=subprocess.DEVNULL, cwd=cwd, env=env)
            return proc.returncode, ""
        proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              timeout=timeout, check=False, errors="replace",
                              stdin=subprocess.DEVNULL, cwd=cwd, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def which(name: str) -> str | None:
    return shutil.which(name)


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())


def fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    """The JSON document at ``url``, or ``None`` when it cannot be read."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 — operator-supplied URL
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) else None


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def docker_cmd() -> str:
    return os.environ.get("PSEUDOLIFE_DOCKER") or "docker"


def data_dir() -> Path:
    return Path(os.environ.get("PSEUDOLIFE_MCP_DATA_DIR") or home() / ".pseudolife-mcp")


# ── options ─────────────────────────────────────────────────────────────────

@dataclass
class Options:
    checkout: Path | None = None       # rebuild from this tree (the wrappers); None = a release
    tag: str | None = None             # release version to install; default: the newest on PyPI
    rollback_suffix: str = ""          # -Tag / --tag of the wrappers: the rollback tag's suffix
    no_backup: bool = False
    keep_rollbacks: int = 2
    keep_cache_hours: int = 168
    no_cache_prune: bool = False
    force_rollback_tag: bool = False
    allow_dirty: bool = False
    health_retries: int = 30
    health_delay_ms: int = 1500
    all_clients: bool = False          # checkout mode: also the client side (-All)
    clients_only: bool = False
    daemon_only: bool = False
    reinstall: bool = False            # release mode: proceed at the same version
    allow_downgrade: bool = False      # release mode: a target older than the daemon
    env_file: Path | None = None       # release mode: the compose env file when the labelled one is gone
    check: bool = False
    as_json: bool = False
    daemon_url: str = DEFAULT_DAEMON_URL


@dataclass
class Report:
    tier: str = ""
    mode: str = ""
    target: str | None = None
    current: str | None = None
    steps: list = field(default_factory=list)
    rollback: dict = field(default_factory=dict)
    clients: dict | None = None
    ok: bool = True
    exit_code: int = 0


class UpdateError(RuntimeError):
    """The update stops here; the message says why and what to do."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


# ── helpers ─────────────────────────────────────────────────────────────────

def version_key(value: str) -> tuple[int, ...] | None:
    match = re.match(r"^(\d+(?:\.\d+)*)", value or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _env_file_sets_credentials(env_file: Path | None) -> bool:
    if env_file is None or not env_file.is_file():
        return False
    try:
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(re.match(r"^\s*(?:export\s+)?PSEUDOLIFE_MCP_TOKENS?\s*=", line) for line in lines)


def compose_environment(env_file: Path | None, extra: dict | None = None) -> dict:
    """The environment a ``docker compose`` call gets: this process's, with
    the bearer variables removed when the env file sets its own (explicit
    machine-local authentication must not be shadowed by a client's
    credentials inherited from the calling process), plus ``extra``."""
    env = dict(os.environ)
    if _env_file_sets_credentials(env_file):
        for name in list(env):
            if name.upper() in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS"):
                env.pop(name)
    env.update(extra or {})
    return env


def tree_state(repo: Path) -> dict:
    """``{"sha", "dirty", "lines", "error"}`` of a checkout: ``dirty`` is
    ``"true"``, ``"false"`` or ``"unknown"`` (git could not report; ``error``
    quotes its reason). Only stdout is data: git may write warnings or
    GIT_TRACE lines to stderr on success."""
    state = {"sha": "unknown", "dirty": "unknown", "lines": [], "error": "git is not on PATH"}
    git = which("git")
    if not git:
        return state
    code, out = _run_stdout([git, "-C", str(repo), "rev-parse", "HEAD"])
    if code != 0:
        state["error"] = out.strip()
        return state
    sha = out.strip().splitlines()[0].strip() if out.strip() else ""
    code, out = _run_stdout([git, "-C", str(repo), "status", "--porcelain", "--untracked-files=normal"])
    if code != 0:
        state["error"] = out.strip()
        return state
    lines = [line for line in out.splitlines() if line.strip()]
    state.update({"sha": sha, "dirty": "true" if lines else "false", "lines": lines, "error": None})
    return state


def _run_stdout(argv) -> tuple[int, str]:
    """Like :func:`run_cli` but stdout alone on success, stderr on failure."""
    try:
        proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=120,
                              check=False, errors="replace", stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout))


# ── the update ──────────────────────────────────────────────────────────────

class Update:
    def __init__(self, options: Options, *, log: Callable[[str], None] | None = None,
                 warn: Callable[[str], None] | None = None):
        self.o = options
        self.report = Report()
        self._log = log or (lambda line: print(f"==> {line}"))
        self._warn = warn or (lambda line: print(f"WARNING: {line}", file=sys.stderr))
        self.docker = docker_cmd()
        self.rollback_lines: list[str] = []

    # -- output ------------------------------------------------------------
    def step(self, text: str) -> None:
        self.report.steps.append(text)
        if not self.o.as_json:
            self._log(text)

    def warn(self, text: str) -> None:
        self.report.steps.append(f"WARNING: {text}")
        if not self.o.as_json:
            self._warn(text)

    # -- entry -------------------------------------------------------------
    def run(self) -> int:
        try:
            self._run()
        except UpdateError as exc:
            self.warn(str(exc))
            self.report.ok = False
            self.report.exit_code = exc.exit_code
        if self.o.as_json:
            print(json.dumps(self.report.__dict__, indent=2, default=str))
        return self.report.exit_code

    def _run(self) -> None:
        if self.o.checkout is not None:
            for name, value in (("--clients-only", self.o.clients_only), ("--tag", self.o.tag),
                                ("--reinstall", self.o.reinstall), ("--allow-downgrade", self.o.allow_downgrade),
                                ("--env-file", self.o.env_file)):
                if value:
                    raise UpdateError(f"{name} is a release-mode option; a checkout deploy builds what the tree "
                                      f"holds (for the clients alone: python ops/update_clients.py)", 2)
        tier = self.detect_tier()
        self.report.tier = tier
        if self.o.check:
            self.check_for_release(tier)
            return
        if tier == "pip":
            if self.o.daemon_only or self.o.clients_only:
                raise UpdateError("no daemon container here: this is a pip install, which has no daemon/client "
                                  "halves; run without --daemon-only / --clients-only", 2)
            self.upgrade_pip()
            return
        if self.o.checkout is not None:
            self.report.mode = "checkout"
            self.deploy_checkout()
        else:
            self.report.mode = "release"
            self.deploy_release()

    # -- tier --------------------------------------------------------------
    def detect_tier(self) -> str:
        """``docker`` when the daemon container exists (running or stopped)
        or a checkout with the compose file was named; ``pip`` when Docker
        answers that there is no such container, or there is no docker at
        all. Docker present but not answering (Docker Desktop stopped, the
        engine down) is neither: the daemon may well be a container, so
        the update stops rather than pip-upgrade the shim's own runtime."""
        if self.o.checkout is not None:
            return "docker"
        if not which(self.docker) and self.docker == "docker":
            return "pip"
        code, out = run_cli([self.docker, "inspect", "-f", "{{.Id}}", DAEMON_CONTAINER], timeout=60)
        if code == 0:
            return "docker"
        if "no such object" in out.lower() or "no such container" in out.lower():
            return "pip"
        raise UpdateError(f"docker is installed but did not answer ({_last_line(out)}): start Docker and retry. "
                          f"Nothing was changed", 2)

    # -- versions ----------------------------------------------------------
    def latest_release(self) -> str | None:
        data = fetch_json(PYPI_JSON, timeout=10)
        version = ((data or {}).get("info") or {}).get("version")
        return version if isinstance(version, str) and version_key(version) else None

    def daemon_health(self, timeout: float = 3.0) -> dict | None:
        return fetch_json(self.o.daemon_url.rstrip("/") + "/health", timeout=timeout)

    def current_version(self, tier: str) -> str | None:
        if tier == "docker":
            health = self.daemon_health()
            version = (health or {}).get("version")
            return version if isinstance(version, str) else None
        return __version__

    def target_version(self) -> str:
        if self.o.tag:
            return self.o.tag
        latest = self.latest_release()
        if latest:
            return latest
        raise UpdateError("could not read the newest release from PyPI; name one with --tag <version>. "
                          "Nothing was changed", 2)

    def refuse_downgrade(self, current: str | None, target: str) -> None:
        if current and version_key(current) and version_key(target) and version_key(target) < version_key(current) \
                and not self.o.allow_downgrade:
            raise UpdateError(f"{target} is older than the running {current}; a downgrade needs --tag {target} "
                              f"--allow-downgrade (the bank's schema may be newer than that release knows). "
                              f"Nothing was changed", 2)

    def check_for_release(self, tier: str) -> None:
        current = self.current_version(tier)
        latest = self.o.tag or self.latest_release()
        self.report.current, self.report.target = current, latest
        if latest is None:
            raise UpdateError("could not read the newest release from PyPI", 2)
        if current is None:
            raise UpdateError("could not read the current version (daemon /health did not answer)", 2)
        if version_key(latest) and version_key(current) and version_key(latest) > version_key(current):
            self.step(f"update available: {current} -> {latest} (run: pseudolife-mcp update)")
            return
        self.step(f"current: {current} is the newest release")
        self.report.exit_code = 3

    # -- pip tier ----------------------------------------------------------
    def upgrade_pip(self) -> None:
        """The pip / lite tier: the package is upgraded in the interpreter
        that holds it. Never in place over a shim runtime (the daemon is a
        container that was not reachable), an editable checkout (the
        2026-09-21 incident) or, on Windows, the running install itself
        (pip cannot replace a running console script); those get the
        command printed instead."""
        from pseudolife_memory import client_updates, runtimes
        target = self.target_version()
        self.report.target, self.report.current = target, __version__
        self.refuse_downgrade(__version__, target)
        if target == __version__ and not self.o.reinstall:
            self.step(f"pseudolife-mcp {__version__} is already the target version; --reinstall to reinstall")
            return
        prefix = Path(sys.prefix)
        layout = runtimes.default_layout()
        if runtimes._under(str(prefix), layout.root):
            raise UpdateError(f"this command runs from the shim runtime {prefix}, which serves no daemon: the "
                              f"daemon is a container this run could not see. Start Docker and retry; a shim "
                              f"runtime is never pip-upgraded in place", 2)
        kind, where = client_updates.install_kind(Path(sys.executable))
        if kind in ("editable", "unknown"):
            raise UpdateError(f"the running package is {'an editable install of ' + where if kind == 'editable' else 'of a kind this cannot tell'} "
                              f"({sys.executable}); it is never pip-upgraded in place. Pull that checkout, or "
                              f"upgrade it yourself: \"{sys.executable}\" -m pip install --upgrade pseudolife-mcp=={target}", 2)
        pipx_home = os.environ.get("PIPX_HOME") or str(home() / ".local" / "pipx")
        under_pipx = "pipx" in prefix.parts or str(prefix).lower().startswith(str(pipx_home).lower())
        if under_pipx:
            argv = [which("pipx") or "pipx", "install", "--force", f"pseudolife-mcp=={target}"]
        else:
            argv = [sys.executable, "-m", "pip", "install", "--upgrade", f"pseudolife-mcp=={target}"]
        shown = " ".join(argv)
        self.step("a pip install carries the bank with it: back it up first (pseudolife-mcp backup) if you have not")
        if os.name == "nt" or under_pipx:
            # pipx deletes the venv this command runs from; on Windows pip cannot
            # replace the running console script either. The command is the deliverable.
            self.step(f"run this from a shell where no pseudolife-mcp process is running: {shown}")
            self.step("then restart the daemon (`pseudolife-mcp serve`) and start new sessions")
            return
        self.step(f"upgrading the package: {shown}")
        code, out = run_cli(argv, timeout=1800)
        if code != 0:
            raise UpdateError(f"the package upgrade failed ({_last_line(out)}); run it yourself with every "
                              f"pseudolife-mcp process stopped: {shown}")
        self.step(f"pseudolife-mcp {target} installed. Nothing running was restarted: restart the daemon "
                  f"(`pseudolife-mcp serve`) and start new sessions to run it")

    # -- docker: shared steps ------------------------------------------------
    def backup(self, checkout: Path | None) -> None:
        if self.o.no_backup:
            self.warn("skipping backup (--no-backup)")
            return
        script = self._checkout_script(checkout, "backup")
        if script is not None:
            self.step(f"backing up the bank ({script.name})...")
            code, _ = run_cli(self._script_argv(script), timeout=3600, stream=not self.o.as_json)
            if code != 0:
                raise UpdateError(f"the backup failed ({script}); nothing was deployed")
            return
        self.step("backing up the bank (pg_dump in the container + state volume tar)...")
        self.builtin_backup()

    def builtin_backup(self) -> dict:
        """pg_dump + gzip inside the Postgres container, copied out and
        checked for the dump's closing marker (a killed dump is a valid gzip
        of a truncated file), and the daemon's /data tarred from inside its
        container. The artifacts land in ``<data dir>/backups`` under the
        checkout scripts' names (``ops/restore.* -BackupFile <path>`` reads
        them; they are not in the checkout's ``data/backups``), and files
        older than seven days that match those names in that directory are
        rotated once the new pair is in place. No manifest, row-count gate
        or mirror: those stay with the checkout's ``ops/backup.*``."""
        out_dir = data_dir() / "backups"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _stamp()
        dump = out_dir / f"pseudolife_memory-{stamp}.sql.gz"
        tmp = f"/tmp/pl_backup-{stamp}.sql.gz"
        code, out = run_cli([self.docker, "exec", PG_CONTAINER, "sh", "-c",
                             f"pg_dump -U pseudolife -d pseudolife_memory -Z9 > {tmp}"], timeout=3600)
        if code != 0:
            run_cli([self.docker, "exec", PG_CONTAINER, "rm", "-f", tmp], timeout=120)
            raise UpdateError(f"pg_dump failed inside {PG_CONTAINER} ({_last_line(out)}); nothing was deployed")
        part = dump.with_name(dump.name + ".part")
        code, out = run_cli([self.docker, "cp", f"{PG_CONTAINER}:{tmp}", str(part)], timeout=3600)
        run_cli([self.docker, "exec", PG_CONTAINER, "rm", "-f", tmp], timeout=120)
        if code != 0 or not part.is_file() or part.stat().st_size == 0:
            raise UpdateError(f"the dump could not be copied out of {PG_CONTAINER} ({_last_line(out)}); nothing was deployed")
        if not _dump_complete(part):
            raise UpdateError(f"the backup is INCOMPLETE ('{_DUMP_MARKER}' missing); the rejected artifact is at {part}. "
                              f"Nothing was deployed")
        part.replace(dump)
        state = out_dir / f"pseudolife_state-{stamp}.tgz"
        state_tmp = f"/tmp/pl_state-{stamp}.tgz"
        code, out = run_cli([self.docker, "exec", DAEMON_CONTAINER, "sh", "-c",
                             f"tar czf {state_tmp} -C /data ."], timeout=3600)
        if code == 0:
            code, out = run_cli([self.docker, "cp", f"{DAEMON_CONTAINER}:{state_tmp}", str(state)], timeout=3600)
            run_cli([self.docker, "exec", DAEMON_CONTAINER, "rm", "-f", state_tmp], timeout=120)
        if code != 0 or not state.is_file() or state.stat().st_size == 0:
            self.warn(f"the state volume could not be archived ({_last_line(out)}); the bank dump {dump.name} is complete")
        self.step(f"backup: {dump}" + (f" + {state.name}" if state.is_file() else "")
                  + f"; restore with ops/restore.* -BackupFile / --backup-file {dump}")
        rotated = _rotate_backups(out_dir, keep_days=7, keep={dump.name, state.name})
        if rotated:
            self.step(f"rotated {len(rotated)} backup file(s) older than 7 days")
        return {"dump": str(dump), "state": str(state) if state.is_file() else None, "rotated": rotated}

    def running_image(self) -> tuple[str | None, str | None]:
        """``(image id, image ref)`` of the daemon container, or Nones."""
        code, out = run_cli([self.docker, "inspect", "-f", "{{.Image}}|{{.Config.Image}}", DAEMON_CONTAINER], timeout=60)
        if code != 0 or "|" not in out:
            return None, None
        image_id, ref = out.strip().splitlines()[-1].split("|", 1)
        return image_id.strip() or None, ref.strip() or None

    def tag_rollback(self, image_tag: str, running_id: str | None, tag_id: str | None,
                     source: str | None = None, rollback: str | None = None,
                     rollback_lines: list[str] | None = None) -> str:
        """Tag the last-good image (``source``, default the version tag
        itself) as ``rollback`` (default ``<image_tag>-<suffix>``). Returns
        the rollback state: ``tagged``, ``kept`` (refused: the version tag
        is not the running daemon's image, so it is an unvalidated build the
        2026-08-13 deploy nearly promoted) or ``none``. ``rollback_lines``
        are the instructions printed for a tagged rollback (release mode
        passes its own, since the GHCR overlay selects the image through
        ``PSEUDOLIFE_IMAGE_TAG``)."""
        suffix = self.o.rollback_suffix or f"pre-update-{_stamp()}"
        rollback = rollback or f"{image_tag}-{suffix}"
        self.report.rollback = {"tag": rollback, "state": "none", "image_tag": image_tag}
        if not tag_id:
            self.warn(f"no current {image_tag} image to tag (first build, or the version was bumped before this image was ever built)")
            self.warn("this deploy has NO rollback image. Rolling back means rebuilding the previous code")
            self.rollback_lines = ["      (no rollback image exists for this deploy - nothing was tagged)",
                                   "      Rebuild the last-good code instead, e.g.:",
                                   "      git checkout master; ops/update.ps1 (or ops/update.sh)"]
            return "none"
        if running_id and running_id != tag_id and not self.o.force_rollback_tag:
            self.warn(f"REFUSING to move the rollback tag: {image_tag} is NOT the image the running daemon deployed "
                      f"({running_id} vs {tag_id})")
            self.warn("that means a build already ran without a completed deploy, so tagging it now would overwrite "
                      "the last-good rollback with an unvalidated image")
            self.warn(f"existing rollback tags are untouched. Re-run with --force-rollback-tag once you are sure "
                      f"{image_tag} IS the image you would want to roll back to")
            self.report.rollback["state"] = "kept"
            repository = image_tag.split(":")[0]
            self.rollback_lines = ["      (the rollback tag was NOT moved this run - see the warning above)",
                                   "      Pick the newest surviving rollback tag and redeploy it:",
                                   f"      docker image ls {repository}",
                                   f"      docker tag <that tag> {image_tag}",
                                   f"      docker compose {self._compose_display()} up -d --no-deps {DAEMON_SERVICE}"]
            return "kept"
        code, out = run_cli([self.docker, "tag", source or image_tag, rollback], timeout=120)
        if code != 0:
            raise UpdateError(f"docker tag {rollback} failed ({_last_line(out)}); nothing was deployed")
        self.report.rollback["state"] = "tagged"
        self.step(f"tagged rollback image: {rollback}")
        if self.o.force_rollback_tag and running_id and running_id != tag_id:
            self.warn(f"--force-rollback-tag: tagged {image_tag} even though the running daemon deployed a different image")
        self.rollback_lines = rollback_lines or [
            f"      docker tag {rollback} {image_tag}",
            f"      docker compose {self._compose_display()} up -d --no-deps {DAEMON_SERVICE}"]
        return "tagged"

    def prune_rollbacks(self, checkout: Path | None, repository: str) -> None:
        script = self._checkout_script(checkout, "prune-rollbacks")
        if script is not None:
            flag = "-Keep" if script.suffix == ".ps1" else "--keep"
            repo_flag = "-Repository" if script.suffix == ".ps1" else "--repository"
            code, out = run_cli(self._script_argv(script) + [flag, str(self.o.keep_rollbacks), repo_flag, repository],
                                timeout=600, stream=not self.o.as_json)
            if code != 0:
                self.warn(f"rollback-tag retention failed (deploy continues): {_last_line(out)}")
            return
        self.builtin_prune_rollbacks(repository)

    def builtin_prune_rollbacks(self, repository: str) -> None:
        """Drop ``<repository>:*-pre-*`` tags beyond the newest N, never an
        image a running container uses."""
        code, out = run_cli([self.docker, "ps", "-q"], timeout=60)
        in_use: set[str] = set()
        ids = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
        if ids:
            code, out = run_cli([self.docker, "inspect", "--format", "{{.Image}}", *ids], timeout=60)
            if code == 0:
                in_use = {line.strip() for line in out.splitlines() if line.strip()}
        code, out = run_cli([self.docker, "image", "ls", repository, "--format", "{{.Repository}}:{{.Tag}}"], timeout=60)
        if code != 0:
            self.warn(f"rollback-tag retention failed (deploy continues): {_last_line(out)}")
            return
        refs = [line.strip() for line in out.splitlines() if re.search(r":.+-pre-", line)]
        candidates = []
        for ref in refs:
            code, out = run_cli([self.docker, "image", "inspect", "--format", "{{.Created}}|{{.Id}}", ref], timeout=60)
            if code == 0 and "|" in out:
                created, image_id = out.strip().split("|", 1)
                candidates.append((created, ref, image_id.strip()))
        candidates.sort(reverse=True)
        for _created, ref, image_id in candidates[self.o.keep_rollbacks:]:
            if image_id in in_use:
                self.step(f"rollback retention: keeping {ref} (image in use by a running container)")
                continue
            code, out = run_cli([self.docker, "rmi", ref], timeout=300)
            if code == 0:
                self.step(f"rollback retention: removed stale tag {ref}")
            else:
                self.warn(f"rollback retention: docker rmi {ref} failed; leaving it")

    def wait_health(self, expect_version: str | None = None) -> dict:
        self.step("waiting for the daemon to report healthy...")
        health = None
        for attempt in range(self.o.health_retries):
            health = self.daemon_health()
            if health and health.get("status") == "ok":
                break
            health = None
            if attempt < self.o.health_retries - 1:
                sleep(self.o.health_delay_ms / 1000.0)
        if not health:
            lines = ["daemon did not report healthy. Logs: docker logs " + DAEMON_CONTAINER, "to roll back:"] + self.rollback_lines
            raise UpdateError("\n".join(lines))
        self.step(f"healthy. version={health.get('version')} schema={health.get('schema')} "
                  f"persist_errors={health.get('persist_errors')}")
        if expect_version and health.get("version") != expect_version:
            lines = [f"the daemon reports version {health.get('version')}, not the {expect_version} that was pulled: "
                     f"the compose project's image setting won (check `docker compose config` for the daemon "
                     f"service). The client side was not moved. To roll back:"] + self.rollback_lines
            raise UpdateError("\n".join(lines))
        if self.rollback_lines and not self.o.as_json:
            print("    Rolled-back deploy if ever needed:")
            for line in self.rollback_lines:
                print(line)
        return health

    def prune_cache(self, checkout: Path | None) -> None:
        if self.o.no_cache_prune:
            return
        script = self._checkout_script(checkout, "prune-build-cache")
        if script is not None:
            flag = "-MaxAgeHours" if script.suffix == ".ps1" else "--max-age-hours"
            code, out = run_cli(self._script_argv(script) + [flag, str(self.o.keep_cache_hours)], timeout=1800,
                                stream=not self.o.as_json)
        else:
            code, out = run_cli([self.docker, "builder", "prune", "-f", "--filter", f"until={self.o.keep_cache_hours}h"],
                                timeout=1800)
        if code != 0:
            self.warn(f"build-cache retention failed (deploy already succeeded): {_last_line(out)}")

    def clients(self, checkout: Path | None, source: str, health: dict | None) -> None:
        """The client side. ``checkout`` is the tree the shim runtime comes
        from in checkout mode; in release mode it is None even when the
        compose project still has one, so the Codex check compares with the
        daemon that was just deployed, not with a checkout at some commit."""
        from pseudolife_memory import client_updates
        self.step("updating the client side (shim, plugin cache, Codex hooks)...")
        report = client_updates.run_steps(("shim", "plugin", "codex"), repo=checkout, source=source,
                                          daemon_digest=(health or {}).get("hooks_digest"))
        self.report.clients = report
        if not self.o.as_json:
            client_updates.print_ladder(report)
        if not report["ok"]:
            self.warn("a client-side step needs attention (see the ladder above); the daemon update itself succeeded")
        self.codex_step(report.get("codex"))

    def codex_step(self, codex: dict | None) -> None:
        """What only the user can do: Codex trusts hooks by hash and asks
        again when a script changes."""
        if not codex or codex.get("state") in ("current", "not-configured", "bundle-present", None):
            return
        self.step("Codex: its hook copy needs re-approval (hooks are trusted by hash). In Codex run /hooks and "
                  "approve the pseudolife-memory handlers, or from a checkout: python ops/setup-codex-hooks.py "
                  "--source plugin --trust ask. Until then Codex sessions start without the memory briefing.")

    # -- docker: checkout mode ---------------------------------------------
    def deploy_checkout(self) -> None:
        repo = Path(self.o.checkout).resolve()
        compose_file = repo / "ops" / "docker-compose.yml"
        if not compose_file.is_file():
            raise UpdateError(f"{compose_file} is missing: --checkout must name a Pseudolife-MCP checkout", 2)
        tree = tree_state(repo)
        if tree["dirty"] != "false":
            why = (f"{repo} has {len(tree['lines'])} uncommitted or untracked path(s):" if tree["dirty"] == "true"
                   else f"cannot tell whether {repo} is a clean git checkout: {tree['error']}")
            if not self.o.allow_dirty:
                self.warn(f"REFUSING to deploy: {why}")
                self._warn_tree_lines(tree["lines"])
                raise UpdateError("commit, stash or remove the listed paths, or re-run with --allow-dirty to deploy "
                                  "this tree as it is (stamped dirty, or unknown when git cannot describe it)")
            self.warn(f"--allow-dirty: deploying anyway. {why}")
            self._warn_tree_lines(tree["lines"])
        stamp = {"PSEUDOLIFE_BUILD_GIT_SHA": tree["sha"], "PSEUDOLIFE_BUILD_DIRTY": tree["dirty"],
                 "PSEUDOLIFE_BUILD_TIME": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        env_file = repo / "ops" / ".env"
        example = repo / "ops" / ".env.example"
        if not env_file.is_file() and example.is_file():
            shutil.copyfile(example, env_file)
            self.step("scaffolded ops/.env from ops/.env.example (all values commented)")
        compose = self._compose_args([compose_file, repo / "ops" / "docker-compose.override.yml"], env_file)
        self._compose_shown = compose
        self.backup(repo)
        image_tag = _compose_image_tag(compose_file)
        if not image_tag:
            raise UpdateError(f"could not find the {DAEMON_SERVICE} image tag in {compose_file}")
        code, out = run_cli([self.docker, "image", "inspect", "-f", "{{.Id}}", image_tag], timeout=60)
        tag_id = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else None
        running_id, ref = self.running_image()
        if running_id and ref and ref.split(":")[0] != image_tag.split(":")[0]:
            # The daemon runs an image from another repository (a release
            # pulled from GHCR by `pseudolife-mcp update`): that IS the
            # last-good image, tagged by id; the version-tag guard does not
            # apply, since the two were never the same build.
            self.step(f"the running daemon is {ref}, not a local build: tagging it by id for rollback")
            self.tag_rollback(image_tag, None, running_id, source=running_id)
        else:
            self.tag_rollback(image_tag, running_id, tag_id)
        self.prune_rollbacks(repo, image_tag.split(":")[0])
        self.step("rebuilding the daemon only (Postgres + extractor untouched)...")
        now = tree_state(repo)
        if now["sha"] != tree["sha"] or now["dirty"] != tree["dirty"]:
            raise UpdateError(f"the checkout changed during this deploy (HEAD {tree['sha']} -> {now['sha']}, dirty "
                              f"{tree['dirty']} -> {now['dirty']}); nothing was built. Re-run the deploy")
        self.step(f"build stamp: commit {tree['sha']}, dirty={tree['dirty']}")
        code, out = run_cli([self.docker, "compose", *compose, "up", "-d", "--no-deps", "--build", DAEMON_SERVICE],
                            timeout=7200, env=compose_environment(env_file, stamp), stream=not self.o.as_json)
        if code != 0:
            raise UpdateError("daemon rebuild failed" + (f" ({_last_line(out)})" if out.strip() else ""))
        health = self.wait_health()
        if self.o.all_clients:
            self.clients(repo, str(repo), health)
        self.prune_cache(repo)

    # -- docker: release mode ----------------------------------------------
    def deploy_release(self) -> None:
        target = self.target_version()
        self.report.target = target
        current = self.current_version("docker")
        self.report.current = current
        if self.o.clients_only:
            self.clients(None, f"pseudolife-mcp=={target}", self.daemon_health())
            return
        self.refuse_downgrade(current, target)
        if current == target and not self.o.reinstall:
            self.step(f"the daemon already runs {target}; nothing to do (--reinstall to recreate it anyway, "
                      f"--clients-only for the shim and plugin)")
            return
        context = self.compose_context()
        compose = context["args"]
        self._compose_shown = compose
        image = f"{GHCR_IMAGE}:{target}"
        self.step(f"pulling {image}...")
        code, out = run_cli([self.docker, "pull", image], timeout=7200, stream=not self.o.as_json)
        if code != 0:
            raise UpdateError(f"docker pull {image} failed" + (f" ({_last_line(out)})" if out.strip() else "")
                              + f"; nothing was changed. Is {target} a published release?")
        self.backup(context["checkout"])
        running_id, ref = self.running_image()
        if not running_id:
            raise UpdateError(f"the {DAEMON_CONTAINER} container vanished during the backup; nothing was deployed")
        # The rollback tag lives in the GHCR repository: the overlay picks the
        # daemon's image as ghcr.io/...:${PSEUDOLIFE_IMAGE_TAG}, so a rollback
        # is that variable naming the tag, whatever repository the previous
        # image (a local build, say) came from. Tagged by id: the container's
        # image IS the deployed one, whatever its tag points at by now.
        suffix = self.o.rollback_suffix or f"pre-update-{_stamp()}"
        rollback_tag = f"{current or 'previous'}-{suffix}"
        rollback = f"{GHCR_IMAGE}:{rollback_tag}"
        shown = self._compose_display()
        lines = [f"      PSEUDOLIFE_IMAGE_TAG={rollback_tag} docker compose {shown} up -d --no-deps {DAEMON_SERVICE}",
                 f"      (PowerShell: $env:PSEUDOLIFE_IMAGE_TAG='{rollback_tag}'; docker compose {shown} up -d --no-deps {DAEMON_SERVICE})"]
        self.tag_rollback(ref or GHCR_IMAGE, None, running_id, source=running_id, rollback=rollback,
                          rollback_lines=lines)
        self.prune_rollbacks(context["checkout"], GHCR_IMAGE)
        self.step(f"recreating the daemon container on {image} (Postgres + extractor untouched)...")
        code, out = run_cli([self.docker, "compose", *compose, "up", "-d", "--no-deps", DAEMON_SERVICE],
                            timeout=3600, env=compose_environment(context["env_file"], {"PSEUDOLIFE_IMAGE_TAG": target}),
                            stream=not self.o.as_json)
        if code != 0:
            raise UpdateError("daemon recreate failed" + (f" ({_last_line(out)})" if out.strip() else "")
                              + "\nto roll back:\n" + "\n".join(self.rollback_lines))
        health = self.wait_health(expect_version=target)
        if not self.o.daemon_only:
            self.clients(None, f"pseudolife-mcp=={target}", health)
        # No cache prune: nothing was built.

    def compose_context(self) -> dict:
        """The compose files and env file the running daemon container was
        created with (its labels), with the GHCR overlay added; when those
        files are gone, this package's bundled copies of the two compose
        files, written under the data dir. ``checkout`` is the compose
        project's working directory when it still is a checkout."""
        code, out = run_cli([self.docker, "inspect", "-f", "{{json .Config.Labels}}", DAEMON_CONTAINER], timeout=60)
        labels: dict = {}
        if code == 0:
            try:
                labels = json.loads(out.strip().splitlines()[-1]) or {}
            except (ValueError, IndexError):
                labels = {}
        config_files = [Path(p) for p in (labels.get("com.docker.compose.project.config_files") or "").split(",") if p]
        working_dir = labels.get("com.docker.compose.project.working_dir")
        project = labels.get("com.docker.compose.project") or "pseudolife-mcp"
        # The env file: --env-file, else the labelled one(s) (comma-joined
        # when several were given), else compose's implicit <working dir>/.env.
        # It carries the Postgres password, the volume names and the daemon's
        # bearer; recreating the daemon without it would interpolate defaults.
        labelled = [Path(p) for p in (labels.get("com.docker.compose.project.environment_file") or "").split(",") if p]
        env_files: list[Path] = []
        if self.o.env_file is not None:
            env_files = [Path(self.o.env_file)]
        elif labelled:
            env_files = [p for p in labelled if p.is_file()]
        elif working_dir and (Path(working_dir) / ".env").is_file():
            env_files = [Path(working_dir) / ".env"]
        if (labelled or self.o.env_file) and not env_files:
            raise UpdateError(f"the daemon's env file(s) {', '.join(map(str, labelled or [self.o.env_file]))} no "
                              f"longer exist; recreating the container without them would reset the Postgres "
                              f"password, the volume names and the bearer. Name the file with --env-file. "
                              f"Nothing was changed", 2)
        for path in env_files:
            if not path.is_file():
                raise UpdateError(f"--env-file {path} does not exist; nothing was changed", 2)
        existing = [p for p in config_files if p.is_file()]
        checkout = None
        if working_dir and (Path(working_dir).parent / "pyproject.toml").is_file() and (Path(working_dir).parent / "ops").is_dir():
            checkout = Path(working_dir).parent
        elif working_dir and (Path(working_dir) / "pyproject.toml").is_file():
            checkout = Path(working_dir)
        if not existing:
            base = self._bundled("docker-compose.yml")
            self.step(f"the daemon's compose files are gone; using the bundled {base}")
            existing = [base]
        overlay = next((p.with_name("docker-compose.ghcr.yml") for p in existing
                        if p.with_name("docker-compose.ghcr.yml").is_file()), None) or self._bundled("docker-compose.ghcr.yml")
        files = existing + ([overlay] if overlay not in existing else [])
        args = ["-p", project] + self._compose_args(files, env_files)
        return {"args": args, "env_file": env_files[0] if env_files else None, "checkout": checkout,
                "files": [str(p) for p in files], "project": project}

    def _bundled(self, name: str) -> Path:
        target = data_dir() / "compose" / name
        source = BUNDLED_COMPOSE / name
        if not source.is_file():
            raise UpdateError(f"this package has no bundled {name} and the daemon's compose files are gone; "
                              f"clone the repository and run ops/update.* from it", 2)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            shutil.copyfile(source, target)
        return target

    # -- small helpers ------------------------------------------------------
    _compose_shown: list = []

    def _compose_args(self, files, env_file) -> list[str]:
        args: list[str] = []
        env_files = env_file if isinstance(env_file, list) else ([env_file] if env_file else [])
        for path in env_files:
            if path is not None and Path(path).is_file():
                args += ["--env-file", str(path)]
        for path in files:
            if Path(path).is_file():
                args += ["-f", str(path)]
        return args

    def _compose_display(self) -> str:
        return " ".join(f'"{a}"' if " " in a else a for a in self._compose_shown)

    def _checkout_script(self, checkout: Path | None, stem: str) -> Path | None:
        if checkout is None:
            return None
        preferred = ".ps1" if os.name == "nt" else ".sh"
        for suffix in (preferred, ".sh" if preferred == ".ps1" else ".ps1"):
            candidate = Path(checkout) / "ops" / f"{stem}{suffix}"
            if candidate.is_file() and (suffix == ".ps1" and which("pwsh") or suffix == ".sh" and which("bash")):
                return candidate
        return None

    def _script_argv(self, script: Path) -> list[str]:
        if script.suffix == ".ps1":
            return [which("pwsh") or "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script)]
        return [which("bash") or "bash", str(script)]

    def _warn_tree_lines(self, lines: list[str]) -> None:
        for line in lines[:20]:
            self.warn(f"    {line}")
        if len(lines) > 20:
            self.warn(f"    ... and {len(lines) - 20} more")


def _compose_image_tag(compose_file: Path) -> str | None:
    """The daemon's ``image:`` line (``pseudolife-daemon:<version>``): the
    single source of truth the deploy reads, so it never drifts."""
    try:
        for line in compose_file.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^\s*image:\s*(pseudolife-daemon:\S+)", line)
            if match:
                return match.group(1)
    except OSError:
        return None
    return None


def _rotate_backups(out_dir: Path, *, keep_days: int, keep: set) -> list[str]:
    """Remove this tool's own artifacts (``pseudolife_memory-<stamp>.sql.gz``,
    ``pseudolife_state-<stamp>.tgz``) older than ``keep_days`` in
    ``out_dir``; anything else there is left alone."""
    removed = []
    cutoff = time.time() - keep_days * 86400
    for path in sorted(out_dir.iterdir()):
        if path.name in keep or not re.fullmatch(r"pseudolife_(memory-\d{8}-\d{6}\.sql\.gz|state-\d{8}-\d{6}\.tgz)", path.name):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(str(path))
        except OSError:
            continue
    return removed


def _dump_complete(path: Path) -> bool:
    """Whether a plain-format gzip dump ends with pg_dump's closing marker,
    read outside COPY data (a stored memory quoting the marker must not
    pass for the end of the dump)."""
    import gzip
    complete, in_copy = False, False
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if in_copy:
                    if line == "\\.":
                        in_copy = False
                elif re.match(r"^COPY \S+ .*FROM stdin;$", line):
                    in_copy = True
                elif line == f"-- {_DUMP_MARKER}":
                    complete = True
    except (OSError, EOFError, ValueError):
        return False
    return complete


def _last_line(out: str) -> str:
    lines = [line.strip() for line in (out or "").strip().splitlines() if line.strip()]
    for line in reversed(lines):
        if line.upper().startswith("ERROR"):
            return line
    return lines[-1] if lines else ""


# ── command line ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pseudolife-mcp update",
                                     description="update the daemon and the client side (shim, plugin cache, Codex hooks)")
    parser.add_argument("--tag", default=None, help="release version to install (default: the newest on PyPI)")
    parser.add_argument("--check", action="store_true",
                        help="report whether a newer release exists: exit 0 when one does, 3 when current")
    parser.add_argument("--checkout", default=None,
                        help="rebuild the daemon from this checkout instead of pulling a release (ops/update.* pass this)")
    parser.add_argument("--rollback-tag", default="", help="suffix of the rollback image tag (default: pre-update-<stamp>)")
    parser.add_argument("--no-backup", action="store_true", help="skip the bank backup (NOT recommended)")
    parser.add_argument("--keep-rollbacks", type=int, default=2, help="rollback tags to retain (default 2)")
    parser.add_argument("--keep-cache-hours", type=int, default=168, help="build cache to retain, hours (default 168)")
    parser.add_argument("--no-cache-prune", action="store_true", help="skip build-cache retention")
    parser.add_argument("--force-rollback-tag", action="store_true",
                        help="tag the rollback even when the version tag is not the running daemon's image")
    parser.add_argument("--allow-dirty", action="store_true", help="checkout mode: deploy an uncommitted tree (stamped dirty)")
    parser.add_argument("--health-retries", type=int, default=30)
    parser.add_argument("--health-delay-ms", type=int, default=1500)
    parser.add_argument("--all", action="store_true", help="checkout mode: also move the client side (shim, plugin, Codex)")
    parser.add_argument("--clients-only", action="store_true", help="only the client side; the daemon is left as it is")
    parser.add_argument("--daemon-only", action="store_true", help="only the daemon; the client side is left as it is")
    parser.add_argument("--reinstall", action="store_true", help="release mode: recreate the daemon even at the same version")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="release mode: allow a --tag older than the version the daemon runs")
    parser.add_argument("--env-file", default=None,
                        help="release mode: the compose env file, when the one the container was created with is gone")
    parser.add_argument("--daemon-url", default=os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL") or DEFAULT_DAEMON_URL)
    parser.add_argument("--json", action="store_true", help="one JSON report instead of the step lines")
    return parser


def options_from_args(args) -> Options:
    return Options(checkout=Path(args.checkout) if args.checkout else None, tag=args.tag,
                   rollback_suffix=args.rollback_tag, no_backup=args.no_backup, keep_rollbacks=args.keep_rollbacks,
                   keep_cache_hours=args.keep_cache_hours, no_cache_prune=args.no_cache_prune,
                   force_rollback_tag=args.force_rollback_tag, allow_dirty=args.allow_dirty,
                   health_retries=args.health_retries, health_delay_ms=args.health_delay_ms,
                   all_clients=args.all, clients_only=args.clients_only, daemon_only=args.daemon_only,
                   reinstall=args.reinstall, allow_downgrade=args.allow_downgrade,
                   env_file=Path(args.env_file) if args.env_file else None,
                   check=args.check, as_json=args.json, daemon_url=args.daemon_url)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.clients_only and args.daemon_only:
        print("--clients-only and --daemon-only exclude each other", file=sys.stderr)
        return 2
    return Update(options_from_args(args)).run()


if __name__ == "__main__":
    sys.exit(main())
