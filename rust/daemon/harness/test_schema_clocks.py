"""Clock normalization admits only values produced during this arm's open."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import schema_cases


class SchemaClocks(unittest.TestCase):
    def test_coarse_windows_endpoints_admit_only_one_tick_of_uncertainty(self):
        state = {"rows": {"public.relations": {"columns": ["name", "created_at"],
                                              "rows": [["uses", 100.01]]}}}
        self.assertEqual(schema_cases.clock_diffs(state, {"rows": {}}, (100.0, 100.0), 0.015625), [])
        state["rows"]["public.relations"]["rows"][0][1] = 100.02
        self.assertTrue(schema_cases.clock_diffs(state, {"rows": {}}, (100.0, 100.0), 0.015625))
    def test_zero_or_outside_window_is_not_normalized(self):
        for clock in [0.0, 99.0, 102.0, float("nan"), float("inf"), True]:
            state = {"rows": {"public.relations": {"columns": ["name", "created_at"],
                                                  "rows": [["uses", clock]]}}}
            self.assertTrue(schema_cases.clock_diffs(state, {"rows": {}}, (100.0, 101.0)))

    def test_new_clock_is_admitted_and_existing_clock_is_left_to_exact_diff(self):
        state = {"rows": {"public.relations": {"columns": ["name", "created_at"],
                                              "rows": [["uses", 100.5]]}}}
        self.assertEqual(schema_cases.clock_diffs(state, {"rows": {}}, (100.0, 101.0)), [])
        self.assertEqual(schema_cases.clock_diffs(state, state, (200.0, 201.0)), [])


if __name__ == "__main__":
    unittest.main()
