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
REAL_BANK_ENV = ("PL_HARNESS_BASE_DATABASE", "PL_HARNESS_STUB_DEFAULT_CONTENT",
                 "PL_HARNESS_DREAM_REST_TIMEOUT", "PL_HARNESS_BASE_CACHE")
# The real-bank arms' REST timeout for /api/dream calls, in seconds: Python's
# Console dream run over the real bank's copy outlasted the default 120 s
# (console-dream-run and -gates, forced run of 2026-11-08). Synthetic arms
# keep the default.
DREAM_REST_TIMEOUT = 600
# Scenarios whose real-bank arms do not run, by scenario, with the reason:
# their premise cannot hold on a real bank, so a real-bank result would be
# timing noise. Their synthetic arm still runs and must pass ("excluded");
# CI's synthetic coverage is unchanged. Conductor decision, 2026-11-08.
REAL_BANK_EXCLUDED = {
    "w2e/sweep-analyzer-tick": (
        "the scenario rests on one analyzer batch (50 anchors) covering every entity, so the "
        "settled state does not depend on how many ticks ran; a real bank holds far more entities, "
        "its seeded entities are the newest, and each daemon's analyzer walks 50 per tick from the "
        "copied cursor on its own timer, so the two arms compare at different stages (the settle "
        "also never went quiet: forced run, 2026-11-08)"),
}
# Known limits the maintainer accepted, by scenario: an error where both real
# attempts each saw the runner's cgroup OOM counter rise is classified
# "accepted" with this reason, not "error". Any other error in these
# scenarios still counts.
ACCEPTED_OOM = {
    name: ("a known Python memory limit at the harness's worst-case -1.0 floors: Python peaks at "
           "14.5 GiB in deep apply plus the candidate judge (3.3 GiB at production floors) while "
           "Rust passes at 4.1 GiB, so both daemons together exceed the 18 GiB runner. Accepted by "
           "the maintainer on 2026-11-08 (option (c), Python is being retired); no Python fix")
    for name in ("w2e/candidate-auto", "w2e/candidate-auto-reopen")
}
DISK_FLOOR = 8 * 1024 ** 3
# Scenarios that drive the dream, judge or analyzer queues run on the seed's
# quiet copy (seed.py quiet): the conductor's decision D1, 2026-11-07. On the
# real bank's copy they waited on its whole backlog (3,915 entries pending a
# dream, 276 entity and 376 edge proposals pending a judge), which no stub
# answer settles, so they compared nothing. Why each prefix is here:
QUIET_PREFIXES = {"w2e": (
    "judge-merge-",           # the merge judge: 276 real entity proposals pending (D1)
    "sweep-analyzer-",        # the analyzer files proposals into the same queues (D1)
    "console-dream-run",      # a Console dream pulls the 3,915-entry dream backlog (D1)
    "junk-",                  # the junk judge reached real proposals at a different time per side
    "link-",                  # the link judge: 376 real edge proposals pending (2026-11-08)
    "hook-session-end-dream",  # session-end dreams expect no pending entry (2026-11-08)
    "cli-episode-end-dream",
)}
QUIET_WHY = ("drives the dream, judge or analyzer queue; on the real bank's copy it "
             "waited on the whole real backlog, so it runs on the seed's copy with that backlog "
             "acknowledged, its pending proposals removed and its automatic decisions (which the "
             "judges reconsider) removed")
QUIET_DB = seed.QUIET_DB
# The curation judge's scenarios, on a quiet copy that also holds no current
# lesson or world fact (conductor decision (c), 2026-11-08). The judge takes
# the 8 most similar pairs over every current record, and on the real bank's
# copy (3,748 lessons, 411 world facts) real pairs outranked the scenarios'
# own, which were never judged: curation-shadow and the curation-auto family
# never settled. Checked before QUIET_PREFIXES.
CURATION_PREFIXES = {"w2e": ("curation-",)}
CURATION_WHY = ("drives the curation judge, which ranks pairs over every current lesson and world "
                "fact; on the real bank's copy real pairs outranked the scenario's, so it runs on a "
                "quiet copy whose real lessons and world facts are superseded")
CURATION_DB = seed.CURATION_DB


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


def base_for(suite: str, name: str) -> tuple[str, str | None]:
    """Which base a scenario's real-bank attempts copy, and why."""
    if any(name.startswith(p) for p in CURATION_PREFIXES.get(suite, ())):
        return "curation", CURATION_WHY
    if any(name.startswith(p) for p in QUIET_PREFIXES.get(suite, ())):
        return "quiet", QUIET_WHY
    return "main", None


