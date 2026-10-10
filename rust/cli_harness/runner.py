"""Command line: run rows live, record goldens, compare against goldens, or
run the mutant control. Exit 0 only when every selected case matches (or,
for ``--mutants``, when every mutant is caught)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import compare, core, normalize, producers, rows

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
GOLDENS = HERE / "goldens"
# The oracle commit of a recording from a tree that is not a git checkout.
COMMIT_ENV = "CLI_HARNESS_ORACLE_COMMIT"
ORACLE_PACKAGE = "pseudolife_memory"


def _default_candidate() -> Path:
    name = "pseudolife-stdio.exe" if core.WINDOWS else "pseudolife-stdio"
    import os
    target = Path(os.environ.get("CARGO_TARGET_DIR", REPO / "rust" / "target"))
    return target / "debug" / name


def _golden_path(row: str) -> Path:
    return GOLDENS / f"{row}.{core.PLATFORM}.json"


def _select(loaded: list[core.Case], wanted: list[str], bank: bool = True,
            replay: bool = False) -> list[core.Case]:
    selected = [c for c in loaded if c.runs_here() and (bank or not c.bank)
                and (c.golden or not replay)]
    if wanted:
        selected = [c for c in selected if c.id in wanted]
        missing = set(wanted) - {c.id for c in selected}
        if missing:
            raise SystemExit(f"unknown or skipped case(s): {sorted(missing)}")
    return selected


def _label(case: core.Case) -> str:
    """A case's id, naming any real host program it is allowed to run."""
    if not case.real_programs:
        return case.id
    return f"{case.id}  [real programs: {', '.join(case.real_programs)}]"


def run_row(row: str, cases: list[core.Case], oracle: core.Target | None,
            candidate: core.Target, golden: dict | None, verbose: bool) -> dict:
    results = {}
    for case in cases:
        started = time.monotonic()
        if golden is not None:
            want = golden["cases"].get(case.id)
            if want is None:
                results[case.id] = {"status": "error", "detail": ["no golden recorded"]}
                continue
            got = core.run_arm(case, candidate, core._home_root() / "h")
            diffs = compare.diff(want, got, case.rules)
        else:
            want, got = core.run_case(case, oracle, candidate)
            diffs = compare.diff(want, got, case.rules)
        status = "match" if not diffs else "DIFF"
        results[case.id] = {"status": status, "detail": diffs,
                            "seconds": round(time.monotonic() - started, 2)}
        if case.real_programs:
            results[case.id]["real_programs"] = list(case.real_programs)
        print(f"  {status:5} {_label(case)}", flush=True)
        if diffs and verbose:
            for line in diffs:
                print(f"        {line}")
    return results


def _host_paths(source: Path, python: str) -> list[tuple[bytes, bytes]]:
    """Host paths a recorded oracle stream may name: its source checkout,
    its interpreter's installation and the real home they usually sit
    under. Each becomes a token in a golden, in raw and JSON-escaped
    spellings, longest first. Only streams a rule replaces can hold them
    (a Python traceback the candidate defers instead of printing), so the
    tokens change no comparison; they keep host identifiers out of goldens."""
    import json as _json  # noqa: PLC0415
    exe = Path(python).resolve()
    paths = {str(source): "{ORACLE}", str(exe.parent.parent): "{PYTHON}",
             str(exe.parent): "{PYTHON}", sys.prefix: "{PYTHON}",
             sys.base_prefix: "{PYTHON}", str(Path.home()): "{USER_HOME}"}
    forms: dict[bytes, bytes] = {}
    for path, token in paths.items():
        if len(path) < 4:
            continue
        for spelling in (path, _json.dumps(path)[1:-1]):
            forms.setdefault(spelling.encode(), token.encode())
    return sorted(forms.items(), key=lambda item: len(item[0]), reverse=True)


