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


def _cache_problems(source: Path) -> list[str]:
    """``__pycache__`` entries that are not a ``<module>.<tag>.pyc`` beside
    an existing ``<module>.py`` (an orphan's bytecode can be imported)."""
    problems = []
    for path in sorted((source / ORACLE_PACKAGE).rglob("*")):
        if "__pycache__" not in path.parts or path.is_dir():
            continue
        rel = path.relative_to(source).as_posix()
        module = path.parent.parent / (path.name.split(".", 1)[0] + ".py")
        if path.parent.name != "__pycache__" or path.suffix != ".pyc" or not module.is_file():
            problems.append(f"cache without its source: {rel}")
    return problems


def oracle_binding(source: Path, declared: str | None, repo: Path = REPO) -> dict:
    """The commit a recording's oracle ran from, or a refusal (SystemExit).

    What binds is the bytes the oracle imports: every file under the oracle
    package as read from disk (not git's index, so an ``assume-unchanged``
    edit counts), compared with the commit's blobs, with no file beyond them
    (an untracked or ignored shadow module, a sourceless ``.pyc``) and no
    ``__pycache__`` entry without its source. In-tree bytecode is never run:
    the oracle arm reads caches only under an empty ``PYTHONPYCACHEPREFIX``
    (``core.python_target``).

    A git checkout (``source/.git`` present) binds to its HEAD, and any git
    command there that fails refuses. Any other tree (an export) needs the
    commit declared by ``--oracle-commit`` or ``CLI_HARNESS_ORACLE_COMMIT``:
    when the harness's own checkout holds that commit, the tree must match it
    file for file (``verified-tree``); with no git checkout to ask, the
    commit is recorded as ``declared``.

    A recording repeats the whole-tree check (``tree_problems``) at its
    start and after every row, before that row's golden is written, so a
    file edited during the run is refused. An edit made and reverted between
    two checks is out of scope: the tree is checked, not snapshotted."""
    declared = declared or os.environ.get(COMMIT_ENV) or None
    if (source / ".git").exists():
        top = _git(source, "rev-parse", "--show-toplevel")
        head = _git(source, "rev-parse", "--verify", "HEAD^{commit}")
        for done in (top, head):
            if done.returncode != 0:
                raise SystemExit(f"--record: git exited {done.returncode} in {source} "
                                 f"({done.stderr.decode('utf-8', 'replace').strip()[:200]}), "
                                 "so the oracle commit is unbound")
        if Path(top.stdout.decode().strip()).resolve() != source.resolve():
            raise SystemExit(f"--record: {source} is not the top of its git checkout")
        head = head.stdout.decode().strip()
        if declared and declared != head:
            raise SystemExit(f"--record: the declared oracle commit {declared} is not "
                             f"{source}'s HEAD {head}")
        problems = _tree_differences(source, head, source)
        if problems is None:
            raise SystemExit(f"--record: git cannot read {source}'s repository, so the "
                             "oracle commit is unbound")
        problems += _cache_problems(source)
        if problems:
            raise SystemExit(f"--record: {source}'s {ORACLE_PACKAGE}/ is not HEAD's "
                             f"({'; '.join(problems[:5])}), so its HEAD does not name the "
                             "oracle; commit or remove it first")
        return {"oracle_commit": head, "oracle_commit_source": "git"}
    if not declared:
        raise SystemExit(f"--record: {source} is not a git checkout, so the oracle commit is "
                         f"unbound; pass --oracle-commit or set {COMMIT_ENV} to the full commit "
                         "the tree was exported from")
    if not re.fullmatch(r"[0-9a-f]{40}", declared):
        raise SystemExit(f"--record: the declared oracle commit must be a full 40-character "
                         f"commit id, not {declared!r}")
    orphans = _cache_problems(source)
    if orphans:
        raise SystemExit(f"--record: {source}'s {ORACLE_PACKAGE}/ has bytecode an import "
                         f"could run: {'; '.join(orphans[:10])}")
    problems = _tree_differences(source, declared, repo)
    if problems is None:
        return {"oracle_commit": declared, "oracle_commit_source": "declared"}
    if problems:
        raise SystemExit(f"--record: {source} is not commit {declared}'s {ORACLE_PACKAGE}/: "
                         + "; ".join(problems[:10]))
    return {"oracle_commit": declared, "oracle_commit_source": "verified-tree"}


def _commit_blobs(repo: Path, commit: str) -> dict[str, str]:
    listing = _git(repo, "ls-tree", "-r", "-z", commit, "--", ORACLE_PACKAGE)
    if listing.returncode != 0:
        raise SystemExit(f"--record: git exited {listing.returncode} listing {commit} in {repo}")
    blobs = {}
    for entry in listing.stdout.split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            blobs[path.decode()] = meta.split()[2].decode()
    return blobs


def tree_problems(source: Path, binding: dict) -> list[str]:
    """``oracle_binding``'s whole-tree check, repeated during a recording:
    bytecode without its source and, where a checkout can answer, any file
    under the oracle package that is not the bound commit's."""
    problems = _cache_problems(source)
    where = {"git": source, "verified-tree": REPO}.get(binding["oracle_commit_source"])
    if where is not None:
        found = _tree_differences(source, binding["oracle_commit"], where)
        problems += [f"git cannot read {where}"] if found is None else found
    return problems


def _binding_blobs(source: Path, binding: dict) -> dict[str, str] | None:
    """The bound commit's blob ids, where a checkout can answer."""
    where = {"git": source, "verified-tree": REPO}.get(binding["oracle_commit_source"])
    return None if where is None else _commit_blobs(where, binding["oracle_commit"])


