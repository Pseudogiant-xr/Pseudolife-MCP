"""Offline safety and verdict checks; no daemon, credentials or sockets."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shadow_compare as shadow


class ShadowCompareTests(unittest.TestCase):
    def test_generated_queries_supply_required_tool_arguments(self):
        tools = {t["name"]: t for t in map(json.loads, (shadow.HERE.parent / "src/mcp/tools.jsonl").read_text().splitlines())}
        for case in shadow.query_cases("fixture query"):
            if "tool" in case:
                required = tools[case["tool"]]["inputSchema"].get("required", [])
                self.assertTrue(set(required) <= case["args"].keys(), case["tool"])

    def test_write_tools_and_routes_are_refused_before_network(self):
        cases = [
            {"tool": "memory_store", "args": {"text": "bad"}},
            {"tool": "memory_message", "args": {"action": "receive"}},
            {"path": "/api/hook/session-start"},
            {"path": "//elsewhere/api/search"},
            {"path": "/api/search?redirect=/api/delete"},
            {"path": "/api/search", "method": "POST"},
            {"tool": "memory_search", "path": "/api/delete"},
        ]
        endpoint = shadow.Endpoint("http://localhost")
        with patch.object(endpoint, "request") as request:
            for case in cases:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    endpoint.read(case)
            request.assert_not_called()

    def test_entire_corpus_is_checked_before_initialization(self):
        with patch.object(shadow.Endpoint, "initialize") as initialize:
            with self.assertRaises(ValueError):
                shadow.compare(shadow.Endpoint("http://localhost"), shadow.Endpoint("http://localhost"),
                               [{"name": "ok", "tool": "memory_search"}, {"tool": "memory_store"}])
            initialize.assert_not_called()

    def test_ids_are_canonicalized_but_never_erased(self):
        diffs = shadow.differences({"id": "42"}, {"id": 42})
        self.assertEqual(diffs[0]["classification"], "comparator_noise")
        self.assertEqual(shadow.differences({"id": 42}, {"id": 43})[0]["classification"], "new_bug")
        self.assertEqual(shadow.differences({"id": True}, {"id": 1})[0]["kind"], "type")

    def test_tolerances_do_not_hide_content_types_or_rank_changes(self):
        for key, left, right, classification in [
            ("score", .7, .7001, "comparator_noise"),
            ("score", .7, .701, "new_bug"),
            ("confidence", .7, .7001, "new_bug"),
            ("created_at", 100.0, 102.0, "comparator_noise"),
            ("created_at", None, 100.0, "new_bug"),
            ("created_at", 100.0, 103.0, "new_bug"),
            ("count", 1, 2, "new_bug"),
            ("rank", 1, 2, "new_bug"),
            ("access_count", 1, 2, "comparator_noise"),
        ]:
            with self.subTest(key=key, right=right):
                self.assertEqual(shadow.differences({key: left}, {key: right})[0]["classification"], classification)
        self.assertTrue(shadow.differences([{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 1}]))
        self.assertEqual(shadow.differences({"id": 1}, {})[0]["kind"], "missing_key")

    def test_redirect_never_forwards_bearer(self):
        with patch("http.client.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 302
            response.read.return_value = b""
            response.getheader.return_value = None
            endpoint = shadow.Endpoint("http://localhost", "synthetic-token")
            with self.assertRaises(ValueError):
                endpoint.request("GET", "/health")
            self.assertEqual(connection.call_count, 1)

    def test_shadow_required_and_equal_errors_fail(self):
        class Fake:
            def __init__(self, read_only=True):
                self.read_only = read_only

            def initialize(self):
                return {"version": "fixture"}

            def request(self, *args):
                return 200, {"ready": True, "read_only": self.read_only, "version": "fixture", "build": {"git_sha": "fixture"}}

            def read(self, case):
                return 500, {"error": "failed"}

        with self.assertRaises(ValueError):
            shadow.compare(Fake(), Fake(False), [])
        summary = shadow.compare(Fake(), Fake(), [{"name": "one", "tool": "memory_graph"}])
        self.assertEqual(summary["classifications"], {"new_bug": 1})
        self.assertEqual(summary["differences_by_kind"], {"read_error": 1})
        self.assertIn("fixture", shadow.digest(summary))
        json.dumps(summary)


if __name__ == "__main__":
    unittest.main()