def quiet_base_use(report: dict) -> dict:
    """The scenarios that ran on the quiet base, for the summary."""
    used = [f"{suite}/{r['scenario']}" for suite, body in report["suites"].items()
            for r in body["rows"] if r.get("base") == "quiet"]
    return {"database": QUIET_DB, "scenarios": used, "why": QUIET_WHY}


def curation_base_use(report: dict) -> dict:
    """The scenarios that ran on the curation base, for the summary."""
    used = [f"{suite}/{r['scenario']}" for suite, body in report["suites"].items()
            for r in body["rows"] if r.get("base") == "curation"]
    return {"database": CURATION_DB, "scenarios": used, "why": CURATION_WHY}


# A kept daemon log's tail: one log covers one launch (daemons.py opens it
# "wb"), and its last lines say why it died.
KEPT_LOG_BYTES = 4 * 1024 * 1024


def keep_daemon_logs(scratch: Path, kept: Path) -> str:
    """Copy the tail of each daemon.log under ``scratch`` into ``kept``. The
    daemons' own logs say which one died and why; they may quote bank text,
    so they stay beside the private report on the host. A log that cannot be
    kept (a full disk, a log gone mid-copy) is noted, never fatal."""
    notes = ""
    for daemon_log in scratch.rglob("daemon.log") if scratch.is_dir() else ():
        rel = daemon_log.parent.relative_to(scratch)
        try:
            target = kept / rel
            target.mkdir(parents=True, exist_ok=True)
            with daemon_log.open("rb") as src, (target / "daemon.log").open("wb") as dst:
                size = src.seek(0, os.SEEK_END)
                if size > KEPT_LOG_BYTES:
                    dst.write(f"[truncated: last {KEPT_LOG_BYTES} of {size} bytes]\n".encode())
                src.seek(max(0, size - KEPT_LOG_BYTES))
                shutil.copyfileobj(src, dst)
        except OSError as e:
            notes += f"\nkept no daemon log for {rel}: {e}"
    return notes


def read_oom_kills() -> int | None:
    """The OOM kills inside this container's memory cgroup so far, or None
    outside one: an error with kills during its attempt is the runner's
    capacity, not a daemon's verdict."""
    try:
        for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        pass
    return None


