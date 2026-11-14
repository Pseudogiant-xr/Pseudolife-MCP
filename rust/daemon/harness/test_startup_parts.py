"""CI's two startup parts run every cell and control once, with no extra build."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE))
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



class Started(Exception):
    """A service start the test intercepted."""


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args):
        pass


EMPTY_STATE = {"rows": {}, "catalog": {}}


class GoldenChecking(unittest.TestCase):
    """CI checks committed goldens with the Rust side alone (#1314's sibling):
    the Python service may seed a template but is never started as an oracle,
    and every case CI runs has a golden for each stage it compares."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="pl-golden-checking-"))
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
                    self.assertEqual(set(stage), {"rows", "replaced", "catalog_changed"})
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

    def start_cells(self, module, main_args, invoke_python_as="python"):
        """Run a harness's main with every service start intercepted, and
        return the arms it started, in order. A template seed is not a start."""
        started = []

        def invoke(binary, kind, *args, seed_case=None, seed_only=False, **kwargs):
            if seed_case or seed_only:
                return None, [0.0, 0.0]
            started.append(kind)
            raise Started(kind)

        with patch.object(module, "invoke", invoke), \
                patch.object(module.pg, "create", lambda name, template=None: f"postgresql:///{name}"), \
                patch.object(module.pg, "drop", lambda name, *args: None), \
                patch.object(module.dbstate, "dump", lambda *args, **kwargs: json.loads(json.dumps(EMPTY_STATE))), \
                patch.object(module.daemons, "make_home", lambda root, name, config: Path(root) / name), \
                patch.dict(sys.modules, {"embedding_fixture": type(sys)("embedding_fixture")}), \
                patch("sys.stdout"):
            sys.modules["embedding_fixture"].create = lambda model: None
            module.main(["--rust-test-bin", str(self.binary), "--out", str(self.out), *main_args])
        return started

    def test_a_mutant_that_stops_a_table_write_reaches_its_row_path(self):
        wanted = "/rows/public.entries/rows:"
        self.assertTrue(startup_cases.reaches("/rows/public.entries/rows: row only in python: [1]", wanted))
        self.assertTrue(startup_cases.reaches("/rows/public.entries: only in python", wanted))
        # Another table, or a prefix of this one's name, is not this path.
        self.assertFalse(startup_cases.reaches("/rows/public.entries_archive: only in python", wanted))
        self.assertFalse(startup_cases.reaches("/replaced/public.entries: only in python", wanted))
        self.assertFalse(startup_cases.reaches("/startup/world: only in python", "/startup/world_facts:"))

    def test_checking_startup_goldens_never_starts_the_python_service(self):
        started = self.start_cells(startup_cases, ["--check-goldens", "--only", "empty"])
        self.assertEqual(started, ["rust"])

    def test_checking_the_clock_retry_golden_never_starts_the_python_service(self):
        started = self.start_cells(clock_retry_cases, ["--check-goldens"])
        self.assertEqual(started, ["rust"])

    def test_recording_still_starts_both_services(self):
        started = self.start_cells(startup_cases, ["--only", "empty"])
        self.assertEqual(started, ["python"])

    def test_checking_schema_goldens_opens_only_the_rust_storage(self):
        opened = []

        def storage(dsn):
            opened.append("python")
            raise Started("python")

        def rust_open(binary, dsn, refusal=None):
            opened.append("rust")
            raise Started("rust")

        import pseudolife_memory.storage.postgres as postgres
        with patch.object(postgres, "PostgresStorage", storage), \
                patch.object(schema_cases, "rust_open", rust_open), \
                patch.object(schema_cases.pg, "create", lambda name, template=None: f"postgresql:///{name}"), \
                patch.object(schema_cases.pg, "drop", lambda name, *args: None), \
                patch.object(schema_cases, "snapshot", lambda dsn: json.loads(json.dumps(EMPTY_STATE))), \
                patch.object(schema_cases.psycopg, "connect", lambda *args, **kwargs: Connection()), \
                patch.object(schema_cases, "assert_disposable_database", lambda conn: None), \
                patch.dict(os.environ, {"PL_HARNESS_SLICE": "pgs"}), \
                self.assertRaises(Started):
            schema_cases.main(["--rust-test-bin", str(self.binary), "--check-goldens", "--out", str(self.out)])
        self.assertEqual(opened, ["rust"])


if __name__ == "__main__":
    unittest.main()
