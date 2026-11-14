"""Re-record a startup harness golden from the Rust candidate (--record-candidate).

Since the Python freeze the committed goldens are Rust regression snapshots,
and ``--record-goldens`` records the frozen oracle, which reproduces a golden
a deliberate Rust change departs from. ``schema_cases.py``,
``startup_cases.py`` and ``clock_retry_cases.py`` therefore take
``--record-candidate [--write]``, with the vocabulary of the CLI harness's
recorder (``rust/cli_harness/README.md``, "Goldens after the freeze"):

- each selected case runs once on the candidate, without the oracle, and is
  classed against the committed golden: ``unchanged``, ``changed`` (printed
  with its difference, the golden labelled ``golden``) or ``new``; recording
  the whole matrix also lists a recorded case it no longer defines as
  ``removed``;
- without ``--write`` nothing is written, and the run exits 1 if anything
  would change; with it only changed and new entries are replaced (and
  removed ones dropped), every other byte of the file stays;
- a case whose observation failed the harness's own checks (clock,
  idempotence, epoch, refusal state, the expected outcome), or is vacuous, is
  refused, and then nothing is written; so is a case whose entry names a host
  path (the checkout, the home or temporary directory), which would not
  replay on another machine and would publish a user name, and a whole run
  with ``PSEUDOLIFE_DAEMON_MUTANT`` set, which would record a mutant;
- the golden's top-level ``recorded_from_candidate`` names, for each case
  written this way, the checkout's commit, whether its Rust sources differed
  from that commit (``rust_tree``), the platform and the candidate's SHA-256.
  Checking reads only the cases, so it never compares this.

One golden serves both operating systems, so after ``--write`` check it with
``--check-goldens --mutants`` on this OS and on the other before committing:
the recorder compares each entry only with the run it came from.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CANDIDATE_PROVENANCE = "recorded_from_candidate"
MUTANT_ENV = "PSEUDOLIFE_DAEMON_MUTANT"
# The paths whose change makes the binary differ from the commit (the CLI
# harness's recorder uses the same set).
RUST_SOURCES = (":(glob)rust/**/*.rs", ":(glob)rust/**/Cargo.toml", "rust/Cargo.lock",
                ":(glob)rust/**/build.rs", "rust/rust-toolchain.toml")
PLATFORM = "windows" if os.name == "nt" else "linux"


def refuse_mutant_environment() -> str | None:
    """Why a recording must not start: a mutant selected in the environment."""
    if os.environ.get(MUTANT_ENV):
        return (f"{MUTANT_ENV}={os.environ[MUTANT_ENV]} is set, so the candidate would run a source "
                "mutant and its state would become the golden; unset it and record again")
    return None


def host_paths(entry) -> list[str]:
    """The host paths an entry names, raw or JSON-escaped: the checkout, the
    home directory and the temporary directory. A golden must hold none."""
    text = json.dumps(entry, allow_nan=False)
    found = []
    for path in {str(REPO), str(Path.home()), tempfile.gettempdir()}:
        if len(path) < 4:
            continue
        spellings = {path, path.replace("\\", "/"), json.dumps(path)[1:-1]}
        if any(spelling in text for spelling in spellings):
            found.append(path)
    return sorted(found)


def round_trip(entry) -> list[str]:
    """The differences between an entry and its JSON round trip: the entry
    is exactly what --check-goldens compares, so it must survive the file."""
    import dbstate
    return dbstate.diff(entry, json.loads(json.dumps(entry, allow_nan=False)))


def provenance(candidate: Path, repo: Path = REPO) -> dict:
    """Where a candidate recording comes from."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    head = git("rev-parse", "--verify", "HEAD^{commit}")
    status = git("status", "--porcelain", "--", *RUST_SOURCES)
    tree = "unknown" if head.returncode or status.returncode else (
        "modified" if status.stdout.strip() else "clean")
    return {"commit": head.stdout.strip() if head.returncode == 0 else None, "rust_tree": tree,
            "platform": PLATFORM, "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}


def labelled(differences: list[str]) -> list[str]:
    """dbstate.diff names its sides python and rust; here they are the
    committed golden and the candidate's observation."""
    swaps = (("only in python", "only in golden"), ("only in rust", "only in candidate"),
             (": python ", ": golden "), (" vs rust ", " vs candidate "))
    out = []
    for difference in differences:
        for old, new in swaps:
            difference = difference.replace(old, new)
        out.append(difference)
    return out


def section_diff(want, got, path: str) -> list[str]:
    """dbstate.diff, descending into lists of the same length: dbstate.diff
    compares a list outside ``/rows`` as one value, printing only its first
    300 characters, so a change late in a stage would show identical
    prefixes. A ``/rows`` list keeps dbstate's row-set comparison."""
    import dbstate
    if isinstance(want, dict) and isinstance(got, dict):
        out = []
        for key in sorted(set(want) | set(got)):
            if key in want and key in got:
                out += section_diff(want[key], got[key], f"{path}/{key}")
            else:
                out += dbstate.diff({key: want[key]} if key in want else {},
                                    {key: got[key]} if key in got else {}, path)
        return out
    if (isinstance(want, list) and isinstance(got, list) and len(want) == len(got)
            and not path.endswith("/rows")):
        return [d for index, (left, right) in enumerate(zip(want, got))
                for d in section_diff(left, right, f"{path}/{index}")]
    return dbstate.diff(want, got, path)


class Recording:
    """One golden file's candidate recording: classify each case, then write."""

    def __init__(self, golden: Path, sections: tuple[str, ...], candidate: Path, *, whole: bool,
                 defined: list[str], dump, single: bool = False):
        """``sections`` are the golden's top-level maps from case to entry;
        with ``single`` the golden holds one case and each section is that
        case's entry itself (clock-retry.json)."""
        self.golden, self.sections, self.candidate = golden, sections, candidate
        self.whole, self.defined, self.dump, self.single = whole and not single, defined, dump, single
        self.committed = json.loads(golden.read_text(encoding="utf-8")) if golden.is_file() else {}
        self.report: dict = {"unchanged": [], "changed": [], "new": [], "removed": [], "refused": {},
                             "written": False}
        self.updates: dict[str, dict] = {}

    def entry(self, case: str) -> dict | None:
        """The committed entry of ``case``: each section's value, or None if
        any section lacks it."""
        values = {}
        if self.single:
            return ({section: self.committed[section] for section in self.sections}
                    if all(section in self.committed for section in self.sections) else None)
        for section in self.sections:
            table = self.committed.get(section)
            if not isinstance(table, dict) or case not in table:
                return None
            values[section] = table[case]
        return values

    def observe(self, case: str, observed: dict | None, failures: list[str], recheck) -> None:
        """Class one case. ``observed`` maps each section to the candidate's
        entry (None when the run produced none); ``failures`` are the
        harness's own checks that failed; ``recheck(observed)`` returns the
        differences between the entry and the comparison of the run it came
        from, which must be empty."""
        import dbstate
        want = self.entry(case)
        if observed is None or failures:
            why = ("its observation is vacuous: the run produced no entry" if observed is None else
                   "its observation failed the harness's own checks: " + "; ".join(failures[:5]))
            self.report["refused"][case] = why
            print(f"  REFUSED {case}: {why}", flush=True)
            return
        named = host_paths(observed)
        if named:
            why = ("its entry names a host path (" + ", ".join("<home>" if p == str(Path.home()) else p
                                                         for p in named) + "), which a golden must not hold")
            self.report["refused"][case] = why
            print(f"  REFUSED {case}: {why}", flush=True)
            return
        again = recheck(observed)
        if again:
            why = "the entry would not check against the run it came from: " + "; ".join(again[:3])
            self.report["refused"][case] = why
            print(f"  REFUSED {case}: {why}", flush=True)
            return
        if want is not None:
            differences = labelled([d for section in self.sections
                                    for d in section_diff(want[section], observed[section], f"/{section}")])
            if not differences:
                self.report["unchanged"].append(case)
                print(f"  unchanged {case}", flush=True)
                return
            self.report["changed"].append(case)
            print(f"  CHANGED {case}", flush=True)
            for difference in differences[:20]:
                print(f"        {difference}")
        else:
            self.report["new"].append(case)
            print(f"  new {case}", flush=True)
            for section in self.sections:
                print(f"        {section}: {json.dumps(observed[section])[:400]}")
        self.updates[case] = observed

    def finish(self, write: bool, check_command: str) -> int:
        """Write or report; the exit status the harness returns."""
        if not self.whole and not self.single:
            observed = set(self.report["unchanged"]) | set(self.updates) | set(self.report["refused"])
            kept = [case for case in self.defined if self.entry(case) is not None and case not in observed]
            if kept:
                print(f"  kept as recorded (not selected): {', '.join(kept)}", flush=True)
        if self.whole:
            recorded = set()
            for section in self.sections:
                recorded |= set(self.committed.get(section) or {})
            self.report["removed"] = sorted(recorded - set(self.defined))
            for case in self.report["removed"]:
                print(f"  removed {case} (recorded, but no longer a case)", flush=True)
        if self.report["refused"]:
            print(f"  refused {len(self.report['refused'])} case(s): nothing written to {self.golden.name}",
                  flush=True)
            return 1
        would_change = bool(self.updates or self.report["removed"])
        if not would_change:
            print(f"  {self.golden.name}: nothing to change", flush=True)
            return 0
        if not write:
            print(f"  dry run: {self.golden.name} would change ({len(self.report['changed'])} changed, "
                  f"{len(self.report['new'])} new, {len(self.report['removed'])} removed); review the "
                  "differences above, then add --write", flush=True)
            return 1
        golden = self.committed
        for section in self.sections:
            if self.single:
                for observed in self.updates.values():
                    golden[section] = observed[section]
                continue
            table = dict(golden.get(section) or {})
            for case in self.report["removed"]:
                table.pop(case, None)
            for case, observed in self.updates.items():
                table[case] = observed[section]
            golden[section] = table
        stamp = provenance(self.candidate)
        record = dict(golden.get(CANDIDATE_PROVENANCE) or {})
        for case in self.updates:
            record[case] = stamp
        for case in self.report["removed"]:
            record.pop(case, None)
        golden[CANDIDATE_PROVENANCE] = record
        self.golden.write_text(self.dump(golden), encoding="utf-8", newline="\n")
        self.report["written"] = True
        print(f"  wrote {self.golden} ({len(self.updates)} case(s) from the candidate at "
              f"{(stamp['commit'] or 'an unknown commit')[:12]}, Rust tree {stamp['rust_tree']}); one "
              f"golden serves both operating systems, so check it here and on the other OS before "
              f"committing: {check_command}", flush=True)
        return 0


def dump_sorted(value: dict) -> str:
    """The startup goldens' own format."""
    return json.dumps(value, indent=1, sort_keys=True, allow_nan=False) + "\n"


def stage_failures(stage: dict, ignore: tuple[str, ...] = ()) -> list[str]:
    """Every difference a stage's own checks report, by key."""
    return [f"{key}: {difference}" for key, value in stage.items()
            if key.endswith("_diffs") and key not in ignore
            for difference in (value if isinstance(value, list) else
                               [d for phase in value.values() for d in phase] if isinstance(value, dict) else [])]


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
