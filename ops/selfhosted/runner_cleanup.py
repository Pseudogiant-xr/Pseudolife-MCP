"""Keep a parity runner machine's disk use bounded: one pass, logged.

The self-hosted parity lane (docs/specs/2026-10-19-self-hosted-parity-lane.md,
"Disk use") runs persistent runners, so whatever a job leaves behind stays.
This removes what is rebuilt on demand, never anything else, and runs as the
runner account itself, never root: every path it touches is the account's
own, so a link a job planted cannot redirect a deletion at someone else's
files. Two passes:

- ``--runner DIR --home DIR`` runs from the job-started hook, before the
  job's first step: nothing of that runner's is running yet. It trims the
  runner's checkout and diagnostic logs and the caches in its HOME, each
  against a cap. The runner has already made the job's temp directory and
  downloaded its actions by then, so those are never touched.
- ``--shared`` runs from a timer and trims what no single runner owns: the
  account's rootless Docker (sparing the pinned images), its leftovers in
  the system temp directory, and uv's cache.

A cache over its cap is removed whole (pip, Hugging Face, cargo, rustup and
uv rebuild what the next job needs). With less than ``--floor-gb`` free on
the work drive every cap counts as zero. Each pass appends one JSON line to
``--log`` (rotated at 1 MB) and prints a one-line summary; a failure to
remove something is logged and never fails the job.

Standard library only.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GB = 1024 ** 3
DAY = 86400
LOG_LIMIT = 1024 * 1024

# Caps for one runner, sized so three runners and the shared caches stay
# under about 50 GB on a 200 GB dev box whose suite user holds about 90 GB
# (measured 2026-10-28: a checkout 0.8-1.1 GB, pip 0.5 GB, rustup 0.6 GB,
# Hugging Face 1.2 GB on one runner).
HOME_CACHES = {
    ".cache/pip": 2,
    ".cache/huggingface": 3,
    ".cache/uv": 3,
    ".cargo/registry": 2,
    ".cargo/git": 1,
    ".rustup": 2,
}
WORK_CAP_GB = 4
DIAG_DAYS = 14
TEMP_DAYS = 1
# Rootless Docker: the w1c-connection shard builds pseudolife-pg:18 on every
# run, so the build cache only needs its last few layers.
PINNED_IMAGES = ("pseudolife-pg:18",)
IMAGE_DAYS = 7
BUILD_CACHE_GB = 3
FLOOR_GB = 25


def size(path: Path) -> int:
    """Bytes under path, never following a link."""
    try:
        info = path.lstat()
    except OSError:
        return 0
    if not stat.S_ISDIR(info.st_mode):
        return info.st_size
    total = 0
    stack = [path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def age_days(path: Path, now: float) -> float:
    try:
        return (now - path.lstat().st_mtime) / DAY
    except OSError:
        return 0.0


class Pass:
    def __init__(self, mode: str, disk: Path, floor_gb: float, dry_run: bool):
        self.mode, self.disk, self.dry_run = mode, disk, dry_run
        self.free_before = shutil.disk_usage(disk).free
        self.aggressive = self.free_before < floor_gb * GB
        self.removed: list[dict] = []
        self.errors: list[str] = []

    def cap(self, gb: float) -> float:
        return 0 if self.aggressive else gb * GB

    def remove(self, path: Path, why: str, within: Path) -> None:
        """Remove path (a link itself, never its target) if it lies under within."""
        try:
            parent = path.parent.resolve(strict=True)
            if parent != within.resolve() and within.resolve() not in parent.parents:
                raise OSError(f"{path} is not under {within}")
            info = path.lstat()
        except OSError as error:
            if not isinstance(error, FileNotFoundError):
                self.errors.append(f"{path}: {error}")
            return
        freed = size(path)
        if not self.dry_run:
            try:
                if stat.S_ISDIR(info.st_mode):
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as error:
                self.errors.append(f"{path}: {error}")
                freed -= size(path)
        self.removed.append({"path": str(path), "bytes": freed, "why": why})

    def report(self, log: Path | None, **extra) -> str:
        freed = sum(item["bytes"] for item in self.removed)
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": self.mode,
                  "dry_run": self.dry_run, "aggressive": self.aggressive,
                  "free_gb_before": round(self.free_before / GB, 1),
                  "free_gb_after": round(shutil.disk_usage(self.disk).free / GB, 1),
                  "freed_gb": round(freed / GB, 2), "removed": self.removed,
                  "errors": self.errors, **extra}
        if log is not None:
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                if log.exists() and log.stat().st_size > LOG_LIMIT:
                    os.replace(log, log.with_name(log.name + ".1"))
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
            except OSError as error:
                self.errors.append(f"log {log}: {error}")
        verb = "would free" if self.dry_run else "freed"
        mode = " (below the free-disk floor: every cap is zero)" if self.aggressive else ""
        return (f"runner_cleanup {self.mode}: {verb} {record['freed_gb']} GB in "
                f"{len(self.removed)} paths{mode}; {record['free_gb_after']} GB free; "
                f"{len(self.errors)} errors")


def runner_pass(runner: Path, home: Path, cleanup: Pass, now: float) -> None:
    work = runner / "_work"
    if size(work) > cleanup.cap(WORK_CAP_GB):
        # Only what is inside each checkout (_work/<repo>/<repo>): the runner
        # made that directory for this job and its first step runs there, and
        # the names starting with "_" (the job's temp directory, the actions it
        # downloaded, the workspace mapping) belong to the job already.
        for repo in sorted(work.iterdir()) if work.is_dir() else []:
            if repo.name.startswith("_") or not repo.is_dir() or repo.is_symlink():
                continue
            for checkout in sorted(repo.iterdir()):
                if checkout.is_dir() and not checkout.is_symlink():
                    for entry in sorted(checkout.iterdir()):
                        cleanup.remove(entry, "work directory over its cap", checkout)
    diag = runner / "_diag"
    for entry in sorted(diag.rglob("*")) if diag.is_dir() else []:
        if entry.is_file() and age_days(entry, now) > DIAG_DAYS:
            cleanup.remove(entry, f"diagnostic log older than {DIAG_DAYS} days", diag)
    for relative, gb in HOME_CACHES.items():
        path = home / relative
        if size(path) > cleanup.cap(gb):
            cleanup.remove(path, f"cache over its {gb} GB cap", home)


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=600)


def docker_pass(cleanup: Pass, now: float) -> dict:
    if shutil.which("docker") is None:
        return {"docker": "absent"}
    before = docker("system", "df", "--format", "{{.Type}} {{.Size}}")
    if before.returncode:
        cleanup.errors.append("docker system df: " + before.stderr.strip()[-300:])
        return {}
    steps = [("container", "prune", "--force", "--filter", "until=24h")]
    listing = docker("image", "ls", "--format", "{{.ID}} {{.Repository}}:{{.Tag}} {{.CreatedAt}}")
    pinned_ids = set()
    old = []
    for line in listing.stdout.splitlines():
        image_id, name, created = (line.split(" ", 2) + ["", ""])[:3]
        if name in PINNED_IMAGES:
            pinned_ids.add(image_id)
            continue
        try:
            made = calendar.timegm(time.strptime(created[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            made = now
        if cleanup.aggressive or (now - made) / DAY > IMAGE_DAYS:
            old.append(image_id)
    for image_id in sorted(set(old) - pinned_ids):
        steps.append(("image", "rm", image_id))
    steps.append(("builder", "prune", "--all", "--force", "--filter", f"until={IMAGE_DAYS * 24}h"))
    # --all is load-bearing: without it builder prune removes nothing
    # (ops/prune-build-cache.sh, 2026-08-06).
    steps.append(("builder", "prune", "--all", "--force", "--max-used-space",
                  str(int(cleanup.cap(BUILD_CACHE_GB)))))
    ran = []
    for step in steps:
        if cleanup.dry_run:
            ran.append(" ".join(step))
            continue
        result = docker(*step)
        # An image a container still uses is refused, which is the point.
        if result.returncode and not (step[0] == "image" and "conflict" in result.stderr):
            cleanup.errors.append(f"docker {' '.join(step)}: {result.stderr.strip()[-300:]}")
        ran.append(" ".join(step))
    after = docker("system", "df", "--format", "{{.Type}} {{.Size}}")
    return {"docker_before": before.stdout.split("\n"), "docker_after": after.stdout.split("\n"),
            "docker_steps": ran}


def shared_pass(cleanup: Pass, now: float, temp: Path) -> dict:
    extra = docker_pass(cleanup, now)
    # The system temp directory is shared, so only the account's own entries;
    # on Windows each account has its own temp directory and no owner check.
    mine = os.getuid() if hasattr(os, "getuid") else None
    for entry in sorted(temp.iterdir()) if temp.is_dir() else []:
        try:
            owner = entry.lstat().st_uid
        except OSError:
            continue
        if mine is not None and owner == mine and age_days(entry, now) > TEMP_DAYS:
            cleanup.remove(entry, f"temporary file older than {TEMP_DAYS} day", temp)
    if shutil.which("uv") and not cleanup.dry_run:
        result = subprocess.run(["uv", "cache", "prune"], capture_output=True, text=True, timeout=600)
        if result.returncode:
            cleanup.errors.append("uv cache prune: " + result.stderr.strip()[-300:])
    return extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--runner", type=Path, help="this runner's directory (the job-started pass)")
    mode.add_argument("--shared", action="store_true", help="the timer's pass")
    parser.add_argument("--home", type=Path, help="this runner's HOME (with --runner)")
    parser.add_argument("--temp", type=Path, default=Path(tempfile.gettempdir()),
                        help="the system temp directory (with --shared)")
    parser.add_argument("--disk", type=Path,
                        help="a path on the work drive, whose free space the floor counts "
                             "(default: --runner, else HOME)")
    parser.add_argument("--log", type=Path, help="append one JSON line per pass here")
    parser.add_argument("--floor-gb", type=float, default=FLOOR_GB,
                        help=f"below this much free, every cap is zero (default {FLOOR_GB})")
    parser.add_argument("--dry-run", action="store_true", help="report, remove nothing")
    options = parser.parse_args(argv)
    if options.runner and not options.home:
        parser.error("--runner needs --home")
    now = time.time()
    disk = options.disk or options.runner or Path.home()
    try:
        if options.runner:
            cleanup = Pass("runner " + options.runner.name, disk, options.floor_gb, options.dry_run)
            runner_pass(options.runner, options.home, cleanup, now)
            extra = {}
        else:
            cleanup = Pass("shared", disk, options.floor_gb, options.dry_run)
            extra = shared_pass(cleanup, now, options.temp)
        print(cleanup.report(options.log, **extra))
    except Exception as error:  # noqa: BLE001 - a cleanup never fails the job
        print(f"runner_cleanup: {type(error).__name__}: {error}; nothing more was removed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
