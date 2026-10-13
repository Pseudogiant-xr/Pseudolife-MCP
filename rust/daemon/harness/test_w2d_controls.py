"""A mutant is evidence only after a clean, covered baseline."""
import contextlib
import copy
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
    def test_timeline_response_cannot_normalize_reversed_dense_hits(self):
        entries = [{"id": 1, "score": 0.8}, {"id": 2, "score": 0.8001}]
        response = {"status": 200, "headers": {"content-type": "application/json"},
                    "json": {"query": "when did this happen", "entries": entries}}
        reversed_response = copy.deepcopy(response)
        reversed_response["json"]["entries"].reverse()
        self.assertTrue(search.compare(response, reversed_response, timeline_fired=True)[0])
        self.assertFalse(search.compare(response, reversed_response)[0])

    def test_timeline_queries_use_oracle_event_context(self):
        state = {"rows": {"public.retrieval_events": {
            "columns": ["query", "params"],
            "rows": [["when", {"timeline": {"fired": True}}],
                     ["other", {"timeline": {"fired": False}}]]}}}
        self.assertEqual(search.timeline_queries(state), {"when"})

    def test_timeline_telemetry_cannot_normalize_reversed_dense_hits(self):
        served = [{"entry_id": 1, "rank": 0, "score": 0.8},
                  {"entry_id": 2, "rank": 1, "score": 0.8001}]
        state = {"rows": {"public.retrieval_events": {
            "columns": ["id", "served", "params"],
            "rows": [[1, served, {"timeline": {"fired": True}}]]}}}
        reversed_state = copy.deepcopy(state)
        reversed_state["rows"]["public.retrieval_events"]["rows"][0][1] = [
            dict(served[1], rank=0), dict(served[0], rank=1)]
        self.assertTrue(search.bank_diff(state, reversed_state))

    def test_slot_read_control_is_exercised(self):
        self.assertEqual(search.MUTANTS.get("w2d-cortex-skip-slot-reads"), "default")

    def test_new_seed_ages_past_the_initial_relative_time_bucket(self):
        with (patch.object(search.pg, "create", return_value="fixture"),
              patch.object(search.daemons, "make_home"),
              patch.object(search.daemons, "base_env", return_value={}),
              patch.object(search.subprocess, "run") as run,
              patch.object(search.time, "sleep") as sleep):
            run.return_value.returncode = 0
            run.return_value.stdout = "stored fixture"
            search.seed_template("fixture", 0)
        sleep.assert_called_once_with(70)

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
