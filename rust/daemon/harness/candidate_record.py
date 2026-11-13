"""Re-recording a daemon harness golden from the Rust candidate.

A golden is a regression snapshot of the Rust daemon once the Python oracle
is frozen: when a Rust change alters behaviour on purpose, its golden is
recorded again from the candidate, through the replay's own path, so that
what is written is exactly what the replay will compare. The runners that
support it (``w2e.py golden --record-candidate``, ``run.py golden
--record-candidate``) share the rules here: a dry run unless ``--write``,
every scenario reported as unchanged, changed (with the replay's own
difference) or new, a refusal for a deferral or an entry that would not
replay against the run it came from, and provenance that replay never reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
# Top-level golden key: where the snapshot came from. Replay reads only the
# recorded steps or responses, so it never compares this.
PROVENANCE_KEY = "recorded_from_candidate"
# What an unported path answers: the daemon's declared 501 body
# (``http.rs``) and the CLI's deferral line.
DEFERRAL_MARKS = ('"not_implemented"', "is deferred in this candidate")
PLATFORM = "windows" if os.name == "nt" else "linux"
# Paths whose change makes the binary differ from the commit: Rust sources
# and the build inputs, not the harness, goldens or docs under rust/.
RUST_SOURCES = (":(glob)rust/**/*.rs", ":(glob)rust/**/Cargo.toml", "rust/Cargo.lock",
                ":(glob)rust/**/build.rs", "rust/rust-toolchain.toml")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)


def provenance(binary: Path) -> dict:
    """This checkout's commit, whether its Rust sources and build inputs
    differ from it (``RUST_SOURCES``), the platform and the binary's digest."""
    head = _git("rev-parse", "--verify", "HEAD^{commit}")
    status = _git("status", "--porcelain", "--", *RUST_SOURCES)
    if head.returncode != 0 or status.returncode != 0:
        tree = "unknown"
    else:
        tree = "modified" if status.stdout.strip() else "clean"
    return {"commit": head.stdout.decode().strip() if head.returncode == 0 else None,
            "rust_tree": tree, "platform": PLATFORM,
            "candidate_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest()}


def deferral(value) -> str | None:
    """The deferral marker an answer carries, if any."""
    text = json.dumps(value, sort_keys=True, default=str)
    return next((mark for mark in DEFERRAL_MARKS if mark in text), None)


def classify(committed: bool, diffs: list[str]) -> str:
    if not committed:
        return "new"
    return "changed" if diffs else "unchanged"


def summary_line(name: str, kind: str, write: bool, refused: str | None) -> str:
    if refused:
        return f"  REFUSED {name}: {refused}; nothing written"
    if kind == "unchanged":
        return f"  unchanged {name}"
    label = "CHANGED" if kind == "changed" else "new"
    tail = "written" if write else "dry run: add --write after reviewing the difference above"
    return f"  {label} {name} ({tail})"


def write_json(path: Path, data: dict, *, newline_at_end: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, sort_keys=True) + ("\n" if newline_at_end else "")
    path.write_text(text, encoding="utf-8", newline="\n")
