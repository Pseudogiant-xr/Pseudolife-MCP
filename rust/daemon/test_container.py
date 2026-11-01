"""Bounded tests for the opt-in image inputs and runtime probe."""
from __future__ import annotations

import importlib.util
import json
import re
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
VERIFIED_GRAPH = "f5fdee679d0ffb98e1cb0bb178e8ee2b86739ab7ab035a3a9327761d5c6b1f0b"
DEFAULT_PATHS = ("ops/Dockerfile.daemon", "ops/docker-compose.yml", "ops/docker-compose.ghcr.yml",
                 "pseudolife_memory/compose/docker-compose.yml", "pseudolife_memory/compose/docker-compose.ghcr.yml",
                 "pseudolife_memory/update_cli.py", ".github/workflows/release.yml",
                 "ops/install.sh", "ops/install.ps1")


def require_python_defaults(contents, graph):
    # This is one cutover prerequisite, not authorization to switch defaults.
    if graph == VERIFIED_GRAPH:
        return
    for path, text in contents.items():
        assert not any(symbol in text for symbol in
                       ("Dockerfile.daemon-rust", "docker-compose.rust.yml", "w3j-daemon-rust")), path
    commands = [json.loads(line[4:]) for line in contents["ops/Dockerfile.daemon"].splitlines()
                if line.startswith("CMD ")]
    assert commands[-1] == ["python", "-m", "pseudolife_memory.cli", "serve"]


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContainerTests(unittest.TestCase):
    def test_build_context_excludes_cargo_outputs(self):
        patterns = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertTrue({"rust/target", "**/target"}.intersection(patterns))

    def test_unverified_graph_cannot_enter_production_selection(self):
        graph = load("provision_container_model").FILES["model.onnx"]
        contents = {path: (ROOT / path).read_text() for path in DEFAULT_PATHS}
        require_python_defaults(contents, graph)
        for path in DEFAULT_PATHS:
            counterfactual = dict(contents)
            counterfactual[path] += "\nimage: w3j-daemon-rust:prep\n"
            with self.subTest(path=path), self.assertRaises(AssertionError):
                require_python_defaults(counterfactual, graph)
        native_cmd = dict(contents)
        native_cmd["ops/Dockerfile.daemon"] = 'CMD ["pseudolife-daemon"]\n'
        with self.assertRaises(AssertionError):
            require_python_defaults(native_cmd, graph)
        require_python_defaults(counterfactual, VERIFIED_GRAPH)

    def test_image_gate_matches_inputs_and_skips_unrelated_changes(self):
        gate = load("container_inputs")
        for path in (".dockerignore", "rust/daemon/src/embed.rs", "rust/Cargo.toml", "rust/Cargo.lock",
                     "ops/Dockerfile.daemon-rust", "ops/docker-compose.rust.yml",
                     "ops/provision-daemon-models.sh",
                     ".github/workflows/rust.yml", "plugin/hooks/session-start.sh"):
            with self.subTest(path=path):
                self.assertTrue(gate.needs_image([path]))
        self.assertFalse(gate.needs_image(["README.md", "docs/guide/configuration.md",
                                          "pseudolife_memory/service.py", "rust/shim/src/main.rs"]))

    def test_runtime_contract_is_explicit_and_default_compose_is_not_used(self):
        dockerfile = (ROOT / "ops/Dockerfile.daemon-rust").read_text()
        for item in ("USER root", 'EXPOSE 8765', 'VOLUME ["/data"]',
                     "HF_HUB_OFFLINE=1", "CUDA_VISIBLE_DEVICES=-1", "ORT_DYLIB_PATH=",
                     "PSEUDOLIFE_DAEMON_ONNX_DIR=", "HEALTHCHECK", "--locked", "-j 3",
                     "provision pdfium", "COPY --from=builder /opt/pdfium /opt/pdfium",
                     "PSEUDOLIFE_DAEMON_PDFIUM_DIR=/opt/pdfium"):
            self.assertIn(item, dockerfile)
        compose = (ROOT / "ops/docker-compose.rust.yml").read_text()
        self.assertIn("name: w3j-container-prep", compose)
        self.assertIn("-p w3j-container-prep", compose)
        self.assertNotIn("internal: true", compose)
        self.assertIn("127.0.0.1:${W3J_RUST_PORT:-8766}:8765", compose)
        self.assertNotIn("pseudolife-mcp-bank", compose)
        self.assertNotIn("container_name:", compose)

    def test_provisioning_rejects_corrupted_download(self):
        import hashlib
        import io
        import tempfile
        from unittest.mock import patch
        provision = load("provision_container_model")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.onnx"
            with patch.object(provision.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
                with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                    provision.download("https://example.com/model", target, hashlib.sha256(b"good").hexdigest())
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".onnx.partial").exists())

    def test_preparation_cache_is_on_disk_and_provisioned_before_health_wait(self):
        compose = (ROOT / "ops/docker-compose.rust.yml").read_text()
        self.assertIn("PSEUDOLIFE_DAEMON_MODELS_DIR: /models", compose)
        self.assertIn("- models:/models", compose)
        self.assertIn("\nvolumes:\n  models: {}", compose)
        job = (ROOT / ".github/workflows/rust.yml").read_text().split(
            "\n  daemon-image:", 1)[1].split("\n  parity:", 1)[0]
        self.assertIn("uses: actions/cache@v4", job)
        self.assertIn("hashFiles('rust/daemon/src/model_provision.rs')", job)
        self.assertIn("device: ${W3J_MODELS_CACHE}", job)
        self.assertLess(job.index("run --rm --no-deps rust-daemon pseudolife-daemon provision models"),
                        job.index("up --no-build --wait --wait-timeout 180"))

    @unittest.skipIf(os.name == "nt", "the image entrypoint runs on Linux")
    def test_entrypoint_defers_existing_verification_and_respects_local_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = root / "calls"
            daemon = root / "pseudolife-daemon"
            daemon.write_text('#!/bin/sh\n'
                              'if [ "$*" = "provision models --if-missing" ]; then\n'
                              '  test -s "$TMPDIR/pseudolife-model-provisioning" || exit 8\n'
                              '  test "${FAIL:-0}" = 0 || exit 7\n'
                              'elif [ "$#" = 0 ]; then\n'
                              '  read phase started < "$TMPDIR/pseudolife-model-provisioning"\n'
                              '  test "$phase" = init || exit 9\n'
                              'else test ! -e "$TMPDIR/pseudolife-model-provisioning" || exit 9; fi\n'
                              'printf "%s\\n" "$*" >> "$CALLS"\n')
            daemon.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"], CALLS=str(calls), TMPDIR=str(root))
            env.pop("PSEUDOLIFE_DAEMON_ONNX_DIR", None)
            entrypoint = str(ROOT / "ops/provision-daemon-models.sh")
            for args, override, expected in [
                ([], None, ["provision models --if-missing", ""]),
                (["pseudolife-daemon"], "/local/model", [""]),
                (["pseudolife-daemon", "provision", "models"], None, ["provision models"]),
            ]:
                calls.unlink(missing_ok=True)
                env.pop("PSEUDOLIFE_DAEMON_ONNX_DIR", None)
                if override:
                    env["PSEUDOLIFE_DAEMON_ONNX_DIR"] = override
                subprocess.run(["sh", entrypoint, *args], env=env, check=True)
                self.assertEqual(calls.read_text().splitlines(), expected)
            env["FAIL"] = "1"
            result = subprocess.run(["sh", entrypoint], env=env)
            self.assertEqual(result.returncode, 7)
            self.assertFalse((root / "pseudolife-model-provisioning").exists())

    @unittest.skipIf(os.name == "nt", "the image healthcheck runs on Linux")
    def test_health_waits_for_cold_init_without_claiming_ready_during_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Virtual clock: 1799s downloading, then 89s initializing (longer
            # than five 15s retries). The check must not return success early.
            scripts = {
                "date": 'cat "$TMPDIR/now"',
                "curl": 'now=$(cat "$TMPDIR/now"); echo "$now" >> "$TMPDIR/probes"\n'
                        'if [ "$now" -ge "$READY_AT" ]; then echo \'{"ready":true}\'; '
                        'else echo \'{"ready":false}\'; fi',
                "sleep": 'now=$(cat "$TMPDIR/now")\n'
                         'if [ "$MODE" = cold ]; then\n'
                         '  case "$now" in\n'
                         '    100) echo "init 1899" > "$TMPDIR/pseudolife-model-provisioning"; next=1899 ;;\n'
                         '    1899) next=1975 ;;\n'
                         '    1975) next=1988 ;;\n'
                         '    *) exit 9 ;;\n'
                         '  esac\n'
                         'else next=190; fi\n'
                         'echo "$next" > "$TMPDIR/now"',
            }
            for name, script in scripts.items():
                command = root / name
                command.write_text("#!/bin/sh\n" + script + "\n")
                command.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       TMPDIR=str(root), READY_AT="1988", MODE="cold")
            marker = root / "pseudolife-model-provisioning"
            now = root / "now"
            now.write_text("100\n")
            def check():
                return subprocess.run(["sh", str(ROOT / "ops/provision-daemon-models.sh"), "--healthcheck"],
                                      env=env, capture_output=True, timeout=10).returncode
            self.assertNotEqual(check(), 0)  # HTTP 200 is not readiness.
            (root / "probes").unlink()
            marker.write_text("provision 100\n")
            self.assertEqual(check(), 0)
            self.assertEqual((root / "probes").read_text().splitlines(), ["100", "1899", "1975", "1988"])
            self.assertFalse(marker.exists())  # Only actual readiness clears startup.
            # Once ready, a new failure gets no startup grace.
            env["READY_AT"] = "9999"
            self.assertNotEqual(check(), 0)
            env["MODE"] = "warm"
            now.write_text("100\n")
            marker.write_text("init 100\n")
            self.assertNotEqual(check(), 0)
            self.assertEqual(now.read_text().strip(), "190")  # 90s, not 30m.
            marker.write_text("provision 100\n")
            now.write_text("1900\n")
            self.assertNotEqual(check(), 0)  # A stuck download has a bound.
            dockerfile = (ROOT / "ops/Dockerfile.daemon-rust").read_text()
            health = next(line for line in dockerfile.splitlines() if line.startswith("HEALTHCHECK "))
            options = dict(re.findall(r"--([a-z-]+)=(\S+)", health))
            self.assertEqual(options["start-period"], "0s")  # No early success is needed to end grace.
            self.assertGreaterEqual(int(options["timeout"].removesuffix("m")) * 60, 1800 + 90)
            self.assertEqual(90 + (int(options["retries"]) - 1) * int(options["interval"].removesuffix("s")), 150)

    @unittest.skipUnless(os.name != "nt" and os.environ.get("PSEUDOLIFE_TEST_MODEL_BINARY"),
                         "opt-in Linux integration: model binary/root and initialized disposable DSN")
    def test_default_embedder_stores_and_searches_without_a_reranker(self):
        import psycopg.conninfo
        sys.path.insert(0, str(ROOT / "rust/daemon/harness"))
        import w2d_search as search
        dsn = os.environ["PSEUDOLIFE_TEST_MODEL_DATABASE_URL"]
        self.assertTrue(psycopg.conninfo.conninfo_to_dict(dsn)["dbname"].startswith(("s1_", "pl_cf_")),
                        "the model startup test requires a disposable database")
        with tempfile.TemporaryDirectory() as temporary:
            home = search.daemons.make_home(Path(temporary), "without-reranker", search.config_for(search.Scenario()))
            env = search.daemons.base_env(home, dict(search.common_env(dsn),
                ORT_DYLIB_PATH=os.environ["ORT_DYLIB_PATH"],
                PSEUDOLIFE_DAEMON_ONNX_DIR=os.environ["PSEUDOLIFE_TEST_MODEL_ROOT"],
                PSEUDOLIFE_DAEMON_ORT_THREADS="3"))
            self.assertNotIn("PSEUDOLIFE_DAEMON_RERANK_DIR", env)
            daemon = search.daemons.rust_daemon(Path(os.environ["PSEUDOLIFE_TEST_MODEL_BINARY"]),
                                               home, search.daemons.free_port(), env)
            try:
                daemon.start(wait_s=180)
                cause = "Cross-encoder reranker failed to load (model artifact is missing or unsafe:"
                deadline = time.monotonic() + 10
                while cause not in daemon.log.read_text() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(daemon.log.read_text().count(cause), 1, daemon.log.read_text())  # Before any store/search.
                text = "Default pinned embedding works without the optional reranker."
                stored = search.mcp_call(daemon.port, search.mcp_case("store", {"text": text}, tool="memory_store"), None)
                self.assertEqual(stored["status"], 200, stored)
                found = search.mcp_call(daemon.port, search.mcp_case("search", {"query": text, "rerank": True}), None)
                self.assertEqual(found["status"], 200, found)
                self.assertIn(text, json.dumps(found))
                self.assertEqual(daemon.log.read_text().count(cause), 1)  # Reuses the disabled state.
                health = search.call(daemon.port, "GET", "/health", [])
                self.assertTrue(health["json"]["ready"])
                self.assertEqual(health["json"]["embedder"]["backend"], "onnx")
            finally:
                daemon.stop()

    def test_provisioned_layout_matches_the_default_loader(self):
        import tempfile
        from unittest.mock import patch
        provision = load("provision_container_model")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "qwen3"

            def downloaded(url, destination, expected):
                self.assertTrue(destination.parent.is_dir())
                self.assertEqual(len(expected), 64)
                name = destination.relative_to(root).as_posix()
                if name.startswith("onnx/"):
                    name = name.removeprefix("onnx/")
                base = provision.METADATA_BASE if name in provision.METADATA_FILES else provision.BASE
                self.assertEqual(url, base + "/" + name)
                destination.write_bytes(b"verified fixture")

            with patch.object(provision, "download", side_effect=downloaded), \
                 patch("sys.argv", ["provision_container_model.py", str(root)]):
                provision.main()
            for name in ("onnx/model.onnx", "onnx/model.onnx_data", "tokenizer.json",
                         "config.json", "tokenizer_config.json", "modules.json", "1_Pooling/config.json"):
                self.assertEqual((root / name).read_bytes(), b"verified fixture")
            defaults = (ROOT / "rust/daemon/src/config.rs").read_text()
            self.assertIn('onnx_file_name: "onnx/model.onnx".to_string()', defaults)

    def test_metadata_pins_match_the_recorded_qwen_model(self):
        provision = load("provision_container_model")
        receipt = json.loads((ROOT / "rust/daemon/harness/results/embedding-20261010.json").read_text())
        recorded = next(row["identities"] for row in receipt["runs"] if row["model"] == "Qwen")
        for name in ("modules.json", "1_Pooling/config.json", "config.json", "tokenizer_config.json"):
            self.assertEqual(provision.FILES[name], recorded[name])

    def test_export_and_harness_pins_match_the_native_manifest(self):
        source = (ROOT / "rust/daemon/src/model_provision.rs").read_text()
        rows = re.findall(r'artifact!\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"'
                          r'\s*,\s*"([^"]+)"\s*,\s*"([0-9a-f]{64})"\s*,\s*(\d+)\s*\)', source)
        self.assertEqual(len(rows), 12)
        pins = {path: digest for path, _, _, _, digest, _ in rows}
        self.assertTrue(all(int(size) > 0 for *_, size in rows))
        legacy = load("provision_container_model")
        for name, digest in legacy.FILES.items():
            path = "embedding/" + ("onnx/" if name.startswith("model.onnx") else "") + name
            self.assertEqual(digest, pins[path], path)
        spec = importlib.util.spec_from_file_location("embedding_pin_check", ROOT / "rust/daemon/harness/embedding.py")
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        current = harness.QWEN_FP32_IDENTITIES[1]
        self.assertEqual(set(current), {"onnx/model.onnx", "onnx/model.onnx_data", "tokenizer.json"})
        for name, digest in current.items():
            self.assertEqual(digest, pins["embedding/" + name], name)

    def test_startup_policy_refuses_missing_or_different_metadata(self):
        probe = load("container_probe")
        expected = {"pooling": "last-token", "padding": "right", "normalize": True}
        log = "rust-daemon-1 | embedding-readiness: " + json.dumps(expected) + "\n"
        self.assertEqual(probe.verify_policy(log), expected)
        with self.assertRaisesRegex(RuntimeError, "embedding readiness"):
            probe.verify_policy("daemon: listening\n")
        for key, value in (("pooling", "mean"), ("padding", "left"), ("normalize", False)):
            changed = dict(expected, **{key: value})
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "embedding readiness"):
                probe.verify_policy("embedding-readiness: " + json.dumps(changed) + "\n")

    def test_probe_requires_authenticated_search_and_refused_open_search(self):
        probe = load("container_probe")
        token = "disposable-test-token"
        state = {"refuse": True, "count": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                authenticated = self.headers.get("Authorization") == "Bearer " + token
                if self.path == "/health":
                    status, body = 200, {"status": "ok", "db": "ok", "embedder": {"backend": "onnx", "device": "cpu"}}
                elif authenticated:
                    state["count"] += 1
                    status, body = 200, {"entries": [], "count": 0, "query": probe.QUERY}
                else:
                    status, body = (401 if state["refuse"] else 200), {}
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:" + str(server.server_port)
            self.assertEqual(probe.verify(url, token, timeout=2)["unauthenticated_status"], 401)
            self.assertEqual(state["count"], 1)
            state["refuse"] = False
            with self.assertRaisesRegex(RuntimeError, "unauthenticated"):
                probe.verify(url, token, timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
