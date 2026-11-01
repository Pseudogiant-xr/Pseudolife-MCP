"""Safety/verdict checks; synthetic credentials and loopback-only DB failures."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shadow_compare as shadow


class ShadowCompareTests(unittest.TestCase):
    def assert_database_setup_failure(self, dsn):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PSEUDOLIFE_", "PG"))}
        env["SHADOW_TEST_DATABASE_URL"] = dsn
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "summary.json"
            result = subprocess.run(
                [sys.executable, str(shadow.HERE / "shadow_compare.py"),
                 "--python-url", "http://127.0.0.1:1", "--rust-url", "http://127.0.0.1:1",
                 "--database-url-env", "SHADOW_TEST_DATABASE_URL", "--out", str(output)],
                env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())
        self.assertEqual(result.stdout, "")
        self.assertIn("database setup failed", result.stderr)
        self.assertIn("DSN redacted", result.stderr)
        self.assertIn("SELECT credentials", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(dsn not in result.stderr and "shadow-secret-marker" not in result.stderr,
                        "database diagnostic leaked the synthetic DSN")

    def test_malformed_database_dsn_is_sanitized_setup_failure(self):
        self.assert_database_setup_failure("password=shadow-secret-marker invalid-shadow-option=value")

    def test_refused_database_connection_is_sanitized_setup_failure(self):
        # Reserve a loopback port without listening: libpq gets refusal from
        # the OS, with no database, daemon or external endpoint involved.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self.assert_database_setup_failure(
                f"host=127.0.0.1 port={reserved.getsockname()[1]} dbname=shadow_fixture "
                "user=shadow_fixture password=shadow-secret-marker connect_timeout=1")

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

    def test_every_replay_request_has_a_reserved_session_tag(self):
        with patch("http.client.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{}'
            response.getheader.return_value = None
            endpoint = shadow.Endpoint("http://localhost")
            for method, path in (("GET", "/api/search"), ("POST", "/mcp")):
                endpoint.request(method, path)
                headers = connection.return_value.request.call_args.args[3]
                self.assertTrue(headers["X-PL-Session"].startswith(shadow.REPLAY_SESSION_PREFIX))
                self.assertEqual(headers["X-PL-Session"], endpoint.replay_session)

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

    def test_compare_preserves_replay_opt_out_and_existing_tag(self):
        python = shadow.Endpoint("http://localhost", replay=False)
        rust = shadow.Endpoint("http://localhost")
        tag = rust.replay_session
        with patch.object(shadow.Endpoint, "initialize", return_value={}), patch.object(
                shadow.Endpoint, "request", return_value=(200, {"ready": True, "read_only": True})):
            shadow.compare(python, rust, [])
            shadow.compare(python, rust, [])
        self.assertIsNone(python.replay_session)
        self.assertEqual(rust.replay_session, tag)


if __name__ == "__main__":
    unittest.main()
