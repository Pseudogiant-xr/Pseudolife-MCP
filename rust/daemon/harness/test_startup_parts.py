"""CI's two startup parts run every cell and control once, with no extra build."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE))
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
        self.assertEqual(first + second, startup_cases.CASES)

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

    def test_build_only_refuses_run_options(self):
        for extra in (["--out", "schema.json"], ["--record-goldens"], []):
            arguments = ["--build-only", *extra] + ([] if extra == [] else ["--test-bin-out", "path.txt"])
            with self.subTest(extra=extra), self.assertRaises(SystemExit), patch("sys.stderr"):
                schema_ci.main(arguments)

    def test_a_run_still_requires_its_evidence_path(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            schema_ci.main(["--no-build", "--rust-test-bin", "daemon-test"])


if __name__ == "__main__":
    unittest.main()
