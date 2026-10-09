"""Bounded tests for the opt-in image inputs and runtime probe."""
from __future__ import annotations

import importlib.util
import json
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
                     ".github/workflows/rust.yml", "plugin/hooks/session-start.sh"):
            with self.subTest(path=path):
                self.assertTrue(gate.needs_image([path]))
        self.assertFalse(gate.needs_image(["README.md", "docs/guide/configuration.md",
                                          "pseudolife_memory/service.py", "rust/shim/src/main.rs"]))

    def test_runtime_contract_is_explicit_and_default_compose_is_not_used(self):
        dockerfile = (ROOT / "ops/Dockerfile.daemon-rust").read_text()
        for item in ("USER root", 'EXPOSE 8765', 'VOLUME ["/data"]',
                     "HF_HUB_OFFLINE=1", "CUDA_VISIBLE_DEVICES=-1", "ORT_DYLIB_PATH=",
                     "PSEUDOLIFE_DAEMON_ONNX_DIR=", "HEALTHCHECK", "--locked", "-j 3"):
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