class Runner:
    """One harness process per scenario; tests replace ``invoke`` or ``_run``."""

    def __init__(self, rust_bin: Path, out: Path, timeout: float, stop: Path, sweep=None,
                 scratch: Path = Path("/tmp/forced"), oom_kills=read_oom_kills):
        self.rust_bin, self.out, self.timeout, self.stop = rust_bin, out, timeout, stop
        self.sweep = sweep or (lambda: 0)
        self.scratch, self.oom_kills = scratch, oom_kills

    def invoke(self, suite: str, name: str, label: str, base: bool) -> dict:
        script = HARNESS / SUITES[suite][0]
        work = self.out / suite / label
        work.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
        env = dict(os.environ, PYTHONUNBUFFERED="1",
                   PL_HARNESS_SCRATCH=str(self.scratch / suite / safe))
        which = base_for(suite, name)[0] if base else None
        if not base:
            for key in REAL_BANK_ENV:
                env.pop(key, None)
        elif which == "quiet":
            env["PL_HARNESS_BASE_DATABASE"] = QUIET_DB
        elif which == "curation":
            env["PL_HARNESS_BASE_DATABASE"] = CURATION_DB
        if base:
            env.setdefault("PL_HARNESS_DREAM_REST_TIMEOUT", str(DREAM_REST_TIMEOUT))
            # One read of each base's row hashes for the whole run, not one per
            # scenario process (dbstate checks it against the base each time).
            env["PL_HARNESS_BASE_CACHE"] = str(self.scratch / "base-cache")
        reaped = self.reap_leftovers()
        before = self.oom_kills()
        started = time.monotonic()
        code, log = self._run([sys.executable, str(script), "live", "--rust-bin", str(self.rust_bin),
                               "--only", name, "--out", str(work / f"{safe}.json")], env)
        after = self.oom_kills()
        # Scenarios run one at a time, so no disposable database should
        # outlive its attempt; a harness that raised never reached its drops.
        swept = self.sweep()
        if swept:
            log += f"\nswept {swept} leftover disposable databases"
        if reaped:
            log = f"stopped {len(reaped)} leftover processes before this scenario\n" + log
        kept = work / f"{safe}.daemon-logs"
        shutil.rmtree(kept, ignore_errors=True)  # an earlier attempt's logs are not this one's
        try:
            if outcome(code, log)["status"] != "pass":
                log += keep_daemon_logs(Path(env["PL_HARNESS_SCRATCH"]), kept)
        finally:
            shutil.rmtree(env["PL_HARNESS_SCRATCH"], ignore_errors=True)
        (work / f"{safe}.log").write_text(log, encoding="utf-8")
        kills = after - before if before is not None and after is not None else None
        return {**outcome(code, log), "exit": code, "seconds": round(time.monotonic() - started, 1),
                "oom_kills": kills, "base": which}

    def _run(self, argv: list[str], env: dict) -> tuple[int | None, str]:
        # Its own process group, stopped whatever the outcome: a harness that
        # raised may leave a daemon its cleanup lost (2.9 GiB each).
        proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            raw, _ = proc.communicate(timeout=self.timeout)
            code, log = proc.returncode, raw.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            code, log = None, self._stop(proc) + f"\nTIMEOUT after {self.timeout:g}s"
        if hasattr(os, "killpg"):  # the runner is Linux
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return code, log

    def reap_leftovers(self) -> list[int]:
        """Stop every process left from an earlier scenario, found by this
        run's own marker (a daemon's data directory under the run's scratch
        root): a daemon in a session of its own escapes its group's stop."""
        marker = str(self.scratch) + os.sep
        found = []
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return found
        for entry in proc_root.iterdir():
            if not entry.name.isdigit() or int(entry.name) in (os.getpid(), os.getppid()):
                continue
            try:
                raw = (entry / "environ").read_bytes()
            except OSError:
                continue
            for item in raw.split(b"\0"):
                if item.startswith(b"PSEUDOLIFE_MCP_DATA_DIR=") \
                        and item.split(b"=", 1)[1].decode(errors="replace").startswith(marker):
                    try:
                        os.kill(int(entry.name), signal.SIGKILL)
                        found.append(int(entry.name))
                    except ProcessLookupError:
                        pass
                    break
        return found

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
            if f"{suite}/{name}" in REAL_BANK_EXCLUDED:
                synthetic = runner.invoke(suite, name, "synthetic", base=False)
                # Only the real bank is excluded: a failing synthetic arm is an error.
                verdict = "excluded" if synthetic["status"] == "pass" else "error"
                rows.append({"scenario": name, "class": verdict,
                             "differences": len(synthetic["diffs"]), "seconds": synthetic["seconds"],
                             "base": None, "oom_kills": _oom_total(synthetic)})
                if verdict != "excluded":
                    private[f"{suite}/{name}"] = {"real": None, "real_retry": None,
                                                  "synthetic": synthetic}
                print(f"{suite}/{name}: {verdict} (real bank excluded; synthetic "
                      f"{synthetic['status']})", flush=True)
                continue
            first = runner.invoke(suite, name, "real", base=True)
            retry = synthetic = None
            if first["status"] != "pass":
                retry = runner.invoke(suite, name, "real-retry", base=True)
                if retry["status"] != "pass":
                    synthetic = runner.invoke(suite, name, "synthetic", base=False)
            verdict = classify(first, retry, synthetic)
            # Each failing real attempt must itself have been OOM-killed: an
            # unrelated failure stays an error, and the small synthetic arm's
            # kills are not the accepted limit.
            if verdict == "error" and f"{suite}/{name}" in ACCEPTED_OOM \
                    and all((a.get("oom_kills") or 0) > 0 for a in (first, retry)):
                verdict = "accepted"
            rows.append({"scenario": name, "class": verdict, "differences": len(first["diffs"]),
                         "seconds": first["seconds"], "base": first.get("base"),
                         "oom_kills": _oom_total(first, retry, synthetic)})
            if verdict != "pass":
                private[f"{suite}/{name}"] = {"real": first, "real_retry": retry, "synthetic": synthetic}
            print(f"{suite}/{name}: {verdict} ({len(first['diffs'])} differences, {first['seconds']}s)",
                  flush=True)
    report["complete"] = True
    return report, private


def _oom_total(*attempts) -> int | None:
    measured = [a["oom_kills"] for a in attempts if a and a.get("oom_kills") is not None]
    return sum(measured) if measured else None


# A real bank's own rows can stand in for a seed's fixture, which then may not
# be the case the scenario names: reported beside the counts.
FIXTURE_NOTES = {
    "w2e/dream-deep-exclusion": "the real bank already holds an entity named 'release-checklist', "
                                "which stands in for the seed's lesson-only one, so the lesson-only "
                                "exclusion case may not be exercised on the real copy",
}


