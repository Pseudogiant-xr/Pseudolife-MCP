"""Forced real-data run: every live Python-vs-Rust differential on a copy of
the real bank.

It runs inside the mirror runner (``ops/Dockerfile.mirror-runner``) on the
mirror's internal network. Every disposable database copies the seeded,
credential-rewritten bank (``PL_HARNESS_BASE_DATABASE``, from ``seed.py``),
so each scenario runs its own seed and steps on top of real data. Dumps
hold only what differs from that bank (``dbstate``), and an extractor or
judge prompt that no table answers gets the launcher's default
(``PL_HARNESS_STUB_DEFAULT_CONTENT``). Both daemons then see identical
model output, and their request bytes are still compared.

Each scenario runs in its own harness process, so one crash costs one
scenario. A scenario that differs or fails runs twice more:

* again on the real bank, where a clean rerun means ``transient``;
* on the harness's own synthetic bank, where a difference there too means
  ``preexisting``, and a clean run means ``new`` (it differs on real data
  only).

``summary.json`` holds counts and scenario names only. ``private.json``
holds the differences, which quote bank text, and stays on the host.

    python rust/daemon/harness/mirror/forced.py --rust-bin BIN --out DIR [--suites w2e ...] [--only NAME ...]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
ROOT = HARNESS.parents[2]
sys.path.insert(0, str(HERE))
import seed  # noqa: E402

# suite -> (script, directory it imports from, expression naming its scenarios)
SUITES = {
    "w2e": ("w2e.py", HARNESS, "import w2e; names = list(w2e.SCENARIOS)"),
    "w2d_search": ("w2d_search.py", HARNESS, "import w2d_search; names = w2d_search.default_scenarios('live')"),
    "board": ("board/run.py", HARNESS / "board",
              "import run; names = list(run.S.SCENARIOS) + ['mixed']"),
    "routes": ("run.py", HARNESS, "import run; names = list(run.SCENARIOS) + ['refusals']"),
}
# Harnesses this run does not force, and why (reported with the counts).
NOT_FORCED = {
    "dream_cursors": "admits only the bench PostgreSQL at 127.0.0.1:5433 by design; "
                     "the w2e dream, deep-dream and rollback scenarios run instead",
    "cli_harness": "its rows drive a fixture HTTP daemon or build their own synthetic "
                   "databases, not a bank a restore can replace",
    "golden and mutant modes": "they compare against recordings made on synthetic banks",
}
# The summary lines every live harness prints, one per difference: "DIFF
# [scenario]" and "DB DIFF [...]", run.py's refusal rows "DIFF <case>:", and
# the board's "STATE [...]" and "CHAIN [...]". Indented progress lines repeat
# them and are not counted.
DIFF_LINE = re.compile(r"^(?:(?:DB )?DIFF[ \[]|STATE \[|CHAIN \[)")
# Where a difference is: its step or case, and the first state or response path.
LOCATION = re.compile(r": (?:db )?(/[^\s:]+)")
STEP = re.compile(r"\] (?:step |#)?(\d+)")
# What the synthetic arm drops, so it is the harness's own baseline (CI's).
REAL_BANK_ENV = ("PL_HARNESS_BASE_DATABASE", "PL_HARNESS_STUB_DEFAULT_CONTENT")
DISK_FLOOR = 8 * 1024 ** 3


def scenario_names(suite: str) -> list[str]:
    _, directory, expression = SUITES[suite]
    # The script's own directory first, as when it runs: board/run.py, not run.py.
    code = (f"import json, sys; sys.path.insert(0, {str(HARNESS)!r}); "
            f"sys.path.insert(0, {str(directory)!r}); {expression}; print(json.dumps(names))")
    out = subprocess.run([sys.executable, "-c", code], cwd=directory, capture_output=True,
                         text=True, check=True, timeout=300)
    return json.loads(out.stdout.strip().splitlines()[-1])


def outcome(exit_code: int | None, log: str) -> dict:
    """``pass``, ``diff`` or ``error``, read from the harness's own output:
    every live harness prints one ``DIFF [...]`` line per difference and
    exits non-zero on any."""
    diffs = [line for line in log.splitlines() if DIFF_LINE.match(line)]
    if exit_code == 0 and not diffs:
        return {"status": "pass", "diffs": []}
    status = "diff" if diffs else "error"
    tail = [] if diffs else log.strip().splitlines()[-6:]
    return {"status": status, "diffs": diffs, "error_tail": tail}


def signatures(result: dict) -> set:
    """Each difference's (step, path)."""
    out = set()
    for line in result["diffs"]:
        step, where = STEP.search(line), LOCATION.search(line)
        out.add((step.group(1) if step else "", where.group(1) if where else line[:120]))
    return out


