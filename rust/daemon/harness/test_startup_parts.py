"""CI's two startup parts run every cell and control once, with no extra build."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE))
import candidate_record
import clock_retry_cases
import schema_cases
import schema_ci
import startup_ci
import startup_cases


def script(command):
    """The harness script a recorded command runs, or the program itself."""
    return Path(command[1]).name if len(command) > 1 and command[0] == sys.executable else command[0]


class StartupParts(unittest.TestCase):
    def run_part(self, *part):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(list(map(str, command)))
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(startup_ci.subprocess, "run", side_effect=fake_run), \
                patch.object(startup_ci, "writer_guard", return_value=0) as guard:
            binary = Path(tmp) / "daemon-test"
            binary.write_bytes(b"")
            code = startup_ci.main(["--no-build", "--rust-test-bin", str(binary), *part,
                                    "--out", str(Path(tmp) / "startup.json")])
        cells = [command for command in commands if script(command) == "startup_cases.py"]
        retries = [command for command in commands if script(command) == "clock_retry_cases.py"]
        self.assertFalse([command for command in commands if command[0] == "cargo"])
        self.assertEqual(code, 0)
        self.assertEqual(len(cells), 1)
        return cells[0], retries, guard.called

    def test_the_parts_partition_the_cases(self):
        first, second = startup_cases.part_cases(1), startup_cases.part_cases(2)
        self.assertTrue(first and second)
        self.assertEqual(sorted(first + second), sorted(startup_cases.CASES))
        self.assertFalse(set(first) & set(second))

    def test_every_mutant_control_runs_in_the_mutant_part(self):
        # Part 2 runs the mutants only once its own cells passed.
        controls = {case for case, _ in startup_cases.MUTANTS.values()}
        self.assertLessEqual(controls, set(startup_cases.part_cases(2)))

    def test_startup_cases_runs_exactly_the_selected_part(self):
        for part in (1, 2):
            ran = []

            def fake_cell(binary, number, case, result, mutant=None, expected=None, oracle=True):
                ran.append((case, mutant))
                result["stages"] = []

            with self.subTest(part=part), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(startup_cases.pg, "SLICE", "pgs"), \
                    patch.object(startup_cases, "cell", side_effect=fake_cell):
                binary = Path(tmp) / "daemon-test"
                binary.write_bytes(b"")
                code = startup_cases.main(["--rust-test-bin", str(binary), "--part", str(part),
                                           *(["--mutants"] if part == 2 else []),
                                           "--out", str(Path(tmp) / "startup.json")])
                # A stage-less fake cell catches no mutant, so part 2 fails its verdict.
                self.assertEqual(code, 0 if part == 1 else 1)
                self.assertEqual(tuple(case for case, mutant in ran if mutant is None),
                                 startup_cases.part_cases(part))
                self.assertEqual({mutant for _, mutant in ran if mutant},
                                 set(startup_cases.MUTANTS) if part == 2 else set())

    def test_part_one_runs_its_cases_without_the_controls(self):
        cells, retries, guarded = self.run_part("--part", "1")
        self.assertEqual(cells[cells.index("--part") + 1], "1")
        self.assertIn("--check-goldens", cells)
        self.assertNotIn("--mutants", cells)
        self.assertEqual((retries, guarded), ([], False))

    def test_part_two_runs_the_rest_with_every_control(self):
        cells, retries, guarded = self.run_part("--part", "2")
        self.assertEqual(cells[cells.index("--part") + 1], "2")
        self.assertTrue({"--check-goldens", "--mutants"} <= set(cells))
        self.assertEqual(len(retries), 1)
        self.assertIn("--mutants", retries[0])
        self.assertTrue(guarded)

    def test_without_a_part_every_cell_and_control_runs(self):
        cells, retries, guarded = self.run_part()
        self.assertNotIn("--part", cells)
        self.assertTrue({"--check-goldens", "--mutants"} <= set(cells))
        self.assertEqual(len(retries), 1)
        self.assertTrue(guarded)

    def test_a_part_cannot_record_goldens(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            startup_ci.main(["--part", "1", "--record-goldens", "--out", "startup.json"])


class SchemaBuildOnly(unittest.TestCase):
    def test_build_only_hands_off_the_executable_and_runs_nothing(self):
        commands = []
        artifact = {"reason": "compiler-artifact", "package_id": "path+file:///rust/daemon#pseudolife-daemon@0.19.0",
                    "profile": {"test": True}, "executable": None}

        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "pseudolife_daemon-0123.exe"
            executable.write_bytes(b"")
            artifact["executable"] = str(executable)

            def fake_run(command, **kwargs):
                commands.append(list(map(str, command)))
                return subprocess.CompletedProcess(command, 0, json.dumps(artifact) + "\n", "")

            handoff = Path(tmp) / "path.txt"
            with patch.object(schema_ci.subprocess, "run", side_effect=fake_run):
                code = schema_ci.main(["--build-only", "--test-bin-out", str(handoff)])
            self.assertEqual(code, 0)
            self.assertEqual(handoff.read_text(encoding="utf-8").strip(), str(executable.resolve()))
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:3], ["cargo", "test", "--locked"])
        self.assertTrue({"--no-run", "--features", "mutants"} <= set(commands[0]))
        # The daemon binary's own test executable, whatever integration
        # test binaries the package also has (tests/pdfium_provision.rs).
        self.assertIn(["--bin", schema_ci.PACKAGE],
                      [commands[0][i:i + 2] for i in range(len(commands[0]) - 1)])

    def test_build_only_refuses_run_options(self):
        for extra in (["--out", "schema.json"], ["--record-goldens"], []):
            arguments = ["--build-only", *extra] + ([] if extra == [] else ["--test-bin-out", "path.txt"])
            with self.subTest(extra=extra), self.assertRaises(SystemExit), patch("sys.stderr"):
                schema_ci.main(arguments)

    def test_a_run_still_requires_its_evidence_path(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            schema_ci.main(["--no-build", "--rust-test-bin", "daemon-test"])


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args):
        pass


# A bank dump with only the lease epoch, enough for every per-arm check to run.
EMPTY_STATE = {"rows": {"public.meta": {"columns": ["key", "value"], "rows": [["writer_lease_epoch", 1]]}},
               "catalog": {"extensions": []}}


def empty_state(*args, **kwargs):
    return json.loads(json.dumps(EMPTY_STATE))


class GoldenChecking(unittest.TestCase):
    """CI checks committed goldens with the Rust side alone: the Python
    service may seed a template but is never started as an oracle, and every
    case CI runs has a golden for each stage it compares."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="pl-golden-checking-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.binary = self.scratch / "daemon-test"
        self.binary.write_text("")
        self.out = self.scratch / "out.json"

    def test_every_startup_case_has_snapshot_and_database_goldens(self):
        golden = json.loads((HERE / "goldens" / "startup-hydration.json").read_text())
        self.assertEqual(set(golden["cases"]), set(startup_cases.CASES))
        self.assertEqual(set(golden["database"]), set(startup_cases.CASES))
        for case in startup_cases.CASES:
            with self.subTest(case=case):
                self.assertEqual(len(golden["cases"][case]), 2)
                self.assertEqual(len(golden["database"][case]), 2)
                for stage in golden["database"][case]:
                    self.assertEqual(set(stage), {"rows", "replaced", "catalog_rows"})
        mutant_cases = {case for case, _ in startup_cases.MUTANTS.values()}
        self.assertLessEqual(mutant_cases, set(golden["database"]))

    def test_the_clock_retry_golden_holds_its_attempts_and_every_phase(self):
        golden = json.loads((HERE / "goldens" / "clock-retry.json").read_text())
        self.assertEqual(len(golden["oracle"]["attempts"]), 3)
        self.assertEqual(set(golden["database"]), set(clock_retry_cases.PHASES))

    def test_every_schema_source_has_a_golden_for_both_opens(self):
        golden = json.loads((HERE / "goldens" / "schema-startup.json").read_text())
        history = json.loads((HERE / "schema_history.json").read_text())["versions"]
        self.assertEqual(set(golden["cases"]), {"fresh", *(source["commit"] for source in history)})
        for case, stages in golden["cases"].items():
            with self.subTest(case=case):
                self.assertEqual(len(stages), 2)

    def starts(self, module, main_args):
        """Run a harness's main with every service start faked, and return
        the services it started, in order. A template seed is not a start."""
        started = []

        def invoke(binary, kind, dsn, home, *args, seed_case=None, seed_only=False, **kwargs):
            if seed_case or seed_only:
                return None, [0.0, 0.0]
            started.append(kind)
            if module is clock_retry_cases:
                phases = Path(args[1])
                phases.mkdir(parents=True, exist_ok=True)
                for phase in clock_retry_cases.PHASES:
                    (phases / f"{phase}.json").write_text(json.dumps(EMPTY_STATE))
                return {"attempts": []}
            return {"ok": True, "startup": {}, "entries": []}, [0.0, 0.0]

        with patch.object(module, "invoke", invoke), \
                patch.object(module.pg, "create", lambda name, template=None: f"postgresql:///{name}"), \
                patch.object(module.pg, "drop", lambda name, *args: None), \
                patch.object(module.dbstate, "dump", empty_state), \
                patch.object(module.daemons, "make_home", lambda root, name, config: Path(root) / name), \
                patch.dict(sys.modules, {"embedding_fixture": type(sys)("embedding_fixture")}), \
                patch("sys.stdout"):
            sys.modules["embedding_fixture"].create = lambda model: None
            module.main(["--rust-test-bin", str(self.binary), "--out", str(self.out), *main_args])
        return started

    def opens(self, main_args):
        """The same for the schema cells: which storages opened, in order."""
        opened = []

        class Storage:
            def __init__(self, dsn):
                opened.append("python")

            def close(self):
                pass

        def rust_open(binary, dsn, refusal=None):
            opened.append("rust")

        import pseudolife_memory.storage.postgres as postgres
        with patch.object(postgres, "PostgresStorage", Storage), \
                patch.object(schema_cases, "rust_open", rust_open), \
                patch.object(schema_cases.pg, "create", lambda name, template=None: f"postgresql:///{name}"), \
                patch.object(schema_cases.pg, "drop", lambda name, *args: None), \
                patch.object(schema_cases, "snapshot", empty_state), \
                patch.object(schema_cases.psycopg, "connect", lambda *args, **kwargs: Connection()), \
                patch.object(schema_cases, "assert_disposable_database", lambda conn: None), \
                patch.dict(os.environ, {"PL_HARNESS_SLICE": "pgs"}), \
                patch("sys.stdout"):
            schema_cases.main(["--rust-test-bin", str(self.binary), "--out", str(self.out), *main_args])
        return opened

    def test_a_mutant_that_stops_a_table_write_reaches_its_row_path(self):
        wanted = "/rows/public.entries/rows:"
        self.assertTrue(startup_cases.reaches("/rows/public.entries/rows: row only in python: [1]", wanted))
        self.assertTrue(startup_cases.reaches("/rows/public.entries: only in python", wanted))
        # Another table, or a prefix of this one's name, is not this path.
        self.assertFalse(startup_cases.reaches("/rows/public.entries_archive: only in python", wanted))
        self.assertFalse(startup_cases.reaches("/replaced/public.entries: only in python", wanted))
        self.assertFalse(startup_cases.reaches("/startup/world: only in python", "/startup/world_facts:"))

    def test_checking_startup_goldens_never_starts_the_python_service(self):
        self.assertEqual(self.starts(startup_cases, ["--check-goldens", "--only", "empty"]), ["rust", "rust"])

    def test_checking_the_clock_retry_golden_never_starts_the_python_service(self):
        self.assertEqual(self.starts(clock_retry_cases, ["--check-goldens"]), ["rust"])

    def test_checking_schema_goldens_opens_only_the_rust_storage(self):
        self.assertEqual(self.opens(["--check-goldens"]), ["rust", "rust"])

    def test_recording_from_the_candidate_starts_only_rust_and_a_dry_run_writes_nothing(self):
        goldens = [HERE / "goldens" / name for name in ("startup-hydration.json", "clock-retry.json")]
        before = [path.read_bytes() for path in goldens]
        with patch.dict(os.environ):
            os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
            self.assertEqual(self.starts(startup_cases, ["--record-candidate", "--only", "empty"]), ["rust", "rust"])
            self.assertEqual(self.starts(clock_retry_cases, ["--record-candidate"]), ["rust"])
        self.assertEqual([path.read_bytes() for path in goldens], before)

    def test_without_golden_checking_both_sides_still_run(self):
        # Recording (and a plain run) still compares the two live.
        self.assertEqual(self.starts(startup_cases, ["--only", "empty"]), ["python", "rust"] * 2)
        self.assertEqual(self.starts(clock_retry_cases, []), ["python", "rust"])
        self.assertEqual(self.opens([]), ["python", "rust"] * 2)



