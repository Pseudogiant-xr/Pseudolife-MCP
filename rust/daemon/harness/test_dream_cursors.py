"""Small, database-free checks of the dream differential comparator."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dream_cursors import compare_step, golden_state, normalize_secret, normalize_state, scenarios
import dream_cursors


def state():
    return {"catalog": {"tables": [["entries"]]}, "rows": {
        "public.entries": {"columns": ["id", "dream_state"],
                           "rows": [[1, "pending"], [2, "pending"]]},
        "public.meta": {"columns": ["key", "value"],
                        "rows": [["cortex_dream_cursor", 10.0]]},
    }}


class DreamHarnessTests(unittest.TestCase):
    def test_seed_setup_passes_payload_without_materializing_it(self):
        fixture = {"meta": {"dream_ack_secret_v1": "synthetic-fixture-value"}}
        with tempfile.TemporaryDirectory(prefix="pl-w3d-seed-") as directory:
            home = Path(directory)
            with patch("dream_cursors.subprocess.run") as run:
                run.return_value.returncode = 0
                dream_cursors.seed_template("fixture-dsn", fixture, home)
            self.assertEqual(list(home.iterdir()), [])
            self.assertEqual(run.call_args.args[0][-2:], ["--seed", "-"])
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), fixture)

    def test_rejects_output_order_source_signature_and_numeric_mutations(self):
        response = {"entries": [{"db_id": 1, "source": "notes"},
                                {"db_id": 2, "source": "notes"}], "count": 2}
        for mutated in (
            {**response, "entries": list(reversed(response["entries"]))},
            {**response, "entries": response["entries"][:1]},
            {"error": "invalid_dream_commit_token"},
            {**response, "count": 2.0},
        ):
            with self.subTest(mutated=mutated):
                self.assertTrue(compare_step(response, mutated, state(), state())["diffs"])

    def test_rejects_ack_all_rewind_skip_write_and_explicit_classification(self):
        for change in ("all", "rewind", "skip", "classify"):
            expected, actual = state(), state()
            expected["rows"]["public.entries"]["rows"][0][1] = "acknowledged"
            if change == "all":
                actual["rows"]["public.entries"]["rows"] = [[1, "acknowledged"], [2, "acknowledged"]]
            elif change == "rewind":
                actual = copy.deepcopy(expected)
                actual["rows"]["public.meta"]["rows"][0][1] = 5.0
            elif change == "classify":
                actual["rows"]["public.entries"]["rows"][0][1] = "legacy-covered"
            result = compare_step({"ok": True}, {"ok": True}, expected, actual)
            with self.subTest(change=change):
                self.assertTrue(result["db_diffs"])

    def test_catalog_and_failed_action_are_checked(self):
        actual = state()
        actual["catalog"]["tables"].append(["unexpected"])
        self.assertTrue(compare_step({"error": "rejected"}, {"error": "rejected"},
                                     state(), actual)["db_diffs"])

    def test_generated_secret_validates_shape_without_masking_corruption(self):
        self.assertEqual(normalize_secret("11" * 32), normalize_secret("22" * 32))
        for invalid in ("bad", "g" * 64, None, 123):
            self.assertNotEqual(normalize_secret(invalid), normalize_secret("11" * 32))

    def test_seed_clock_normalization_does_not_hide_rewrites(self):
        before = state()
        before["rows"]["public.relations"] = {
            "columns": ["id", "created_at"], "rows": [[1, 123.0]]}
        changed = copy.deepcopy(before)
        changed["rows"]["public.relations"]["rows"][0][1] = 456.0
        self.assertNotEqual(normalize_state(before, before), normalize_state(changed, before))

    def test_scenarios_include_restart_failures_and_cross_arm_tokens(self):
        actions = [a for scenario in scenarios() for a in scenario["actions"]]
        self.assertTrue(any(a["op"] == "restart" for a in actions))
        self.assertTrue(any(a["op"] == "fault" for a in actions))
        self.assertTrue(any(a.get("token_from") for a in actions))

    def test_golden_catalog_invariant_rejects_changed_catalog_and_rows(self):
        before = state()
        expected = golden_state(before, before)
        for target in ("catalog", "rows"):
            changed = copy.deepcopy(before)
            if target == "catalog":
                changed["catalog"]["tables"].append(["unexpected"])
            else:
                changed["rows"]["public.entries"]["rows"][0][1] = "acknowledged"
            self.assertNotEqual(golden_state(changed, before), expected)


if __name__ == "__main__":
    unittest.main()
