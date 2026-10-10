"""Rejecting controls for the store harness's semantic clock comparison."""
import copy
from contextlib import ExitStack
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import graph_store
from graph_store import ClockState, catalog_shape, digest, response_diff, strict_json, wall_time


def state(clock=1.0):
    return {"catalog": {"sequences": [["public", "entities_id_seq", "bigint", 1, 1, 1]]},
            "rows": {"public.entities": {"columns": ["id", "created_at"],
                                          "rows": [[1, clock]]}}}


class ClockControls(unittest.TestCase):
    def test_collation_gate_accepts_only_recorded_locale_aliases(self):
        recorded = {"lc_collate": "en_US.utf8", "lc_ctype": "en_US.utf8"}
        self.assertTrue(graph_store.same_locale(recorded, recorded))
        self.assertTrue(graph_store.same_locale(recorded,
                        {"lc_collate": "en_US.UTF-8", "lc_ctype": "en_US.UTF-8"}))
        self.assertFalse(graph_store.same_locale(recorded,
                         {"lc_collate": "C.UTF-8", "lc_ctype": "en_US.utf8"}))
        self.assertFalse(graph_store.same_locale(recorded,
                         {"lc_collate": "en_US.utf8", "lc_ctype": "C.UTF-8"}))

    def test_collation_skip_has_declared_reason_and_count(self):
        with tempfile.TemporaryDirectory() as home:
            candidate = Path(home) / "candidate"
            candidate.write_bytes(b"fixture")
            report = Path(home) / "report.json"
            with patch("sys.argv", ["graph_store", "golden", "--row", "read",
                                    "--candidate", str(candidate), "--out", str(report)]), \
                    patch("graph_store.subprocess.check_output", return_value="head"), \
                    patch("graph_store.run", return_value=[{"index": i, "diffs": [],
                          **({"skip_reason": "recorded en_US.utf8 differs from C.UTF-8"}
                             if i >= 155 else {})} for i in range(164)]):
                self.assertEqual(graph_store.main(), 0)
            result = json.loads(report.read_text())
            self.assertEqual(result["summary"]["cases"], 155)
            self.assertEqual(result["summary"]["executed_cases"], 164)
            self.assertEqual(result["summary"]["skipped_indices"], list(range(155, 164)))
            self.assertIn("C.UTF-8", result["summary"]["skip_reason"])

    def test_required_recorded_collation_fails_closed(self):
        recorded = {"lc_collate": "en_US.utf8", "lc_ctype": "en_US.utf8"}
        golden = {"database_locale": recorded, "collation_cases": list(range(155, 164))}
        with patch.dict(os.environ, {"PL_GRAPH_REQUIRE_RECORDED_LOCALE": "1"}):
            self.assertEqual(graph_store.golden_collation_skips(golden, recorded), ([], None))
            with self.assertRaisesRegex(AssertionError, "recorded locale"):
                graph_store.golden_collation_skips(golden,
                    {"lc_collate": "C.UTF-8", "lc_ctype": "C.UTF-8"})

    def test_locale_fallback_rejects_unrelated_bank_mutation(self):
        initial = state()
        initial["rows"]["public.entities"]["columns"].append("display")
        initial["rows"]["public.entities"]["rows"][0].append("Node A")
        requests = [{"op": "fixture"}] * 164
        golden = {"operations": requests, "database_locale": {
                    "lc_collate": "C", "lc_ctype": "C"},
                  "collation_cases": list(range(155, 164)),
                  "cases": [{"response": {"value": None},
                             "state_sha256": digest(initial)}] * 164}
        current = [-1]
        process = MagicMock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.stdout.readline.return_value = '{"value":null}\n'
        process.stdin.write.side_effect = lambda _: current.__setitem__(0, current[0] + 1)
        native = graph_store.pg.PREFIX + str(os.getpid() * 10 + 2)

        def observed(url):
            result = copy.deepcopy(initial)
            if url == native and current[0] >= 155:
                result["rows"]["public.entities"]["rows"][0][-1] = "unrelated mutation"
            return result

        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = ("en_US.utf8", "en_US.utf8")
        with tempfile.TemporaryDirectory() as home, ExitStack() as patches:
            (Path(home) / "graph-read.json").write_text(json.dumps(golden), encoding="utf-8")
            patches.enter_context(patch.dict(os.environ, {"PL_GRAPH_REQUIRE_RECORDED_LOCALE": "0"}))
            for target, kwargs in [
                ("graph_store.GOLDEN", {"new": Path(home) / "graph-store.json"}),
                ("graph_store.operations", {"return_value": requests}),
                ("graph_store.pg.create", {"side_effect": lambda name, **_: name}),
                ("graph_store.pg.drop", {}), ("graph_store.seed", {}),
                ("psycopg.connect", {"return_value": connection}),
                ("pseudolife_memory.storage.postgres.PostgresStorage", {}),
                ("graph_store.oracle_call", {"return_value": {"value": None}}),
                ("graph_store.subprocess.Popen", {"return_value": process}),
                ("psutil.Process", {}), ("graph_store.dbstate.dump", {"side_effect": observed}),
            ]:
                patches.enter_context(patch(target, **kwargs))
            cases = graph_store.run(Path(home) / "candidate", "golden", row="read")
        self.assertTrue(all(case["diffs"] for case in cases[155:]))
        self.assertTrue(all(not case["diffs"] for case in cases[:155]))

    def test_mutant_observer_failures_fail_the_run(self):
        with tempfile.TemporaryDirectory() as home:
            candidate = Path(home) / "candidate"
            candidate.write_bytes(b"fixture")
            report = Path(home) / "report.json"
            with patch("sys.argv", ["graph_store", "mutants", "--candidate", str(candidate),
                                    "--out", str(report)]), \
                    patch("graph_store.subprocess.check_output", return_value="head"), \
                    patch("graph_store.run", side_effect=[[{"diffs": []}]] +
                          [AssertionError("broken observer")] * len(graph_store.MUTANTS)):
                self.assertEqual(graph_store.main(), 1)
            result = json.loads(report.read_text())
            self.assertTrue(all(value is None for value in result["mutants"].values()))
            self.assertEqual(set(result["mutant_failures"]), set(graph_store.MUTANTS))

    @unittest.skipUnless(os.name == "nt", "precise Windows clock API")
    def test_candidate_clock_does_not_use_coarse_python_clock(self):
        with patch("graph_store.time.time", return_value=1.0):
            self.assertGreater(wall_time(), 1.0)

    def test_unchanged_seed_value_stays_exact(self):
        clock = ClockState()
        clock.initialize(state())
        self.assertEqual(clock.state(state(), 0, (10, 11)), state())

    def test_changed_clock_requires_own_window(self):
        clock = ClockState()
        clock.initialize(state())
        with self.assertRaisesRegex(AssertionError, "outside operation window"):
            clock.state(state(9), 0, (10, 11))

    def test_changed_clock_cannot_compare_with_unchanged(self):
        clocks = [ClockState(), ClockState()]
        for clock in clocks:
            clock.initialize(state())
        changed = clocks[0].state(state(10), 0, (10, 11))
        unchanged = clocks[1].state(state(), 0, (10, 11))
        self.assertNotEqual(digest(changed), digest(unchanged))

    def test_equal_semantic_changes_compare_inside_each_window(self):
        clocks = [ClockState(), ClockState()]
        for clock in clocks:
            clock.initialize(state())
        a = clocks[0].state(state(10), 3, (10, 11))
        b = clocks[1].state(state(20), 3, (20, 21))
        self.assertEqual(digest(a), digest(b))

    def test_response_clock_must_equal_durable_value(self):
        clock = ClockState()
        clock.initialize(state())
        with self.assertRaisesRegex(AssertionError, "response disagrees"):
            clock.response({"id": 1, "created_at": 2})

    def test_nonfinite_clock_refuses(self):
        clock = ClockState()
        clock.initialize(state())
        with self.assertRaises(AssertionError):
            clock.state(state(float("nan")), 1, (10, 11))

    def test_portable_digest_keeps_sequence_advance(self):
        a, b = state(), state()
        b["catalog"]["sequences"][0][-1] = 2
        self.assertNotEqual(digest(a), digest(b))
        self.assertEqual(catalog_shape(a), catalog_shape(b))

    def test_catalog_shape_keeps_nonsequence_mutation(self):
        a, b = state(), state()
        b["catalog"]["extra"] = "unexpected table"
        self.assertNotEqual(catalog_shape(a), catalog_shape(b))

    def test_row_multiplicity_is_preserved(self):
        a, b = state(), copy.deepcopy(state())
        b["rows"]["public.entities"]["rows"].append([1, 1.0])
        self.assertNotEqual(digest(a), digest(b))

    def test_clock_scalar_type_is_not_erased(self):
        clock = ClockState()
        clock.initialize(state())
        with self.assertRaisesRegex(AssertionError, "response disagrees"):
            clock.response({"id": 1, "created_at": True})

    def test_response_list_scalar_types_stay_distinct(self):
        self.assertTrue(response_diff({"nodes": [{"id": 1}]}, {"nodes": [{"id": True}]}))

    def test_duplicate_keys_refuse(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            strict_json('{"value":1,"value":2}')

    def test_nonfinite_json_refuses(self):
        with self.assertRaisesRegex(ValueError, "non-finite"):
            strict_json('{"value":NaN}')


if __name__ == "__main__":
    unittest.main()