def _scrub(normal: dict, forms: list[tuple[bytes, bytes]]) -> None:
    import base64  # noqa: PLC0415
    for field in ("stdout", "stderr"):
        data = base64.b64decode(normal[field])
        for old, new in forms:
            data = data.replace(old, new)
        normal[field] = base64.b64encode(data).decode()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _tree_differences(source: Path, commit: str, repo: Path) -> list[str] | None:
    """How ``source``'s oracle package differs from ``commit``'s, by git blob
    id (a CRLF checkout of an LF blob counts as equal); ``None`` when ``repo``
    is not a git checkout that could answer."""
    if _git(repo, "rev-parse", "--git-dir").returncode != 0:
        return None
    listing = _git(repo, "ls-tree", "-r", "-z", commit, "--", ORACLE_PACKAGE)
    if listing.returncode != 0:
        return [f"commit {commit} is not in {repo}"]
    expected = {}
    for entry in listing.stdout.split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            expected[path.decode()] = meta.split()[2].decode()
    if not expected:
        return [f"commit {commit} has no {ORACLE_PACKAGE}/"]
    found = {p.relative_to(source).as_posix(): p
             for p in (source / ORACLE_PACKAGE).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts}
    problems = [f"missing {rel}" for rel in sorted(set(expected) - set(found))]
    problems += [f"not in the commit: {rel}" for rel in sorted(set(found) - set(expected))]
    for rel in sorted(set(expected) & set(found)):
        data = found[rel].read_bytes()
        if expected[rel] not in (_blob_sha(data), _blob_sha(data.replace(b"\r\n", b"\n"))):
            problems.append(f"changed {rel}")
    return problems


def oracle_binding(source: Path, declared: str | None, repo: Path = REPO) -> dict:
    """The commit a recording's oracle ran from, or a refusal (SystemExit).

    A git checkout binds to its HEAD, refusing uncommitted changes to the
    oracle package. Any other tree (an export) needs the commit declared by
    ``--oracle-commit`` or ``CLI_HARNESS_ORACLE_COMMIT``: when the harness's
    own checkout holds that commit, the tree's package must match it file
    for file (``verified-tree``); with no git checkout to ask, the commit is
    recorded as ``declared``."""
    declared = declared or os.environ.get(COMMIT_ENV) or None
    top = _git(source, "rev-parse", "--show-toplevel")
    if top.returncode == 0 and Path(top.stdout.decode().strip()).resolve() == source.resolve():
        head = _git(source, "rev-parse", "HEAD").stdout.decode().strip()
        if declared and declared != head:
            raise SystemExit(f"--record: the declared oracle commit {declared} is not "
                             f"{source}'s HEAD {head}")
        # Anything but HEAD's tracked content can be imported: a modified
        # file, an untracked shadow module, or an ignored one (a sourceless
        # .pyc beside the sources). Only __pycache__ is allowed, whose
        # bytecode Python uses only for a source that exists.
        status = _git(source, "status", "--porcelain", "--untracked-files=all", "--ignored",
                      "--", ORACLE_PACKAGE).stdout.decode("utf-8", "replace")
        stray = [line for line in status.splitlines()
                 if not (line.startswith("!! ") and "/__pycache__/" in line)]
        if stray:
            raise SystemExit(f"--record: {source} has content under {ORACLE_PACKAGE}/ that "
                             f"is not HEAD's ({'; '.join(stray[:5])}), so its HEAD does not "
                             "name the oracle; commit or remove it first")
        return {"oracle_commit": head, "oracle_commit_source": "git"}
    if not declared:
        raise SystemExit(f"--record: {source} is not a git checkout, so the oracle commit is "
                         f"unbound; pass --oracle-commit or set {COMMIT_ENV} to the full commit "
                         "the tree was exported from")
    if not re.fullmatch(r"[0-9a-f]{40}", declared):
        raise SystemExit(f"--record: the declared oracle commit must be a full 40-character "
                         f"commit id, not {declared!r}")
    problems = _tree_differences(source, declared, repo)
    if problems is None:
        return {"oracle_commit": declared, "oracle_commit_source": "declared"}
    if problems:
        raise SystemExit(f"--record: {source} is not commit {declared}'s {ORACLE_PACKAGE}/: "
                         + "; ".join(problems[:10]))
    return {"oracle_commit": declared, "oracle_commit_source": "verified-tree"}


