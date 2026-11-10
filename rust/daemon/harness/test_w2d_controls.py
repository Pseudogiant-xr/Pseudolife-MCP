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
import w2d_cortex_check as cortex


class RerankerAvailabilityTests(unittest.TestCase):
    def test_requested_reranking_cannot_silently_fall_back(self):
        log = "Cross-encoder reranker failed to load (checksum mismatch) — disabling reranking."
        for case in [search.case("REST explicit", "/api/search?q=x&rerank=true"),
                     search.case("trace", "/api/trace?q=x&rerank=1"),
                     search.mcp_case("MCP", {"query": "x", "rerank": True})]:
            with self.subTest(case=case["name"]), self.assertRaisesRegex(RuntimeError, "requested reranking.*checksum mismatch"):
                search.require_requested_reranker([case], [({}, {"status": 200})], {"rust": log})

    def test_default_search_does_not_request_reranking(self):
        log = "Cross-encoder reranker cache unavailable (missing) — disabling."
        cases = [search.case("default", "/api/search?q=x"),
                 search.mcp_case("MCP default", {"query": "x"})]
        search.require_requested_reranker(cases, [({}, {"status": 200})] * 2, {"rust": log})

    def test_inherited_and_explicit_reranking_follow_scenario_config(self):
        log = "Cross-encoder reranker cache unavailable (missing) — disabling."
        for enabled in (False, True):
            scenario = search.Scenario()
            scenario.config_yaml = f"memory:\n  reranker:\n    enabled: {str(enabled).lower()}\n"
            inherited = [search.case("absent", "/api/search?q=x"),
                         search.mcp_case("MCP absent", {"query": "x"}),
                         search.mcp_case("MCP null", {"query": "x", "rerank": None})]
            inherited += [search.case(value, f"/api/search?q=x&rerank={value}")
                          for value in ("", "null", "auto")]
            for case in inherited:
                with self.subTest(enabled=enabled, case=case["name"]):
                    if enabled:
                        with self.assertRaisesRegex(RuntimeError, "requested reranking"):
                            search.require_requested_reranker([case], [({}, {"status": 200})], {"rust": log}, scenario)
                    else:
                        search.require_requested_reranker([case], [({}, {"status": 200})], {"rust": log}, scenario)
            off = [search.case("off", "/api/search?q=x&rerank=false"),
                   search.mcp_case("MCP off", {"query": "x", "rerank": False})]
            search.require_requested_reranker(off, [({}, {"status": 200})] * 2, {"rust": log}, scenario)

    def test_explicitly_disabled_or_refused_requests_are_not_rerank_proof(self):
        log = "Cross-encoder reranker cache unavailable (missing) — disabling."
        cases = [search.case("off", "/api/search?q=x&rerank=false"),
                 search.mcp_case("MCP off", {"query": "x", "rerank": False})]
        search.require_requested_reranker(cases, [({}, {"status": 200})] * 2, {"rust": log})
        search.require_requested_reranker([search.case("unauth", "/api/search?q=x")],
                                         [({}, {"status": 401})], {"rust": log})

    # The Python oracle's own line (reranker.py) in its daemon's log format,
    # offline with no cross-encoder in HF_HOME: transformers' reason spans two lines.
    PYTHON_OFFLINE = (
        "[2026-11-10 12:00:00,000] pseudolife_memory.memory.reranker WARNING: Cross-encoder reranker failed to load "
        "(We couldn't connect to 'https://huggingface.co' to load the files, and couldn't "
        "find them in the cached files.\n"
        "Check your internet connection or see how to run the library in offline mode at "
        "'https://huggingface.co/docs/transformers/installation#offline-mode'.) — disabling. "
        "Memory will fall back to bi-encoder scoring.\n")
    LOADED = "[2026-11-10 12:00:00,000] pseudolife_memory.memory.reranker INFO: Cross-encoder reranker loaded.\n"

    def test_a_degraded_python_oracle_refuses_requested_reranking(self):
        cases = [search.case("REST explicit", "/api/search?q=x&rerank=true"),
                 search.case("trace", "/api/trace?q=x&rerank=1"),
                 search.mcp_case("MCP", {"query": "x", "rerank": True})]
        for case in cases:
            with self.subTest(case=case["name"]), self.assertRaisesRegex(
                    RuntimeError, r"(?s)requested reranking but the Python oracle disabled it"
                                  r".*couldn't connect.*HF_HOME.*Dockerfile\.mirror-runner"):
                search.require_requested_reranker([case], [({"status": 200}, {"status": 200})],
                                                  {"python": self.PYTHON_OFFLINE, "rust": ""})

    def test_either_side_degraded_refuses_and_names_that_side(self):
        rust = "Cross-encoder reranker cache unavailable (missing) — disabling."
        case = search.case("REST explicit", "/api/search?q=x&rerank=true")
        for logs, side in (({"python": self.LOADED, "rust": rust}, "Rust daemon"),
                           ({"python": self.PYTHON_OFFLINE, "rust": ""}, "Python oracle")):
            with self.subTest(side=side), self.assertRaisesRegex(
                    RuntimeError, f"requested reranking but the {side} disabled it"):
                search.require_requested_reranker([case], [({"status": 200}, {"status": 200})], logs)
        with self.assertRaisesRegex(RuntimeError, "PSEUDOLIFE_DAEMON_RERANK_DIR"):
            search.require_requested_reranker([case], [({"status": 200}, {"status": 200})],
                                              {"python": self.LOADED, "rust": rust})

    def test_both_loaded_and_unrequested_reranking_pass(self):
        requested = [search.case("REST explicit", "/api/search?q=x&rerank=true"),
                     search.mcp_case("MCP", {"query": "x", "rerank": True})]
        search.require_requested_reranker(requested, [({"status": 200}, {"status": 200})] * 2,
                                          {"python": self.LOADED, "rust": ""})
        unrequested = [search.case("default", "/api/search?q=x"),
                       search.case("off", "/api/search?q=x&rerank=false"),
                       search.mcp_case("MCP off", {"query": "x", "rerank": False})]
        search.require_requested_reranker(unrequested, [({"status": 200}, {"status": 200})] * 3,
                                          {"python": self.PYTHON_OFFLINE, "rust": ""})
        # The oracle refused the call itself: no ranking to compare, no proof claimed.
        search.require_requested_reranker(requested[:1], [({"status": 401}, {"status": 200})],
                                          {"python": self.PYTHON_OFFLINE, "rust": ""})


class Scenario:
    require = {"with_entries": 1}


class MutantQualification(unittest.TestCase):
    def test_cortex_requires_one_result_per_oracle_case(self):
        cases = [{"query": "first", "entries": []}, {"query": "second", "entries": []}]
        state = {"rows": {}}
        for count in (1, 2, 3):
            with (self.subTest(results=count),
                  patch.object(sys, "argv", ["w2d_cortex_check.py", "--mode", "default"]),
                  patch.dict(cortex.os.environ, {"CARGO_TARGET_DIR": "unused"}),
                  patch.object(cortex.tempfile, "mkdtemp", return_value="unused"),
                  patch.object(cortex, "seed", return_value=(0, [])),
                  patch.object(cortex, "create", return_value="fixture"),
                  patch.object(cortex, "drop"),
                  patch.object(cortex, "run_python", return_value=(cases, state, state)),
                  patch.object(cortex, "run_rust", return_value=([{"entries": []}] * count, state, state)),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(cortex.main(), 0 if count == len(cases) else 1)

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
            "columns": ["query_text", "params"],
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
