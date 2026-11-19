"""Small, database-free checks of the dream differential comparator."""
from __future__ import annotations

import copy
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dream_cursors import compare_step, golden_state, normalize_secret, normalize_state, scenarios
import dream_cursors
from rust.harness_support.import_blocker import cold_harness_imports


def state():
    return {"catalog": {"tables": [["entries"]]}, "rows": {
        "public.entries": {"columns": ["id", "dream_state"],
                           "rows": [[1, "pending"], [2, "pending"]]},
        "public.meta": {"columns": ["key", "value"],
                        "rows": [["cortex_dream_cursor", 10.0]]},
    }}


class DreamHarnessTests(unittest.TestCase):
    def test_seed_reaches_connection_with_product_imports_blocked(self):
        class Connected(Exception):
            pass

        with cold_harness_imports(), patch("psycopg.connect", side_effect=Connected):
            importlib.import_module("dream_cursors")
            from dream_cursor_oracle import seed
            with self.assertRaises(Connected):
                seed("postgresql://fixture@127.0.0.1:5434/pl_cf_w3d_probe", {})

    def test_seed_setup_passes_payload_without_materializing_it(self):
        fixture = {"meta": {"dream_ack_secret_v1": "synthetic-fixture-value"}}
        with tempfile.TemporaryDirectory(prefix="pl-w3d-seed-") as directory:
            home = Path(directory)
            with patch("dream_cursors.subprocess.run") as run:
                run.return_value.returncode = 0
                dream_cursors.seed_template("postgresql://fixture@127.0.0.1:5434/pl_cf_w3d_probe", fixture, home)
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

    def test_candidate_recording_dry_run_write_and_replay_comparison(self):
        steps = [{"response": {"count": 1}, "db_state": state()}]
        changed = copy.deepcopy(steps)
        changed[0]["response"]["count"] = 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dream-cursors-fixture.json"
            path.write_text(json.dumps({"scenario": "fixture", "steps": steps}), encoding="utf-8")
            original = path.read_bytes()
            with patch.object(dream_cursors.candidate_record, "provenance",
                              return_value={"commit": "fixture", "rust_tree": "clean"}):
                self.assertEqual(dream_cursors.record_steps(path, "fixture", steps, Path("candidate"), False), 0)
                self.assertEqual(dream_cursors.record_steps(path, "fixture", changed, Path("candidate"), False), 1)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(dream_cursors.record_steps(path, "fixture", changed, Path("candidate"), True), 0)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["recorded_from_candidate"]["fixture"]["commit"], "fixture")
            self.assertEqual(compare_step(written["steps"][0]["response"], changed[0]["response"],
                                          written["steps"][0]["db_state"], changed[0]["db_state"]),
                             {"diffs": [], "db_diffs": []})

    def test_recording_refuses_mutants_empty_output_and_deferrals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dream-cursors-fixture.json"
            for steps in ([], [{"response": {"error": "not_implemented"}}]):
                self.assertEqual(dream_cursors.record_steps(path, "fixture", steps, Path("candidate"), True), 1)
                self.assertFalse(path.exists())
            with patch.dict("os.environ", {"PSEUDOLIFE_DAEMON_MUTANT": "dream-cursor-rewind"}):
                self.assertEqual(dream_cursors.record_steps(path, "fixture", [{"response": {}}],
                                                            Path("candidate"), True), 1)
                with patch.object(dream_cursors, "run_scenario") as run:
                    with patch.object(sys, "argv", ["dream_cursors.py", "golden", "--rust-bin", "candidate",
                                                   "--record-candidate"]):
                        with self.assertRaises(SystemExit):
                            dream_cursors.main()
                    run.assert_not_called()
            self.assertFalse(path.exists())

    def test_recorder_uses_candidate_tokens_and_replay_state(self):
        scenario = {"name": "fixture", "fixture": {}, "generated_secret": False,
                    "actions": [{"op": "issue", "save": "token"},
                                {"op": "verify", "token_from": "token"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "dream-cursors-fixture.json"
            path.write_text(json.dumps({"steps": [
                {"response": {"token": "old"}, "db_state": state()},
                {"response": {"valid": True}, "db_state": state()}]}), encoding="utf-8")
            with patch.object(dream_cursors, "pg"), patch.object(dream_cursors, "seed_template"), \
                    patch.object(dream_cursors.dbstate, "dump", return_value=state()), \
                    patch.object(dream_cursors, "Adapter") as adapter, \
                    patch.object(dream_cursors, "record_steps", return_value=0) as record:
                proc = adapter.return_value.start.return_value
                proc.call.side_effect = [{"token": "candidate"}, {"valid": True}]
                dream_cursors.run_scenario(scenario, Path("candidate"), root, "golden", root, True)
                self.assertEqual(proc.call.call_args_list[1].args[0]["token"], "candidate")
                observed = record.call_args.args[2]
                self.assertEqual(observed[0]["response"], {"token": "candidate"})
                self.assertEqual(observed[0]["db_state"], golden_state(normalize_state(state(), state()), state()))
                proc.stop.assert_called_once()

    def test_fixture_guard_refuses_wrong_target_before_connecting(self):
        from dream_fixtures import connection
        from rust.harness_support.database_guard import ProductionDatabaseError
        for dsn in ("dbname=pseudolife_memory", "dbname=unrelated", "dbname=pl_cf_w3d_"):
            with patch("psycopg.connect") as connect:
                with self.assertRaises((ValueError, ProductionDatabaseError)):
                    with connection(dsn):
                        self.fail("unsafe fixture admitted")
                connect.assert_not_called()

    def test_fixture_guard_checks_the_connected_database_before_writing(self):
        from dream_fixtures import connection
        from rust.harness_support.database_guard import ProductionDatabaseError
        for reached in ("pseudolife_memory", "pl_cf_w3d_different"):
            with patch("psycopg.connect") as connect:
                conn = connect.return_value.__enter__.return_value
                conn.execute.return_value.fetchone.return_value = (reached,)
                with self.assertRaises((ValueError, ProductionDatabaseError)):
                    with connection("dbname=pl_cf_w3d_probe"):
                        self.fail("wrong database admitted")
                conn.execute.assert_called_once_with("SELECT current_database()")


if __name__ == "__main__":
    unittest.main()