# Set by isolated_relaunch for the relaunched harness process.
ISOLATION_ENV = "CLI_HARNESS_PYCACHE_PREFIX"


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def isolated_relaunch() -> int | None:
    """Run this harness invocation again, once, with an empty
    ``PYTHONPYCACHEPREFIX`` and ``PYTHONDONTWRITEBYTECODE``: every oracle
    module the harness process imports itself (writers, seeders, observers)
    is then compiled from the source on disk, never from a ``__pycache__``.
    Returns the relaunch's exit code, or ``None`` in the relaunched process."""
    prefix = os.environ.get(ISOLATION_ENV)
    if (prefix and sys.pycache_prefix and _same_path(sys.pycache_prefix, prefix)
            and sys.dont_write_bytecode):
        return None
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    empty = tempfile.mkdtemp(prefix="cli-harness-parent-pycache-")
    try:
        env = dict(os.environ, PYTHONPYCACHEPREFIX=empty, PYTHONDONTWRITEBYTECODE="1")
        env[ISOLATION_ENV] = empty
        return subprocess.run([sys.executable, *sys.orig_argv[1:]], env=env).returncode
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def parent_module_problems(source: Path, blobs: dict[str, str] | None) -> list[str]:
    """Why the oracle code loaded in this (the harness's) process might not be
    the bound source: not isolated from bytecode caches, a module loaded from
    elsewhere or through a cache outside the empty prefix, or (where the
    commit's blobs are known) source bytes on disk that differ from them."""
    prefix = sys.pycache_prefix
    if not (prefix and sys.dont_write_bytecode):
        return ["it may run cached bytecode: start it as `python rust/cli_harness`, which "
                "relaunches itself with an empty PYTHONPYCACHEPREFIX"]
    if any(Path(prefix).rglob("*.pyc")):
        return [f"its bytecode cache prefix {prefix} is not empty"]
    root, problems = source / ORACLE_PACKAGE, []
    for name, module in sorted(sys.modules.items()):
        if name != ORACLE_PACKAGE and not name.startswith(ORACLE_PACKAGE + "."):
            continue
        spec = getattr(module, "__spec__", None)
        origin = getattr(spec, "origin", None)
        if not origin or not getattr(spec, "has_location", False):
            problems.append(f"{name} has no source file")
            continue
        path = Path(origin).resolve()
        if not path.is_relative_to(root):
            problems.append(f"{name} was loaded from {path}")
            continue
        cached = getattr(spec, "cached", None)
        if cached and not Path(cached).resolve().is_relative_to(Path(prefix).resolve()):
            problems.append(f"{name} may have run {cached}")
        if blobs is not None:
            rel = path.relative_to(source).as_posix()
            data = path.read_bytes()
            if blobs.get(rel) not in (_blob_sha(data), _blob_sha(data.replace(b"\r\n", b"\n"))):
                problems.append(f"{name}'s source {rel} is not the bound commit's")
    return problems


def redact_golden(golden: dict) -> dict:
    """Every case of a golden with its SCRAM verifiers replaced by the fixed
    token (``normalize.redact_scram_verifiers``); bindings and metadata kept."""
    for normal in golden["cases"].values():
        normalize.redact_scram_verifiers(normal)
    return golden


def write_golden(path: Path, golden: dict) -> None:
    """Write a golden, refusing one that would still carry a SCRAM verifier."""
    marks = normalize.scram_marks(golden)
    if marks:
        raise SystemExit(f"refusing to write {path}: SCRAM-SHA-256$ remains at "
                         + ", ".join(marks[:5]))
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(golden, indent=1, ensure_ascii=True) + "\n", encoding="utf-8",
                    newline="\n")


def record(row: str, cases: list[core.Case], oracle: core.Target, source: Path,
           binding: dict, verify=None) -> Path:
    """Record ``row``'s cases from the oracle; ``verify`` runs after the last
    arm and before anything is written (it may refuse with SystemExit)."""
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
    if verify is not None:
        verify()
    path = _golden_path(row)
    write_golden(path, redact_golden(golden))
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
    source = args.oracle_source.resolve()

    # Refuse an unbound recording before the oracle is importable here, and
    # before any writer, seeder or observer runs in this process.
    binding = verify = None
    if args.record:
        binding = oracle_binding(source, args.oracle_commit)
        blobs = _binding_blobs(source, binding)

        def verify() -> None:
            # At the start and after every row's arms, before its golden is
            # written: the tree the oracle subprocesses import from, and the
            # oracle modules loaded in this process.
            problems = tree_problems(source, binding)
            if problems:
                raise SystemExit(f"--record: {source}'s {ORACLE_PACKAGE}/ is no longer the "
                                 "bound commit's: " + "; ".join(problems[:5]))
            problems = parent_module_problems(source, blobs)
            if problems:
                raise SystemExit("--record: the harness process's own oracle code is not the "
                                 "bound source: " + "; ".join(problems[:5]))

        verify()

    producers.use_oracle(source)
    from .rows import _daemon  # noqa: PLC0415
    _daemon.ORACLE.update(python=args.oracle_python, source=source)
    oracle = core.python_target(args.oracle_python, source)
    candidate_path = args.candidate or _default_candidate()

    if args.mutants:
        from . import mutants  # noqa: PLC0415
        mutants.SKIP_BANK = args.skip_bank
        return mutants.main(args.row, args.mutant, oracle, args.verbose)
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
            # verify again after the row's arms: its seeders imported oracle
            # modules lazily, and the golden is written only if they are bound.
            path = record(row, cases, oracle, source, binding, verify)
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
