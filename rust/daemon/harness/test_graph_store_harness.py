"""Rejecting controls for the store harness's semantic clock comparison."""
import copy
from contextlib import ExitStack
import importlib.abc
import os
import json
from pathlib import Path
import runpy
import tempfile
import sys
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
        native = graph_store.pg.name(graph_store.pg.PREFIX + str(os.getpid() * 10 + 2))

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
                ("graph_store.ReadFixture", {"return_value": MagicMock(
                    apply=MagicMock(return_value={"value": None}))}),
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


class IndependentFixtures(unittest.TestCase):
    def test_seed_and_replay_import_without_product(self):
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, *args):
                if fullname == "pseudolife_memory" or fullname.startswith("pseudolife_memory."):
                    raise AssertionError("product imported: " + fullname)

        saved = {n: m for n, m in sys.modules.items() if n == "graph_fixtures" or n == "pseudolife_memory"
                 or n.startswith("pseudolife_memory.")}
        guard = Block()
        sys.meta_path.insert(0, guard)
        for name in saved:
            del sys.modules[name]
        try:
            # Execute the module body again: a cached graph_store import
            # would hide a new top-level dependency from the blocker.
            replay = runpy.run_path(graph_store.__file__)
            import graph_fixtures
            with patch("graph_fixtures.connect") as connection, patch("graph_fixtures.ensure_schema"):
                graph_fixtures.seed("disposable fixture")
                self.assertGreater(connection.return_value.__enter__.return_value.execute.call_count, 20)
            with patch.object(replay["pg"], "create", side_effect=RuntimeError("reached disposable setup")), \
                    patch.object(replay["pg"], "drop"):
                with self.assertRaisesRegex(RuntimeError, "reached disposable setup"):
                    replay["run"](Path("unused"), "golden")
        finally:
            sys.meta_path.remove(guard)
            sys.modules.pop("graph_fixtures", None)
            sys.modules.update(saved)

    def test_seed_guards_target_before_open_and_connection_before_writes(self):
        import graph_fixtures as fixture
        with patch.object(fixture, "refuse_production_database", side_effect=ValueError("refused")), \
                patch.object(fixture.psycopg, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "refused"):
                fixture.seed("postgresql://localhost/pseudolife_memory")
            connect.assert_not_called()
        with patch.object(fixture, "refuse_production_database"), \
                patch.object(fixture, "assert_disposable_database", side_effect=ValueError("server refused")), \
                patch.object(fixture.psycopg, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "server refused"):
                fixture.seed("postgresql://localhost/pl_cf_w3h_fixture")
            connect.return_value.execute.assert_not_called()
            connect.return_value.close.assert_called_once()

    def test_collation_changes_only_the_recorded_inverse_relation(self):
        from graph_fixtures import ReadFixture
        golden = json.loads(graph_store.GOLDEN.with_name("graph-read.json").read_text(encoding="utf-8"))
        fixture = object.__new__(ReadFixture)
        fixture.conn = MagicMock()
        for relation in ("aa", "a-z"):
            fixture.conn.execute.return_value.fetchone.return_value = (relation,)
            for index in range(158, 164):
                want = golden["cases"][index]["response"]
                expected = copy.deepcopy(want)
                for edge in expected["value"]["edges"]:
                    if edge.get("via") == ["inverse:z"]:
                        edge["relation"] = relation
                got = fixture.subgraph(golden["operations"][index], want)
                self.assertEqual(got, expected)
                self.assertEqual(want, golden["cases"][index]["response"])


class CandidateRecording(unittest.TestCase):
    def drive(self, path, write=False, cases=None, requests=None):
        case = {"index": 0, "op": "fixture", "response": {"value": 2}, "state_sha256": "candidate", "diffs": []}
        results = {}
        with patch("graph_store.GOLDEN", path), \
                patch("graph_store.operations", return_value=requests or [{"op": "fixture"}]), \
                patch("graph_store.run", return_value=[case] if cases is None else cases) as run, \
                patch("candidate_record.provenance", return_value={"candidate_sha256": "fixture", "commit": "head", "rust_tree": "clean"}):
            code = graph_store.record_candidate(Path("unused"), "store", write, results)
        return code, results, run

    def test_dry_run_then_write_then_unchanged_round_trip(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "graph-store.json"
            before = '{"operations": [], "cases": [], "normalizers": "retained"}'
            path.write_text(before)
            code, result, run = self.drive(path)
            self.assertEqual(code, 1)
            self.assertEqual(result["recording"]["changed"], ["store"])
            self.assertEqual(path.read_text(), before)
            run.assert_called_once_with(Path("unused"), "record", row="store", metadata=result)
            code, result, _ = self.drive(path, write=True)
            self.assertEqual(code, 0)
            recorded = json.loads(path.read_text())
            self.assertEqual(recorded["normalizers"], "retained")
            self.assertEqual(recorded["recorded_from_candidate"]["store"]["candidate_sha256"], "fixture")
            self.assertEqual(recorded["cases"][0]["response"], {"value": 2})
            self.assertEqual(self.drive(path)[0], 0)

    def test_mutant_refuses_before_launch(self):
        with patch.dict(os.environ, {"PSEUDOLIFE_DAEMON_MUTANT": "graph-alias"}):
            code, result, run = self.drive(Path("unused"), write=True)
        self.assertEqual(code, 1)
        self.assertIn("PSEUDOLIFE_DAEMON_MUTANT", result["failure"])
        run.assert_not_called()

    def test_recording_uses_replay_equivalence_without_hiding_missing_edges(self):
        golden = json.loads(graph_store.GOLDEN.with_name("graph-read.json").read_text(encoding="utf-8"))
        request = golden["operations"][30]
        recorded = copy.deepcopy(golden["cases"][30])
        recorded["index"] = 0
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "graph-store.json"
            path.write_text(json.dumps({"operations": [request], "cases": [recorded]}))
            before = path.read_bytes()
            equivalent = copy.deepcopy(recorded)
            equivalent["response"]["value"].reverse()
            code, result, _ = self.drive(path, cases=[equivalent], requests=[request])
            self.assertEqual(code, 0)
            self.assertEqual(result["recording"]["unchanged"], ["store"])
            self.assertEqual(path.read_bytes(), before)
            changed = copy.deepcopy(recorded)
            changed["response"]["value"].pop()
            code, result, _ = self.drive(path, cases=[changed], requests=[request])
            self.assertEqual(code, 1)
            self.assertEqual(result["recording"]["changed"], ["store"])
            self.assertEqual(path.read_bytes(), before)

    def test_recording_failures_never_write(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "graph-store.json"
            for cases in ([], [{"response": {"value": "is deferred in this candidate"}, "diffs": []}],
                          [{"response": {}, "diffs": ["bad state"]}],
                          [{"response": {"value": home}, "diffs": []}]):
                path.write_text('{"operations": [], "cases": []}')
                before = path.read_bytes()
                self.assertEqual(self.drive(path, write=True, cases=cases)[0], 1)
                self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