def record(row: str, cases: list[core.Case], oracle: core.Target, source: Path,
           binding: dict) -> Path:
    golden = {"row": rows.ROWS[row], "platform": core.PLATFORM, **binding, "cases": {}}
    forms = _host_paths(source, oracle.command[0])
    for case in cases:
        obs = core.run_arm(case, oracle, core._home_root() / "h")
        normal = normalize.apply(obs, (), obs["home"])
        normal["home"] = None
        normal.pop("daemon_url", None)
        _scrub(normal, forms)
        golden["cases"][case.id] = normal
        print(f"  recorded {_label(case)} (exit {obs['exit']})", flush=True)
    path = _golden_path(row)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(golden, indent=1, ensure_ascii=True) + "\n", encoding="utf-8",
                    newline="\n")
    return path


def _write_summary(out: Path | None, summary: dict) -> None:
    """Replace ``out`` with ``summary`` in one step, so neither a reader nor
    a crash mid-write leaves a partial file."""
    if out is None:
        return
    partial = out.with_name(out.name + ".partial")
    partial.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    os.replace(partial, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli_harness",
                                     description="Python CLI vs Rust CLI differential harness")
    parser.add_argument("--row", action="append", choices=sorted(rows.ROWS), required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--oracle-python", default=sys.executable)
    parser.add_argument("--oracle-source", type=Path, default=REPO)
    parser.add_argument("--candidate", type=Path, default=None)
    parser.add_argument("--oracle-commit",
                        help=f"the full commit an exported oracle tree (no .git) came from; "
                             f"also {COMMIT_ENV}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="write goldens from the oracle")
    mode.add_argument("--golden", action="store_true", help="compare against goldens")
    mode.add_argument("--mutants", action="store_true", help="run the mutant control")
    parser.add_argument("--mutant", action="append", default=[], help="only these mutants")
    parser.add_argument("--skip-bank", action="store_true",
                        help="skip cases that need real daemons on disposable banks")
    parser.add_argument("--out", type=Path, help="write a JSON summary")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    producers.use_oracle(args.oracle_source.resolve())
    from .rows import _daemon  # noqa: PLC0415
    _daemon.ORACLE.update(python=args.oracle_python, source=args.oracle_source.resolve())
    oracle = core.python_target(args.oracle_python, args.oracle_source.resolve())
    candidate_path = args.candidate or _default_candidate()

    if args.mutants:
        from . import mutants  # noqa: PLC0415
        mutants.SKIP_BANK = args.skip_bank
        return mutants.main(args.row, args.mutant, oracle, args.verbose)

    # Refuse an unbound recording before any arm runs.
    binding = (oracle_binding(args.oracle_source.resolve(), args.oracle_commit)
               if args.record else None)
    # Keyed by row name: two rows may share a PARITY ID (invite and pair).
    # Written after every row, so a later row's crash keeps the earlier ones;
    # ``complete`` turns true only once every selected row has finished.
    summary: dict = {"platform": core.PLATFORM, "complete": False, "rows": {}}
    failed = 0
    for row in args.row:
        loaded = rows.load(row)
        cases = _select(loaded, args.case,
                        bank=not (args.skip_bank or args.golden or args.record),
                        replay=args.golden or args.record)
        print(f"{rows.ROWS[row]} ({len(cases)} cases, {core.PLATFORM})", flush=True)
        if args.record:
            path = record(row, cases, oracle, args.oracle_source.resolve(), binding)
            print(f"  wrote {path}")
            chosen = {c.id for c in cases}
            summary["rows"][row] = {"parity": rows.ROWS[row], **binding,
                                    "recorded": len(cases),
                                    "skipped": [c.id for c in loaded if c.id not in chosen]}
            _write_summary(args.out, summary)
            continue
        if not candidate_path.is_file():
            raise SystemExit(f"candidate binary not found: {candidate_path}")
        golden = (json.loads(_golden_path(row).read_text(encoding="utf-8"))
                  if args.golden else None)
        results = run_row(row, cases, oracle, core.rust_target(candidate_path), golden,
                          args.verbose)
        bad = [k for k, v in results.items() if v["status"] != "match"]
        failed += len(bad)
        summary["rows"][row] = {"parity": rows.ROWS[row], "cases": len(results), "diffs": bad,
                                "results": results}
        print(f"  {len(results) - len(bad)}/{len(results)} match", flush=True)
        _write_summary(args.out, summary)
    summary["complete"] = True
    _write_summary(args.out, summary)
    return 1 if failed else 0