def classify(first: dict, retry: dict | None, synthetic: dict | None) -> str:
    """``preexisting`` only when the synthetic run differs in the same place:
    an unrelated synthetic failure does not excuse a real-bank difference."""
    if first["status"] == "pass":
        return "pass"
    if retry is not None and retry["status"] == "pass":
        return "transient"
    if first["status"] == "error" and (retry is None or retry["status"] == "error"):
        return "error"  # no comparison completed: never a pass, never preexisting
    if synthetic is not None and synthetic["status"] == "diff" \
            and signatures(first) & signatures(synthetic):
        return "preexisting"
    return "new"


class Runner:
    """One harness process per scenario; tests replace ``invoke``."""

    def __init__(self, rust_bin: Path, out: Path, timeout: float, stop: Path, sweep=None):
        self.rust_bin, self.out, self.timeout, self.stop = rust_bin, out, timeout, stop
        self.sweep = sweep or (lambda: 0)

    def invoke(self, suite: str, name: str, label: str, base: bool) -> dict:
        script = HARNESS / SUITES[suite][0]
        work = self.out / suite / label
        work.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
        env = dict(os.environ, PYTHONUNBUFFERED="1",
                   PL_HARNESS_SCRATCH=str(Path("/tmp") / "forced" / suite / safe))
        if not base:
            for key in REAL_BANK_ENV:
                env.pop(key, None)
        started = time.monotonic()
        # Its own process group: a timeout stops the daemons it started too.
        proc = subprocess.Popen([sys.executable, str(script), "live", "--rust-bin", str(self.rust_bin),
                                 "--only", name, "--out", str(work / f"{safe}.json")],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            raw, _ = proc.communicate(timeout=self.timeout)
            code, log = proc.returncode, raw.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            code, log = None, self._stop(proc) + f"\nTIMEOUT after {self.timeout:g}s"
            swept = self.sweep()
            log += f"\nswept {swept} leftover disposable databases"
        shutil.rmtree(env["PL_HARNESS_SCRATCH"], ignore_errors=True)
        (work / f"{safe}.log").write_text(log, encoding="utf-8")
        return {**outcome(code, log), "exit": code, "seconds": round(time.monotonic() - started, 1)}

    @staticmethod
    def _stop(proc: subprocess.Popen) -> str:
        """SIGINT first, so the harness's own cleanup stops its daemons and
        drops its databases; SIGKILL to the group if it has not exited."""
        for sig, wait in ((signal.SIGINT, 60), (signal.SIGKILL, 30)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                raw, _ = proc.communicate(timeout=wait)
                return raw.decode("utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                continue
        return ""

    def blocked(self) -> str | None:
        if self.stop.exists():
            return "STOP file present"
        if shutil.disk_usage("/").free < DISK_FLOOR:
            return "free disk below the 8 GiB floor"
        return None


def run(runner: Runner, suites: list[str], only: list[str] | None,
        names=scenario_names) -> tuple[dict, dict]:
    report = {"suites": {}, "not_forced": NOT_FORCED, "complete": False}
    private: dict = {}
    for suite in suites:
        try:
            planned = [n for n in names(suite) if not only or n in only]
        except (subprocess.SubprocessError, ValueError, IndexError) as e:
            report["suites"][suite] = {"planned": 0, "rows": [], "error": str(e)[:300]}
            continue
        rows = []
        report["suites"][suite] = {"planned": len(planned), "rows": rows}
        for name in planned:
            reason = runner.blocked()
            if reason:
                report["stopped"] = f"before {suite}/{name}: {reason}"
                return report, private
            first = runner.invoke(suite, name, "real", base=True)
            retry = synthetic = None
            if first["status"] != "pass":
                retry = runner.invoke(suite, name, "real-retry", base=True)
                if retry["status"] != "pass":
                    synthetic = runner.invoke(suite, name, "synthetic", base=False)
            verdict = classify(first, retry, synthetic)
            rows.append({"scenario": name, "class": verdict, "differences": len(first["diffs"]),
                         "seconds": first["seconds"]})
            if verdict != "pass":
                private[f"{suite}/{name}"] = {"real": first, "real_retry": retry, "synthetic": synthetic}
            print(f"{suite}/{name}: {verdict} ({len(first['diffs'])} differences, {first['seconds']}s)",
                  flush=True)
    report["complete"] = True
    return report, private


def counts(report: dict) -> dict:
    out = {}
    for suite, body in report["suites"].items():
        classes = [r["class"] for r in body["rows"]]
        out[suite] = {"planned": body["planned"], "run": len(classes),
                      **{c: classes.count(c) for c in ("pass", "transient", "preexisting", "new", "error")},
                      "differences": sum(r["differences"] for r in body["rows"]),
                      "new_scenarios": [r["scenario"] for r in body["rows"] if r["class"] == "new"]}
    return out


def sweep_leftovers() -> int:
    """After a killed harness: drop the disposable databases it left. The
    mirror's PostgreSQL holds only this run's copies besides the pl_cf_mir_*
    banks, and scenarios run one at a time."""
    sys.path.insert(0, str(HARNESS))
    import pgdisposable
    from psycopg import sql
    dropped = 0
    with pgdisposable._admin() as conn:
        pgdisposable.assert_disposable_database(conn)
        for (name,) in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'pl\\_cf\\_%' "
                                    "AND datname NOT LIKE 'pl\\_cf\\_mir\\_%'").fetchall():
            pgdisposable.refuse_production_database(name)
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            dropped += 1
    return dropped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rust-bin", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--suites", nargs="*", default=list(SUITES), choices=list(SUITES))
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--stop-file", type=Path, default=Path("/control/STOP"))
    args = ap.parse_args(argv)
    if args.only:  # a misspelt name must not make an empty, "complete" run
        known = {n for suite in args.suites for n in scenario_names(suite)}
        unknown = sorted(set(args.only) - known)
        if unknown:
            print(f"forced.py: no such scenario in {args.suites}: {unknown}", file=sys.stderr)
            return 2
    base = os.environ.get("PL_HARNESS_BASE_DATABASE", "")
    # The address the harnesses will use: pgdisposable's own default when unset.
    host = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT") or "127.0.0.1:5433"
    try:  # the run's own refusal, before any database is created
        seed.refuse_live_dsn(f"postgresql://runner@{host}/{base}")
    except seed.Refused as e:
        print(f"forced.py: {e}", file=sys.stderr)
        return 3
    args.out.mkdir(parents=True, exist_ok=True)
    runner = Runner(args.rust_bin, args.out, args.timeout, args.stop_file, sweep=sweep_leftovers)
    report, private = run(runner, args.suites, args.only)
    summary = {"base": base, "complete": report["complete"], "stopped": report.get("stopped"),
               "not_forced": report["not_forced"], "counts": counts(report)}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (args.out / "private.json").write_text(json.dumps({"report": report, "differences": private},
                                                      indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary["counts"]))
    clean = all(not c["new"] and not c["error"] and not body.get("error")
                for c, body in zip(summary["counts"].values(), report["suites"].values()))
    return 0 if report["complete"] and clean else 1


if __name__ == "__main__":
    sys.exit(main())