class CandidateRecording(unittest.TestCase):
    """--record-candidate (candidate_record.py): the CLI harness's recorder
    vocabulary for the three startup goldens."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="pl-candidate-record-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.binary = self.scratch / "daemon-test"
        self.binary.write_bytes(b"candidate")

    def golden(self, value):
        path = self.scratch / "golden.json"
        path.write_text(candidate_record.dump_sorted(value), encoding="utf-8")
        return path

    def recording(self, path, **kwargs):
        options = dict(sections=("cases", "database"), whole=True, defined=["a", "b"])
        options.update(kwargs)
        return candidate_record.Recording(path, options.pop("sections"), self.binary, **options)

    @staticmethod
    def no_difference(entry):
        return []

    def run_quietly(self, recording, write, observations):
        recording.write = write
        with patch("sys.stdout"):
            for case, observed, failures in observations:
                recording.observe(case, observed, failures, self.no_difference)
            return recording.finish(write, "<check command>")

    def test_an_unchanged_golden_is_left_alone_and_passes(self):
        path = self.golden({"cases": {"a": [1], "b": [2]}, "database": {"a": [10], "b": [20]}, "other": 1})
        before = path.read_bytes()
        recording = self.recording(path)
        code = self.run_quietly(recording, True, [("a", {"cases": [1], "database": [10]}, []),
                                                  ("b", {"cases": [2], "database": [20]}, [])])
        self.assertEqual(code, 0)
        self.assertEqual(recording.report["unchanged"], ["a", "b"])
        self.assertEqual(path.read_bytes(), before)

    def test_a_dry_run_names_the_change_writes_nothing_and_fails(self):
        path = self.golden({"cases": {"a": [1], "b": [2]}, "database": {"a": [10], "b": [20]}})
        before = path.read_bytes()
        recording = self.recording(path)
        code = self.run_quietly(recording, False, [("a", {"cases": [1], "database": [11]}, []),
                                                   ("b", {"cases": [2], "database": [20]}, [])])
        self.assertEqual(code, 1)
        self.assertEqual(recording.report["changed"], ["a"])
        self.assertFalse(recording.report["written"])
        self.assertEqual(path.read_bytes(), before)

    def test_write_replaces_only_the_changed_and_new_cases_and_records_where_they_came_from(self):
        path = self.golden({"cases": {"a": [1], "b": [2]}, "database": {"a": [10], "b": [20]}, "other": 1})
        recording = self.recording(path, defined=["a", "b", "c"])
        code = self.run_quietly(recording, True, [("a", {"cases": [1], "database": [11]}, []),
                                                  ("c", {"cases": [3], "database": [30]}, [])])
        self.assertEqual(code, 0)
        written = json.loads(path.read_text())
        self.assertEqual(written["cases"], {"a": [1], "b": [2], "c": [3]})
        self.assertEqual(written["database"], {"a": [11], "b": [20], "c": [30]})
        self.assertEqual(written["other"], 1)
        stamp = written[candidate_record.PROVENANCE_KEY]
        self.assertEqual(set(stamp), {"a", "c"})
        self.assertEqual(set(stamp["a"]), {"commit", "rust_tree", "platform", "candidate_sha256"})
        self.assertEqual(stamp["a"]["candidate_sha256"], candidate_record.hashlib.sha256(b"candidate").hexdigest())

    def test_a_failed_or_vacuous_observation_refuses_the_whole_file(self):
        for observed, failures in (({"cases": [1], "database": [11]}, ["clock_diffs: relations.x"]),
                                   (None, [])):
            with self.subTest(vacuous=observed is None):
                path = self.golden({"cases": {"a": [1], "b": [2]}, "database": {"a": [10], "b": [20]}})
                before = path.read_bytes()
                recording = self.recording(path)
                code = self.run_quietly(recording, True, [("a", observed, failures),
                                                          ("b", {"cases": [2], "database": [21]}, [])])
                self.assertEqual(code, 1)
                self.assertIn("a", recording.report["refused"])
                self.assertFalse(recording.report["written"])
                self.assertEqual(path.read_bytes(), before)

    def test_an_entry_that_does_not_check_against_its_own_run_is_refused(self):
        path = self.golden({"cases": {"a": [1]}, "database": {"a": [10]}})
        recording = self.recording(path, defined=["a"])
        with patch("sys.stdout"):
            recording.observe("a", {"cases": [2], "database": [10]}, [], lambda entry: ["/cases: scrubbed"])
            self.assertEqual(recording.finish(True, "<check command>"), 1)
        self.assertIn("would not check", recording.report["refused"]["a"])

    def test_only_the_whole_matrix_removes_a_case(self):
        for whole, removed in ((True, ["gone"]), (False, [])):
            with self.subTest(whole=whole):
                path = self.golden({"cases": {"a": [1], "gone": [9]}, "database": {"a": [10], "gone": [90]}})
                recording = self.recording(path, whole=whole, defined=["a"])
                self.run_quietly(recording, True, [("a", {"cases": [1], "database": [10]}, [])])
                self.assertEqual(recording.report["removed"], removed)
                self.assertEqual("gone" in json.loads(path.read_text())["cases"], not whole)

    def test_a_single_case_golden_is_its_sections(self):
        path = self.golden({"oracle": {"attempts": [1]}, "database": {"initial": [1]}, "declared_noop_callbacks": []})
        recording = self.recording(path, sections=("oracle", "database"), whole=False,
                                   defined=["clock-retry"], single=True)
        code = self.run_quietly(recording, True, [("clock-retry", {"oracle": {"attempts": [2]},
                                                                    "database": {"initial": [1]}}, [])])
        self.assertEqual(code, 0)
        written = json.loads(path.read_text())
        self.assertEqual(written["oracle"], {"attempts": [2]})
        self.assertEqual(written["declared_noop_callbacks"], [])
        self.assertEqual(set(written[candidate_record.PROVENANCE_KEY]), {"clock-retry"})

    def test_rewriting_a_real_golden_changes_only_the_recorded_case(self):
        # The committed startup golden with one entry corrupted: re-recording
        # that case from its true value restores every other byte.
        real = HERE / "goldens" / "startup-hydration.json"
        committed = json.loads(real.read_text(encoding="utf-8"))
        case = "preset-rename"
        truth = {"cases": committed["cases"][case], "database": committed["database"][case]}
        corrupted = json.loads(json.dumps(committed))
        corrupted["database"][case][0]["rows"] = {}
        path = self.golden(corrupted)
        recording = self.recording(path, whole=False, defined=list(startup_cases.CASES))
        self.assertEqual(self.run_quietly(recording, True, [(case, truth, [])]), 0)
        self.assertEqual(recording.report["changed"], [case])
        rewritten = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(rewritten.pop(candidate_record.PROVENANCE_KEY)), {case})
        self.assertEqual(candidate_record.dump_sorted(rewritten), real.read_text(encoding="utf-8"))

    def test_a_mutant_in_the_environment_refuses_every_recording(self):
        with patch.dict(os.environ, {"PSEUDOLIFE_DAEMON_MUTANT": "startup-skip-world"}), patch("sys.stdout"):
            self.assertIn("PSEUDOLIFE_DAEMON_MUTANT", candidate_record.refuse_mutant_environment())
            for module, extra in ((startup_cases, ["--only", "empty"]), (clock_retry_cases, []),
                                  (schema_cases, ["--all-versions"])):
                with self.subTest(module=module.__name__), patch.object(module, "cell") as cell, \
                        patch.dict(os.environ, {"PL_HARNESS_SLICE": "pgs"}):
                    code = module.main(["--rust-test-bin", str(self.binary), "--out",
                                        str(self.scratch / "out.json"), "--record-candidate", "--write", *extra])
                    self.assertEqual(code, 1)
                    cell.assert_not_called()
            for driver in (startup_ci, schema_ci):
                with self.subTest(driver=driver.__name__), patch.object(driver.subprocess, "run") as run, \
                        self.assertRaises(SystemExit), patch("sys.stderr"):
                    driver.main(["--no-build", "--rust-test-bin", str(self.binary), "--out",
                                 str(self.scratch / "out.json"), "--record-candidate"])
                run.assert_not_called()   # refused before the checks and the build
        with patch.dict(os.environ, {"PSEUDOLIFE_DAEMON_MUTANT": ""}):
            self.assertIsNone(candidate_record.refuse_mutant_environment())

    def test_a_change_late_in_a_stage_is_shown_at_its_own_path(self):
        # Codex review of f126c9cce: dbstate.diff compares a list outside
        # /rows as one value and prints 300 characters of each side.
        stage = {"entries": [{"text": "x" * 400, "tags": []} for _ in range(3)]}
        changed = json.loads(json.dumps(stage))
        changed["entries"][2]["tags"] = ["late"]
        differences = candidate_record.section_diff([stage, stage], [stage, changed], "/cases")
        self.assertTrue(differences)
        self.assertTrue(all(d.startswith("/cases/1") for d in differences), differences)
        self.assertTrue(any("late" in d for d in differences), differences)

    def test_an_entry_naming_a_host_path_is_refused(self):
        for path in (str(Path.home()), tempfile.gettempdir(), str(candidate_record.REPO)):
            with self.subTest(path=path):
                golden = self.golden({"cases": {"a": [1]}, "database": {"a": [10]}})
                before = golden.read_bytes()
                recording = self.recording(golden, defined=["a"])
                observed = {"cases": [{"error": f"could not open {Path(path) / 'x'}"}], "database": [10]}
                self.assertEqual(self.run_quietly(recording, True, [("a", observed, [])]), 1)
                self.assertIn("host path", recording.report["refused"]["a"])
                self.assertEqual(golden.read_bytes(), before)
        self.assertEqual(candidate_record.host_paths({"error": "ordinary text"}), [])

    def test_a_failed_check_in_a_real_cell_result_refuses_the_case(self):
        # The wiring from a cell's own *_diffs to the refusal, as main does it.
        def failing_cell(binary, number, case, result, mutant=None, expected=None, oracle=True):
            stage = {"snapshots": {"rust": {"ok": True, "startup": {}, "entries": []}},
                     "database": {"rust": {"rows": {}, "replaced": {}, "catalog_rows": {}}},
                     "snapshot_diffs": [], "clock_diffs": [], "epoch_diffs": ["arm 0: constructor epoch did not increment once"]}
            result["stages"] = [stage, stage]

        golden = HERE / "goldens" / "startup-hydration.json"
        before = golden.read_bytes()
        with patch.object(startup_cases, "cell", failing_cell), patch("sys.stdout"), \
                patch.dict(os.environ, {"PL_HARNESS_SLICE": "pgs"}):
            os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
            code = startup_cases.main(["--rust-test-bin", str(self.binary), "--out", str(self.scratch / "out.json"),
                                       "--record-candidate", "--write", "--only", "empty"])
        self.assertEqual(code, 1)
        report = json.loads((self.scratch / "out.json").read_text())["record_candidate"]
        self.assertIn("epoch_diffs", report["refused"]["empty"])
        self.assertFalse(report["written"])
        self.assertEqual(golden.read_bytes(), before)

    def test_a_schema_commit_outside_the_history_is_refused(self):
        with patch.object(schema_cases, "cell") as cell, patch("sys.stdout"), \
                patch.dict(os.environ, {"PL_HARNESS_SLICE": "pgs"}):
            os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
            code = schema_cases.main(["--rust-test-bin", str(self.binary), "--out", str(self.scratch / "out.json"),
                                      "--record-candidate", "--commit", "0" * 40])
        self.assertEqual(code, 1)
        cell.assert_not_called()

    def test_the_differences_name_the_golden_and_the_candidate(self):
        self.assertEqual(candidate_record.labelled(["/x: python 1 vs rust 2", "/y: only in python",
                                                    "/rows/t/rows: row only in rust: [1]"]),
                         ["/x: golden 1 vs candidate 2", "/y: only in golden",
                          "/rows/t/rows: row only in candidate: [1]"])

    def test_write_needs_record_candidate_and_record_candidate_takes_no_controls(self):
        for module, arguments in ((startup_cases, ["--write"]), (startup_cases, ["--record-candidate", "--mutants"]),
                                  (startup_cases, ["--record-candidate", "--part", "1"]),
                                  (clock_retry_cases, ["--write"]), (clock_retry_cases, ["--record-candidate", "--mutants"]),
                                  (schema_cases, ["--write"]), (schema_cases, ["--record-candidate", "--refusals"]),
                                  (startup_ci, ["--write"]), (startup_ci, ["--record-candidate", "--record-goldens"]),
                                  (schema_ci, ["--write"]), (schema_ci, ["--record-candidate", "--record-goldens"])):
            with self.subTest(module=module.__name__, arguments=arguments), \
                    patch.dict(os.environ, {"PL_HARNESS_SLICE": "pgs"}), \
                    self.assertRaises(SystemExit), patch("sys.stderr"):
                module.main(["--rust-test-bin", str(self.binary), "--out", str(self.scratch / "out.json"),
                             *(["--no-build"] if module in (startup_ci, schema_ci) else []), *arguments])

    def drive(self, module, *arguments):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(list(map(str, command)))
            # The pre-checks pass; each recorder is a dry run that found a change.
            recorder = "--record-candidate" in map(str, command)
            return subprocess.CompletedProcess(command, 1 if recorder else 0, "", "")

        with patch.object(module.subprocess, "run", side_effect=fake_run):
            code = module.main(["--no-build", "--rust-test-bin", str(self.binary),
                                "--out", str(self.scratch / "out.json"), *arguments])
        return code, commands

    def test_startup_ci_records_both_goldens_even_when_the_first_would_change(self):
        for write in ([], ["--write"]):
            with self.subTest(write=bool(write)):
                code, commands = self.drive(startup_ci, "--record-candidate", *write)
                recorders = [c for c in commands if script(c) in ("startup_cases.py", "clock_retry_cases.py")]
                self.assertEqual([script(c) for c in recorders], ["startup_cases.py", "clock_retry_cases.py"])
                for command in recorders:
                    self.assertIn("--record-candidate", command)
                    self.assertEqual("--write" in command, bool(write))
                    self.assertNotIn("--mutants", command)
                    self.assertNotIn("--record-goldens", command)
                self.assertEqual(code, 1)

    def test_schema_ci_records_the_schema_golden_from_the_candidate(self):
        code, commands = self.drive(schema_ci, "--record-candidate", "--write")
        schema = [c for c in commands if script(c) == "run.py"]
        self.assertEqual(len(schema), 1)
        self.assertTrue({"schema", "--all-versions", "--record-candidate", "--write"} <= set(schema[0]))
        self.assertFalse({"--refusals", "--mutants", "--check-goldens", "--record-goldens"} & set(schema[0]))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