def _seed_and_copy_comments(copy: str) -> tuple[str, str]:
    """The seed's provenance and ``copy``'s stamp, from the mirror PostgreSQL."""
    sys.path.insert(0, str(HARNESS))
    import pgdisposable
    with pgdisposable._admin() as conn:
        def comment(name):
            row = conn.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                               "WHERE datname = %s", (name,)).fetchone()
            return row[0] if row else None
        return comment(os.environ.get("PL_HARNESS_BASE_DATABASE", "")), comment(copy)


def base_comments() -> tuple[str, str]:
    """The seed's provenance and the quiet copy's."""
    return _seed_and_copy_comments(QUIET_DB)


def curation_comments() -> tuple[str, str]:
    """The seed's provenance and the curation copy's."""
    return _seed_and_copy_comments(CURATION_DB)


def run_scratch() -> Path:
    """This run's own marker: a leftover is reaped only under it, so two runs
    on one host never stop each other's daemons."""
    return Path("/tmp/forced") / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}-" \
        f"{os.urandom(3).hex()}"


def counts(report: dict) -> dict:
    out = {}
    for suite, body in report["suites"].items():
        classes = [r["class"] for r in body["rows"]]
        out[suite] = {"planned": body["planned"], "run": len(classes),
                      **{c: classes.count(c) for c in ("pass", "transient", "preexisting", "new",
                                                        "accepted", "excluded", "error")},
                      "differences": sum(r["differences"] for r in body["rows"]),
                      # Errors whose attempts saw the runner's cgroup kill a process.
                      # Null when no attempt could read the cgroup: not measured is not none.
                      "errors_with_oom": None if all(r.get("oom_kills") is None for r in body["rows"])
                      else sum(1 for r in body["rows"] if r["class"] == "error" and r.get("oom_kills")),
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
    if args.suites == []:
        # Given but empty plans nothing, and the run would report itself complete.
        print("forced.py: --suites was given no suite names, so nothing was run; pass the suites "
              "to run, or omit --suites to run them all", file=sys.stderr)
        return 2
    if args.only == []:
        # Given but empty (a name list that came out empty) is not "no
        # filter": it would plan every scenario of the chosen suites.
        print("forced.py: --only was given no scenario names, so nothing was run; pass the names "
              "to run, or omit --only to run every scenario", file=sys.stderr)
        return 2
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
    planned = {suite: [n for n in scenario_names(suite) if not args.only or n in args.only]
               for suite in args.suites if suite in QUIET_PREFIXES or suite in CURATION_PREFIXES}
    # An excluded scenario's real-bank arms never run, so it needs no quiet copy.
    if any(base_for(suite, n)[0] == "quiet" and f"{suite}/{n}" not in REAL_BANK_EXCLUDED
           for suite, names in planned.items() for n in names):
        try:  # a reseed drops the quiet copy; an old one would compare an older backup
            seed.quiet_preflight(*base_comments())
        except seed.Refused as e:
            print(f"forced.py: {e}", file=sys.stderr)
            return 3
    if any(base_for(suite, n)[0] == "curation" for suite, names in planned.items() for n in names):
        try:  # dropped by a reseed too
            seed.curation_preflight(*curation_comments())
        except seed.Refused as e:
            print(f"forced.py: {e}", file=sys.stderr)
            return 3
    args.out.mkdir(parents=True, exist_ok=True)
    runner = Runner(args.rust_bin, args.out, args.timeout, args.stop_file, sweep=sweep_leftovers,
                    scratch=run_scratch())
    report, private = run(runner, args.suites, args.only)
    summary = {"base": base, "complete": report["complete"], "stopped": report.get("stopped"),
               "not_forced": report["not_forced"], "quiet_base": quiet_base_use(report),
               "curation_base": curation_base_use(report),
               "accepted_limits": ACCEPTED_OOM,
               "real_bank_exclusions": REAL_BANK_EXCLUDED,
               "fixture_notes": FIXTURE_NOTES,
               "counts": counts(report)}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (args.out / "private.json").write_text(json.dumps({"report": report, "differences": private},
                                                      indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary["counts"]))
    clean = all(not c["new"] and not c["error"] and not body.get("error")
                for c, body in zip(summary["counts"].values(), report["suites"].values()))
    return 0 if report["complete"] and clean else 1


if __name__ == "__main__":
    sys.exit(main())
