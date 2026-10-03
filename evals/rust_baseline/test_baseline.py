"""CPU-light harness self-checks; no model imports or database mutation."""
import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from . import daemon, daemon_child
from .ci import elapsed, summarize
from .common import child_environment, controls, distribution, lease_gate, provenance, write_result
from .daemon import _daemon_command, corpus


TOY_HEALTH_SERVER = """
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body = json.dumps({'status': 'ok', 'baseline_instance': {
            'nonce': os.environ.get('PSEUDOLIFE_BASELINE_NONCE'), 'pid': os.getpid(),
            'runtime': {'python': 'toy-fixture'}}}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
HTTPServer(('127.0.0.1', int(os.environ['PSEUDOLIFE_MCP_PORT'])), Handler).serve_forever()
"""


@contextmanager
def unrelated_listener():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def launcher_directory():
    temporary = tempfile.TemporaryDirectory(prefix="baseline-launch-test-")
    root = Path(temporary.name).resolve()
    assert root.parent == Path(tempfile.gettempdir()).resolve()
    try:
        yield str(root)
    finally:
        # Windows can retain a just-closed log briefly after the Job is empty;
        # process/subtree assertions precede this filesystem-only retry.
        for attempt in range(50):
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.1)


class BaselineTests(unittest.TestCase):
    def toy_ready(self, directory):
        import httpx
        with patch.object(daemon, "_daemon_command", return_value=[sys.executable, "-c", TOY_HEALTH_SERVER]):
            with daemon.launched_daemon("synthetic-not-connected", directory) as (process, url, cleanup):
                self.assertTrue(cleanup["readiness_identity_verified"])
                self.assertEqual(cleanup["actual_child_runtime"], {"python": "toy-fixture"})
                self.assertNotIn("nonce", json.dumps(cleanup))
                identity = httpx.get(url + "/health", trust_env=False, timeout=1).json()["baseline_instance"]
                nonce = identity["nonce"]
                self.assertEqual(process.verified_runtime_pid, identity["pid"])
                self.assertTrue(process.owns_runtime_pid(process.verified_runtime_pid))
                from .common import memory_tree
                self.assertGreater(memory_tree(process.verified_runtime_pid)["rss_bytes"], 0)
                if os.name == "nt" and sys.prefix != sys.base_prefix:
                    self.assertNotEqual(process.pid, process.verified_runtime_pid)
            self.assertTrue(cleanup["daemon_stopped"])
            self.assertTrue(cleanup["children_stopped"])
            return nonce

    def spawned_child(self, root, *, exit_parent):
        pid_file = root / "toy-child-pid"
        command = ("import pathlib,subprocess,sys,time; "
                   "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)'],"
                   "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                   f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
                   + ("" if exit_parent else "time.sleep(15)"))
        return pid_file, [sys.executable, "-c", command]

    def assert_child_stopped(self, pid_file):
        import psutil
        self.assertTrue(pid_file.exists(), "toy child did not start")
        try:
            child = psutil.Process(int(pid_file.read_text()))
        except psutil.NoSuchProcess:
            return
        try:
            alive = child.is_running() and child.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            alive = False
        try:
            self.assertFalse(alive, "owned descendant survived")
        finally:
            # A watched RED must not leave its synthetic child behind.
            if alive:
                try:
                    child.kill()
                    child.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass

    def test_unrelated_http_200_cannot_satisfy_readiness(self):
        with launcher_directory() as directory, unrelated_listener() as port:
            with patch.object(daemon, "free_port", return_value=port), patch.object(
                    daemon, "_daemon_command", return_value=[sys.executable, "-c", "import time; time.sleep(15)"]):
                with self.assertRaisesRegex(RuntimeError, "readiness identity"):
                    with daemon.launched_daemon("synthetic-not-connected", directory):
                        pass

    def test_launcher_accepts_only_owned_healthy_instance(self):
        with launcher_directory() as directory:
            self.assertTrue(self.toy_ready(directory) != self.toy_ready(directory))

    def test_wrong_nonce_pid_or_health_status_is_rejected(self):
        variants = [TOY_HEALTH_SERVER.replace("os.environ.get('PSEUDOLIFE_BASELINE_NONCE')", "'wrong-instance'"),
                    TOY_HEALTH_SERVER.replace("os.getpid()", "os.getpid()+1"),
                    TOY_HEALTH_SERVER.replace("os.getpid()", "2147483647"),
                    TOY_HEALTH_SERVER.replace("os.getpid()", "True"),
                    TOY_HEALTH_SERVER.replace("os.getpid()", "-1"),
                    TOY_HEALTH_SERVER.replace("os.getpid()", str(os.getpid())),
                    TOY_HEALTH_SERVER.replace("'status': 'ok'", "'status': 'degraded'")]
        for index, command in enumerate(variants):
            with self.subTest(variant=index), launcher_directory() as directory:
                with patch.object(daemon, "_daemon_command", return_value=[sys.executable, "-c", command]):
                    with self.assertRaisesRegex(RuntimeError, "readiness identity"):
                        with daemon.launched_daemon("synthetic-not-connected", directory):
                            pass

    def test_parent_exit_still_cleans_owned_descendant(self):
        with launcher_directory() as directory:
            root = Path(directory)
            pid_file, command = self.spawned_child(root, exit_parent=True)
            try:
                with patch.object(daemon, "_daemon_command", return_value=command):
                    with self.assertRaisesRegex(RuntimeError, "exited during startup"):
                        with daemon.launched_daemon("synthetic-not-connected", directory):
                            pass
            finally:
                self.assert_child_stopped(pid_file)
            self.toy_ready(directory)

    def test_deadline_cleans_owned_descendant(self):
        with launcher_directory() as directory:
            root = Path(directory)
            pid_file, command = self.spawned_child(root, exit_parent=False)
            try:
                with patch.object(daemon, "_daemon_command", return_value=command):
                    with self.assertRaisesRegex(RuntimeError, "startup deadline"):
                        with daemon.launched_daemon("synthetic-not-connected", directory, startup_timeout=0.5):
                            pass
            finally:
                self.assert_child_stopped(pid_file)
            self.toy_ready(directory)

    def test_identity_hook_preserves_real_health_and_refuses_unsupported_source(self):
        from types import SimpleNamespace
        source = SimpleNamespace(_build_health_payload=lambda: {"status": "degraded", "schema": 52})
        runtime = {"python": "fixture", "source_origin_matches_selected_root": True}
        daemon_child.install_readiness_identity(source, "synthetic-instance", runtime)
        payload = source._build_health_payload()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["schema"], 52)
        self.assertTrue(payload["baseline_instance"]["nonce"] == "synthetic-instance")
        self.assertEqual(payload["baseline_instance"]["pid"], os.getpid())
        self.assertEqual(payload["baseline_instance"]["runtime"], runtime)
        with self.assertRaisesRegex(RuntimeError, "readiness identity hook unavailable"):
            daemon_child.install_readiness_identity(SimpleNamespace(), "synthetic-instance")

    def test_private_directory_retries_only_transient_filesystem_cleanup(self):
        with patch.object(daemon.tempfile, "TemporaryDirectory") as factory, patch.object(daemon.time, "sleep"):
            temporary = factory.return_value
            temporary.name = "disposable-fixture"
            temporary.cleanup.side_effect = [PermissionError("retained log"), None]
            with daemon.private_directory() as path:
                self.assertEqual(path, "disposable-fixture")
            self.assertEqual(temporary.cleanup.call_count, 2)
            temporary.cleanup.reset_mock()
            temporary.cleanup.side_effect = PermissionError("persistent lock")
            with self.assertRaises(PermissionError):
                with daemon.private_directory():
                    pass
            self.assertEqual(temporary.cleanup.call_count, 50)

    def test_one_repeat_has_no_noise_floor(self):
        self.assertIsNone(controls([[1, 2, 3]])["noise_floor_abs"])
        self.assertFalse(controls([[1, 2, 3]])["available"])
        self.assertEqual(controls([[1, 2, 3], [4, 5, 6]])["noise_floor_abs"], 3)

    def test_quantiles_use_nearest_rank_and_true_median(self):
        stats = distribution(list(range(1, 21)))
        self.assertEqual(stats["p50"], 10)
        self.assertEqual(stats["median"], 10.5)
        self.assertEqual(stats["p95"], 19)

    def test_parallel_lanes_do_not_sum_into_wall(self):
        runs = [{"head_sha": "fixed", "created_to_last_job_s": 13, "job_span_s": 10,
                 "jobs": [{"name": "a", "wall_s": 10}, {"name": "b", "wall_s": 8}]}]
        summary = summarize(runs)
        self.assertEqual(summary["job_span_s"]["p50"], 10)
        self.assertEqual(summary["lanes_s"]["b"]["p50"], 8)
        self.assertFalse(summary["noise_floor_available"])

    def test_negative_hosted_duration_is_refused(self):
        with self.assertRaises(ValueError):
            elapsed("2026-10-03T01:00:02Z", "2026-10-03T01:00:01Z")

    def test_fixed_seed_is_reproducible(self):
        self.assertEqual(corpus(8), corpus(8))
        self.assertNotEqual(corpus(8), corpus(8, seed=0))
        self.assertEqual(len({r["text"] for r in corpus(128)}), 128)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "pseudolife_memory"
            package.mkdir()
            (package / "__init__.py").write_text("ORACLE_MARKER = 999\n", encoding="utf-8")
            probe = root / "probe.py"
            probe.write_text("import pseudolife_memory; print(pseudolife_memory.ORACLE_MARKER)\n", encoding="utf-8")
            command = _daemon_command(root)
            self.assertIn("evals.rust_baseline.daemon_child", command[-1])
            from evals.rust_port.provenance import module_command
            command = module_command("probe", root)
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "999")

    def test_child_configuration_does_not_inherit_live_identity(self):
        with patch.dict(os.environ, {"PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://fixture/live",
                                     "_PSEUDOLIFE_PRODUCTION_DB": "live",
                                     "PSEUDOLIFE_MCP_TOKEN_FILE": "live-file",
                                     "PSEUDOLIFE_CODEX_SERVER_TOKEN": "live-key",
                                     "CLAUDE_SESSION_ID": "live-session"}):
            env = child_environment("disposable")
        self.assertNotIn("PSEUDOLIFE_MCP_DATABASE_URL", env)
        self.assertNotIn("PSEUDOLIFE_MCP_TOKEN_FILE", env)
        self.assertNotIn("PSEUDOLIFE_CODEX_SERVER_TOKEN", env)
        self.assertNotIn("CLAUDE_SESSION_ID", env)
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "-1")
        self.assertEqual(env["_PSEUDOLIFE_PRODUCTION_DB"], "live")

    def test_offline_clearance_requires_free_local_and_no_known_holder(self):
        report = {"local": {"state": "free"}, "board": {"available": False}, "held": False}
        stamp = "2026-10-03T01:00:00Z"
        def checked(payload, code=0):
            return patch("subprocess.run", return_value=subprocess.CompletedProcess([], code, json.dumps(payload), ""))
        with checked(report):
            with self.assertRaisesRegex(RuntimeError, "without independent"):
                lease_gate()
            resource = lease_gate(offline_resource_checked_at=stamp)
            self.assertTrue(resource["offline_resource_clearance_used"])
            self.assertEqual(resource["offline_resource_checked_at_utc"], stamp)
            self.assertIsNone(resource["lead_board_checked_free_at_utc"])
            self.assertFalse(resource["cli_board_available"])
            self.assertTrue(lease_gate(stamp)["local_lock_free"])
        for code, local, available, held in [(1, "free", False, False), (0, "held", False, False),
                                              (0, "free", True, True), (1, "held", True, True)]:
            with self.subTest(code=code, local=local, available=available, held=held):
                payload = {"local": {"state": local}, "board": {"available": available}, "held": held}
                with checked(payload, code), self.assertRaisesRegex(RuntimeError, "held"):
                    lease_gate(offline_resource_checked_at=stamp)
        report["board"]["available"] = True
        with checked(report):
            self.assertFalse(lease_gate(offline_resource_checked_at=stamp)["offline_resource_clearance_used"])
            self.assertTrue(lease_gate()["cli_board_available"])
        with checked({}), self.assertRaisesRegex(RuntimeError, "malformed"):
            lease_gate(offline_resource_checked_at=stamp)
        with self.assertRaises(ValueError):
            lease_gate(stamp, offline_resource_checked_at=stamp)

    def test_result_does_not_overwrite_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            write_result(path, {"status": "smoke"})
            with self.assertRaises(FileExistsError):
                write_result(path, {"status": "changed"})
            self.assertEqual(json.loads(path.read_text()), {"status": "smoke"})
            schema = Path(directory) / "pseudolife_memory" / "storage" / "schema.py"
            schema.parent.mkdir(parents=True)
            schema.write_text("SCHEMA_META_VERSION = 999\n", encoding="utf-8")
            check_output = subprocess.check_output
            def selected_git(command, **kwargs):
                if command[:2] == ["git", "rev-parse"]:
                    self.assertEqual(Path(kwargs["cwd"]), Path(directory).resolve())
                    return "fixture-head\n"
                if command[:2] == ["git", "status"]:
                    return ""
                return check_output(command, **kwargs)
            with patch("subprocess.check_output", side_effect=selected_git):
                metadata = provenance(source_root=directory)
            self.assertEqual(metadata["source_head"], "fixture-head")
            self.assertEqual(metadata["source_schema"], 999)
            self.assertIn("processes.py", metadata["process_helper_sha256"])
            self.assertIn("provenance.py", metadata["runtime_provenance_sha256"])
            self.assertIn("python", metadata["parent_runtime"])
            self.assertNotIn("dependencies", metadata)
            if os.name == "nt":
                self.assertIn("codex_doorbell.py", metadata["process_helper_sha256"])
            self.assertNotIn(directory, json.dumps(metadata))


if __name__ == "__main__":
    unittest.main()
