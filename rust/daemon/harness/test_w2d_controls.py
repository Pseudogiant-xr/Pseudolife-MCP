"""A mutant is evidence only after a clean, covered baseline."""
import contextlib
import io
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import w2d_search as search


class Scenario:
    require = {"with_entries": 1}


class MutantQualification(unittest.TestCase):
    def run_control(self, results):
        with (patch.object(sys, "argv", ["w2d_search.py", "mutants", "--rust-bin", "unused"]),
              patch.object(search, "SCENARIOS", {"control": Scenario}),
              patch.object(search, "MUTANTS", {"mutation": "control"}),
              patch.object(search, "seed_template"),
              patch.object(search, "run_scenario", side_effect=results),
              patch.dict(search.os.environ, {}, clear=False),
              contextlib.redirect_stdout(io.StringIO())):
            return search.main()

    def test_unmet_oracle_coverage_is_not_a_mutant_kill(self):
        invalid = {"cases": [], "db_diffs": ["coverage with_entries: 0 < 1"],
                   "coverage": {"with_entries": 0}}
        self.assertNotEqual(self.run_control([invalid]), 0)

    def test_preexisting_difference_is_not_a_mutant_kill(self):
        baseline = {"cases": [{"diffs": ["preexisting difference"]}],
                    "db_diffs": [], "coverage": {"with_entries": 1}}
        self.assertNotEqual(self.run_control([baseline]), 0)

    def test_covered_difference_after_clean_baseline_is_a_kill(self):
        baseline = {"cases": [{"diffs": []}], "db_diffs": [],
                    "coverage": {"with_entries": 1}}
        changed = {"cases": [{"diffs": ["mutation difference"]}], "db_diffs": [],
                   "coverage": {"with_entries": 1}}
        self.assertEqual(self.run_control([baseline, changed]), 0)

    def test_mutant_oracle_coverage_failure_is_not_a_kill(self):
        baseline = {"cases": [], "db_diffs": [], "coverage": {"with_entries": 1}}
        invalid = {"cases": [], "db_diffs": ["coverage with_entries: 0 < 1"],
                   "coverage": {"with_entries": 0}}
        self.assertNotEqual(self.run_control([baseline, invalid]), 0)

    def test_clean_mutant_survives(self):
        clean = {"cases": [], "db_diffs": [], "coverage": {"with_entries": 1}}
        self.assertNotEqual(self.run_control([clean, clean]), 0)


if __name__ == "__main__":
    unittest.main()
