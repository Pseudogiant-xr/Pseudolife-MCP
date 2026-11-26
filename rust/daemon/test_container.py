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
DEFAULT_PATHS = ("ops/Dockerfile.daemon", "ops/docker-compose.yml", "ops/docker-compose.ghcr.yml",
                 "pseudolife_memory/compose/docker-compose.yml", "pseudolife_memory/compose/docker-compose.ghcr.yml",
                 "pseudolife_memory/update_cli.py", ".github/workflows/release.yml",
                 "ops/install.sh", "ops/install.ps1")


def native_manifest():
    source = (ROOT / "rust/daemon/src/model_provision.rs").read_text(encoding="utf-8")
    return re.findall(r'artifact!\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"'
                      r'\s*,\s*"([^"]+)"\s*,\s*"([0-9a-f]{64})"\s*,\s*(\d+)\s*\)', source)


def verified_graph():
    # #1114 admitted the public export with measured parity. The compiled
    # provisioner's manifest is the identity, not the historical export hash.
    graphs = [row[4] for row in native_manifest() if row[0] == "embedding/onnx/model.onnx"]
    assert len(graphs) == 1, "native manifest must pin exactly one embedding graph"
    return graphs[0]


def require_python_defaults(contents, graph):
    # This is one cutover prerequisite, not authorization to switch defaults.
    for path, text in contents.items():
        if path == ".github/workflows/release.yml" and graph == verified_graph() and text:
            # Only the opt-in job may refer to the admitted Rust image.
            # Keep this clean-runner guard stdlib-only; parsed job conditions
            # are checked under suite dependencies in test_rust_release_image.py.
            text = re.sub(r"(?ms)^  rust-image:[ \t]*\n.*?(?=^  [A-Za-z0-9_-]+:|\Z)", "", text)
        assert not any(symbol in text for symbol in
                       ("Dockerfile.daemon-rust", "docker-compose.rust.yml", "w3j-daemon-rust",
                        "pseudolife-daemon-rust")), path
    commands = [json.loads(line[4:]) for line in contents["ops/Dockerfile.daemon"].splitlines()
                if line.startswith("CMD ")]
    assert commands[-1] == ["python", "-m", "pseudolife_memory.cli", "serve"]


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContainerTests(unittest.TestCase):
    def test_native_compile_inputs_exist_in_the_image_builder(self):
        self.assertEqual(load("container_inputs").missing_compile_inputs(ROOT), [])

    def test_missing_compile_input_guard_rejects_host_only_python_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ops").mkdir()
            (root / "ops/Dockerfile.daemon-rust").write_text(
                "FROM rust AS builder\nCOPY rust /build/rust\nFROM debian\n", encoding="utf-8")
            source = root / "rust/shim/src"
            source.mkdir(parents=True)
            (source / "probe.rs").write_text('include_str!("../../../pseudolife_memory/schema.py");', encoding="utf-8")
            self.assertEqual(len(load("container_inputs").missing_compile_inputs(root)), 1)

    def test_guard_import_needs_only_the_standard_library(self):
        subprocess.run([sys.executable, "-S", "-c",
                        "import runpy, sys; runpy.run_path(sys.argv[1], run_name='container_guard_import')",
                        str(Path(__file__).resolve())], check=True, capture_output=True, text=True, timeout=10)

    def test_build_context_excludes_cargo_outputs(self):
        patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertTrue({"rust/target", "**/target"}.intersection(patterns))

    def test_unverified_graph_cannot_enter_production_selection(self):
        graph = load("provision_container_model").FILES["model.onnx"]
        contents = {path: (ROOT / path).read_text(encoding="utf-8") for path in DEFAULT_PATHS}
        self.assertEqual(graph, verified_graph())
        require_python_defaults(contents, graph)
        # Isolate each forbidden input: the real workflow now legitimately
        # references the verified opt-in image, so it is not a negative fixture.
        contents = {path: "" for path in DEFAULT_PATHS}
        contents["ops/Dockerfile.daemon"] = 'CMD ["python", "-m", "pseudolife_memory.cli", "serve"]\n'
        unverified = "0" * 64
        require_python_defaults(contents, unverified)
        for path in DEFAULT_PATHS:
            counterfactual = dict(contents)
            counterfactual[path] += "\nimage: w3j-daemon-rust:prep\n"
            with self.subTest(path=path), self.assertRaises(AssertionError):
                require_python_defaults(counterfactual, unverified)
        native_cmd = dict(contents)
        native_cmd["ops/Dockerfile.daemon"] = 'CMD ["pseudolife-daemon"]\n'
        with self.assertRaises(AssertionError):
            require_python_defaults(native_cmd, unverified)
        with self.assertRaises(AssertionError):
            require_python_defaults(counterfactual, verified_graph())

    def test_verified_graph_comes_from_the_native_manifest(self):
        from unittest.mock import patch
        replacement = [("embedding/onnx/model.onnx", "repo", "revision", "model.onnx", "a" * 64, "1")]
        with patch.dict(verified_graph.__globals__, native_manifest=lambda: replacement):
            self.assertEqual(verified_graph(), "a" * 64)

    def test_verified_graph_does_not_allow_switching_real_python_defaults(self):
        contents = {path: (ROOT / path).read_text(encoding="utf-8") for path in DEFAULT_PATHS}
        require_python_defaults(contents, verified_graph())
        for path, replacement in (
            ("ops/docker-compose.yml", re.sub(r"(?m)^    image: pseudolife-daemon:.*$",
                "    image: ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.21.0", contents["ops/docker-compose.yml"])),
            ("ops/Dockerfile.daemon", contents["ops/Dockerfile.daemon"].replace(
                'CMD ["python", "-m", "pseudolife_memory.cli", "serve"]', 'CMD ["pseudolife-daemon"]')),
            (".github/workflows/release.yml", contents[".github/workflows/release.yml"].replace(
                "file: ops/Dockerfile.daemon\n", "file: ops/Dockerfile.daemon-rust\n")),
        ):
            with self.subTest(path=path), self.assertRaises(AssertionError):
                require_python_defaults(dict(contents, **{path: replacement}), verified_graph())

    def test_prompt_context_does_not_allow_the_whole_directory(self):
        self.assertNotIn("!evals/prompts", (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    def test_image_gate_matches_inputs_and_skips_unrelated_changes(self):
        gate = load("container_inputs")
        for path in (".dockerignore", "rust/daemon/src/embed.rs", "rust/Cargo.toml", "rust/Cargo.lock",
                     "evals/prompts/assistant_facts_provenance.txt",
                     "evals/prompts/sonnet_extractor_v5.md",
                     "rust/shim/src/main.rs", "rust/shim/Cargo.toml",
                     "rust/shared/pg_runtime.rs",
                     "ops/Dockerfile.daemon-rust", "ops/docker-compose.rust.yml",
                     "ops/provision-daemon-models.sh",
                     ".github/workflows/rust.yml", "plugin/hooks/session-start.sh",
                     "frontend/dist/index.html", "frontend/dist/vendor/README.md"):
            with self.subTest(path=path):
                self.assertTrue(gate.needs_image([path]))
        self.assertFalse(gate.needs_image(["README.md", "docs/guide/configuration.md",
                                          "pseudolife_memory/service.py", "frontend/src/main.ts"]))

    def test_image_ships_the_native_operator_cli(self):
        dockerfile = (ROOT / "ops/Dockerfile.daemon-rust").read_text(encoding="utf-8")
        self.assertIn("--bin pseudolife-stdio", dockerfile)
        self.assertIn("cp rust/target/release/pseudolife-stdio /pseudolife-mcp", dockerfile)
        self.assertIn("COPY --from=builder /pseudolife-mcp /usr/local/bin/pseudolife-mcp", dockerfile)
        self.assertIn("COPY evals/prompts/assistant_facts_provenance.txt /build/evals/prompts/assistant_facts_provenance.txt", dockerfile)
        self.assertIn("!evals/prompts/assistant_facts_provenance.txt", (ROOT / ".dockerignore").read_text(encoding="utf-8"))
        self.assertIn("COPY evals/prompts/sonnet_extractor_v5.md /build/evals/prompts/sonnet_extractor_v5.md", dockerfile)
        self.assertIn("!evals/prompts/sonnet_extractor_v5.md", (ROOT / ".dockerignore").read_text(encoding="utf-8"))
        job = (ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8").split(
            "\n  daemon-image:", 1)[1].split("\n  parity:", 1)[0]
        self.assertIn("docker run --rm --network none w3j-daemon-rust:prep pseudolife-mcp version", job)

    def test_image_ships_the_console_and_selects_its_asset_directory(self):
        dockerfile = (ROOT / "ops/Dockerfile.daemon-rust").read_text(encoding="utf-8")
        self.assertIn("COPY frontend/dist /app/static", dockerfile.splitlines())
        self.assertIn("PSEUDOLIFE_DAEMON_STATIC_DIR=/app/static", dockerfile)
        self.assertTrue((ROOT / "frontend/dist/index.html").is_file())

    def test_python_image_installs_and_serves_the_console(self):
        dockerfile = (ROOT / "ops/Dockerfile.daemon").read_text(encoding="utf-8")
        self.assertIn("--mount=type=bind,source=setup.py,target=/app/setup.py", dockerfile)
        self.assertIn("--mount=type=bind,source=frontend/dist,target=/app/frontend/dist", dockerfile)
        self.assertIn("COPY frontend/dist /app/pseudolife_memory/web/static", dockerfile.splitlines())

    def test_image_probe_rejects_changed_console_bytes(self):
        import io
        from unittest.mock import patch

        probe = load("container_probe")
        base = "http://127.0.0.1:8766"
        dist = ROOT / "frontend/dist"
        for changed in ("/ui/", "/ui/vendor/README.md"):
            def urlopen(url, *, timeout):
                served = url.removeprefix(base + "/ui/")
                body = b"wrong Console bytes" if url == base + changed else (dist / (served or "index.html")).read_bytes()
                reply = io.BytesIO(body)
                reply.status = 200
                return reply

            with (self.subTest(path=changed),
                  patch.dict(os.environ, {"W3J_RUST_TOKEN": "disposable-test-token"}),
                  patch.object(sys, "argv", ["container_probe.py"]),
                  patch.object(probe, "verify", return_value={"query_inference": True}),
                  patch.object(probe.urllib.request, "urlopen", side_effect=urlopen),
                  self.assertRaisesRegex(RuntimeError, "Console content differs at " + re.escape(changed))):
                probe.main()

    def test_release_overlay_requires_a_pull_and_an_explicit_version(self):
        overlay = (ROOT / "ops/docker-compose.rust-ghcr.yml").read_text(encoding="utf-8")
        self.assertIn("\n    pull_policy: always\n", overlay)
        self.assertIn("\n    build: !reset null\n", overlay)
        self.assertIn("${PSEUDOLIFE_IMAGE_TAG:?", overlay)
        self.assertIn("\n      org.pseudolife.engine: rust\n", overlay)
        # The models stay outside /data, which every backup archives.
        self.assertIn("\n      PSEUDOLIFE_DAEMON_MODELS_DIR: /models\n", overlay)
        self.assertIn("\n      - pseudolife_rust_models:/models\n", overlay)
        self.assertIn("\nvolumes:\n  pseudolife_rust_models: {}\n", overlay)

    def test_runtime_contract_is_explicit_and_default_compose_is_not_used(self):
        dockerfile = (ROOT / "ops/Dockerfile.daemon-rust").read_text(encoding="utf-8")
        for item in ("USER root", 'EXPOSE 8765', 'VOLUME ["/data"]', "ORT_DYLIB_PATH=",
                     "PSEUDOLIFE_DAEMON_ONNX_DIR=", "HEALTHCHECK", "--locked", "-j 3",
                     "provision pdfium", "COPY --from=builder /opt/pdfium /opt/pdfium",
                     "PSEUDOLIFE_DAEMON_PDFIUM_DIR=/opt/pdfium"):
            self.assertIn(item, dockerfile)
        compose = (ROOT / "ops/docker-compose.rust.yml").read_text(encoding="utf-8")
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
        compose = (ROOT / "ops/docker-compose.rust.yml").read_text(encoding="utf-8")
        self.assertIn("PSEUDOLIFE_DAEMON_MODELS_DIR: /models", compose)
        self.assertIn("- models:/models", compose)
        self.assertIn("\nvolumes:\n  models: {}", compose)
        job = (ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8").split(
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
            # Never the root bootstrap here, even when the suite runs as root.
            (root / "id").write_text("#!/bin/sh\necho 10001\n")
            (root / "id").chmod(0o755)
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
                self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), expected)
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
            self.assertEqual((root / "probes").read_text(encoding="utf-8").splitlines(), ["100", "1899", "1975", "1988"])
            self.assertFalse(marker.exists())  # Only actual readiness clears startup.
            # Once ready, a new failure gets no startup grace.
            env["READY_AT"] = "9999"
            self.assertNotEqual(check(), 0)
            env["MODE"] = "warm"
            now.write_text("100\n")
            marker.write_text("init 100\n")
            self.assertNotEqual(check(), 0)
            self.assertEqual(now.read_text(encoding="utf-8").strip(), "190")  # 90s, not 30m.
            marker.write_text("provision 100\n")
            now.write_text("1900\n")
            self.assertNotEqual(check(), 0)  # A stuck download has a bound.
            dockerfile = (ROOT / "ops/Dockerfile.daemon-rust").read_text(encoding="utf-8")
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
                while cause not in daemon.log.read_text(encoding="utf-8") and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(daemon.log.read_text(encoding="utf-8").count(cause), 1, daemon.log.read_text(encoding="utf-8"))  # Before any store/search.
                text = "Default pinned embedding works without the optional reranker."
                stored = search.mcp_call(daemon.port, search.mcp_case("store", {"text": text}, tool="memory_store"), None)
                self.assertEqual(stored["status"], 200, stored)
                found = search.mcp_call(daemon.port, search.mcp_case("search", {"query": text, "rerank": True}), None)
                self.assertEqual(found["status"], 200, found)
                self.assertIn(text, json.dumps(found))
                self.assertEqual(daemon.log.read_text(encoding="utf-8").count(cause), 1)  # Reuses the disabled state.
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
            defaults = (ROOT / "rust/daemon/src/config.rs").read_text(encoding="utf-8")
            self.assertIn('onnx_file_name: "onnx/model.onnx".to_string()', defaults)

    def test_metadata_pins_match_the_recorded_qwen_model(self):
        provision = load("provision_container_model")
        receipt = json.loads((ROOT / "rust/daemon/harness/results/embedding-20261010.json").read_text(encoding="utf-8"))
        recorded = next(row["identities"] for row in receipt["runs"] if row["model"] == "Qwen")
        for name in ("modules.json", "1_Pooling/config.json", "config.json", "tokenizer_config.json"):
            self.assertEqual(provision.FILES[name], recorded[name])

    def test_export_and_harness_pins_match_the_native_manifest(self):
        rows = native_manifest()
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


class RuntimeAccountTest(unittest.TestCase):
    """The Rust daemon runs as the Python image's account with no capabilities."""

    def test_default_engine_drops_root_after_the_ownership_bootstrap(self):
        # Text checks only: the CI step runs this file on the standard library.
        def service(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            start = lines.index("  pseudolife-daemon:")
            block = []
            for line in lines[start + 1:]:
                if re.match(r"  \S", line):
                    break
                block.append(line)
            return block

        def items(block, key):
            start = block.index(f"    {key}:")
            found = []
            for line in block[start + 1:]:
                if line.strip().startswith("#"):
                    continue
                entry = re.fullmatch(r"      - (\S+)", line)
                if entry is None:
                    break
                found.append(entry[1])
            return found
        entry = (ROOT / "ops/provision-daemon-models.sh").read_text(encoding="utf-8")
        dockerfile = (ROOT / "ops/Dockerfile.daemon-rust").read_text(encoding="utf-8")
        account = re.search(r"useradd --uid (\d+) --gid (\d+)", (ROOT / "ops/Dockerfile.daemon").read_text(encoding="utf-8"))
        uid, gid = account.groups()
        # Root only until the drop: supplementary groups, gid, uid and the
        # inheritable set all go; setuid from root clears permitted/effective.
        self.assertIn(f"exec setpriv --reuid={uid} --regid={gid} --clear-groups --inh-caps=-all -- /bin/sh \"$0\" \"$@\"",
                      entry)
        self.assertIn(f"-uid 0 -exec chown -h {uid}:{gid} {{}} +", entry)
        self.assertLess(entry.index("--healthcheck"), entry.index("exec setpriv"))
        self.assertLess(entry.index("exec setpriv"), entry.index("provision models --if-missing"))
        self.assertIn(f"chown {uid}:{gid} /models /data", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/provision-daemon-models"]', dockerfile)
        overlays = [ROOT / "ops/docker-compose.rust-ghcr.yml", ROOT / "pseudolife_memory/compose/docker-compose.rust-ghcr.yml"]
        self.assertEqual(overlays[0].read_bytes().replace(b"\r\n", b"\n"), overlays[1].read_bytes().replace(b"\r\n", b"\n"))
        for base_path, overlay_path in ((ROOT / "ops/docker-compose.yml", overlays[0]),
                                        (ROOT / "pseudolife_memory/compose/docker-compose.yml", overlays[1])):
            base, overlay = service(base_path), service(overlay_path)
            # A `user:` would skip the bootstrap and fail on root-owned volumes;
            # the bootstrap needs exactly the base file's four capabilities.
            for key in ("user", "cap_add", "cap_drop", "security_opt", "entrypoint"):
                self.assertNotIn(f"    {key}:", [line.split(":")[0] + ":" for line in overlay], overlay_path)
            self.assertEqual(items(base, "cap_drop"), ["ALL"])
            self.assertEqual(sorted(items(base, "cap_add")), ["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"])
            self.assertIn("no-new-privileges:true", items(base, "security_opt"))

    @unittest.skipIf(os.name == "nt", "the image entrypoint runs on Linux")
    def test_bootstrap_transfers_root_owned_state_then_execs_as_the_daemon_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = root / "calls"
            stubs = {
                "id": 'echo "${FAKE_UID:-0}"',
                "find": 'echo "find $*" >> "$CALLS"; test "${FIND_FAIL:-0}" = 0',
                "setpriv": 'echo "setpriv $* HOME=$HOME" >> "$CALLS"; test "${SETPRIV_FAIL:-0}" = 0 || exit 5\n'
                           'while [ "$1" != -- ]; do shift; done; shift\nFAKE_UID=10001 exec "$@"',
                "pseudolife-daemon": 'echo "daemon $* uid=$(id -u)" >> "$CALLS"',
            }
            for name, script in stubs.items():
                (root / name).write_text("#!/bin/sh\n" + script + "\n")
                (root / name).chmod(0o755)
            state, models = root / "data", root / "models"
            state.mkdir()
            models.mkdir()
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"], CALLS=str(calls),
                       TMPDIR=str(root), HOME="/root", PSEUDOLIFE_ENTRYPOINT_STATE_DIRS=f"{state} {models} {root}/absent")
            env.pop("PSEUDOLIFE_DAEMON_ONNX_DIR", None)
            entrypoint = str(ROOT / "ops/provision-daemon-models.sh")

            def run(**extra):
                calls.unlink(missing_ok=True)
                result = subprocess.run(["sh", entrypoint], env=dict(env, **extra), capture_output=True, text=True,
                                        timeout=30)
                return result, (calls.read_text(encoding="utf-8").splitlines() if calls.exists() else [])

            result, seen = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            pruned = "-xdev -ignore_readdir_race ( -false ) -prune -o"
            self.assertEqual(seen, [
                f"find {state} {pruned} -uid 0 -exec chown -h 10001:10001 {{}} +",
                f"find {models} {pruned} -uid 0 -exec chown -h 10001:10001 {{}} +",
                f"setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all -- /bin/sh {entrypoint} HOME=/",
                "daemon provision models --if-missing uid=10001",
                "daemon  uid=10001",
            ])
            # A transfer or drop that fails never runs anything as root.
            result, seen = run(FIND_FAIL="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("startup refused", result.stderr)
            self.assertEqual([line for line in seen if not line.startswith("find ")], [])
            result, seen = run(SETPRIV_FAIL="1")
            self.assertEqual(result.returncode, 5)
            self.assertFalse(any(line.startswith("daemon ") for line in seen))
            # An explicit non-root `user:` (mirror, shadow) changes nothing.
            result, seen = run(FAKE_UID="10001")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(seen, ["daemon provision models --if-missing uid=10001", "daemon  uid=10001"])


def ownership_records(stdout: bytes) -> list[str]:
    """The NUL-separated fields of one helper answer (sent as od's hex)."""
    raw = bytes.fromhex(stdout.decode("ascii").replace("\n", " "))
    return raw.decode("utf-8", "replace").split("\0")


class OwnershipModesTest(unittest.TestCase):
    """The entrypoint's --probe-access and --repair-ownership modes, which the
    native client runs with `/bin/sh -c` in its ownership helpers."""

    ENTRY = ROOT / "ops/provision-daemon-models.sh"

    def run_mode(self, *args, **env):
        return subprocess.run(["sh", "-c", self.ENTRY.read_text(encoding="utf-8"), "provision-daemon-models", *args],
                              capture_output=True, timeout=60, env=dict(os.environ, **env))

    def test_the_native_client_runs_a_byte_exact_copy(self):
        copy = ROOT / "rust/shim/src/cli/ownership/provision-daemon-models.sh"
        self.assertEqual(copy.read_bytes(), self.ENTRY.read_bytes())
        self.assertNotIn(b"\r", self.ENTRY.read_bytes())

    @unittest.skipIf(os.name == "nt", "the entrypoint runs on Linux")
    def test_usage_and_missing_roots_are_refused_with_distinct_codes(self):
        self.assertEqual(self.run_mode("--probe-access").returncode, 64)
        with tempfile.TemporaryDirectory() as temporary:
            # A root the account cannot reach is one it cannot use...
            missing = self.run_mode("--probe-access", f"{temporary}/absent")
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertEqual(ownership_records(missing.stdout),
                             ["probe-access", "1", "U", "?", f"{temporary}/absent", "end", ""])
            # ...while the uid-0 modes refuse a missing one.
            if os.geteuid() == 0:
                missing = self.run_mode("--list-root-owned", f"{temporary}/absent")
                self.assertEqual(missing.returncode, 72, missing.stderr)
        if os.geteuid() != 0:
            for mode in ("--repair-ownership", "--list-root-owned"):
                refused = self.run_mode(mode, "/tmp")
                self.assertEqual(refused.returncode, 64)
                self.assertIn(b"ownership helper", refused.stderr)

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0,
                     "access checks need a non-root account")
    def test_the_probe_reports_what_the_account_cannot_use_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            (root / "closed/deep").mkdir(parents=True)
            (root / "closed/deep/f").write_text("x")
            (root / "open").mkdir()
            (root / "open/unreadable").write_text("x")
            (root / "open/link").symlink_to("/nonexistent")
            before = sorted(str(p) for p in root.rglob("*"))
            (root / "open/unreadable").chmod(0o000)
            (root / "closed").chmod(0o000)
            try:
                result = self.run_mode("--probe-access", str(root))
            finally:
                (root / "closed").chmod(0o700)
            self.assertEqual(result.returncode, 0, result.stderr)
            fields = ownership_records(result.stdout)
            self.assertEqual(fields[:2], ["probe-access", "1"])
            self.assertEqual(fields[-2:], ["end", ""])
            # This account is not 10001, so its own entries count as another
            # owner's: the closed directory (pruned, never entered) and the
            # unreadable file; a dangling link is never an entry to use.
            uid = str(os.geteuid())
            self.assertIn(["U", uid, str(root / "closed")], [fields[i:i + 3] for i in range(len(fields))])
            self.assertIn(["U", uid, str(root / "open/unreadable")], [fields[i:i + 3] for i in range(len(fields))])
            self.assertNotIn(str(root / "closed/deep"), fields)
            self.assertNotIn(str(root / "open/link"), fields)
            self.assertEqual(before, sorted(str(p) for p in root.rglob("*")))


def docker_works() -> bool:
    if os.name == "nt":
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(docker_works(), "needs a Linux Docker engine")
class OwnershipRepairOnRealEngineTest(unittest.TestCase):
    """A seeded volume with root-owned 0700 trees, a nested volume and links,
    repaired by the exact helper the native client starts, then used by a
    container running as 10001 with no capabilities. Every container and
    volume is disposable, named with this run's tag, and removed by name."""

    # The bases of both daemon image families; PSEUDOLIFE_OWNERSHIP_TEST_IMAGES
    # names others (the published images) to check their tools too.
    IMAGES = os.environ.get("PSEUDOLIFE_OWNERSHIP_TEST_IMAGES",
                            "debian:bookworm-slim python:3.12-slim-bookworm").split()
    ENTRY = ROOT / "ops/provision-daemon-models.sh"

    def setUp(self):
        import uuid
        self.tag = f"pl-ownership-test-{uuid.uuid4().hex[:10]}"
        self.created = []
        self.fresh()

    def fresh(self):
        """New empty volumes for the next seed (a mount point cannot be emptied)."""
        n = len(self.created) // 3
        self.volumes = [f"{self.tag}-{n}-data", f"{self.tag}-{n}-nested", f"{self.tag}-{n}-models"]
        for volume in self.volumes:
            subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True, timeout=60)
            self.created.append(volume)

    def tearDown(self):
        subprocess.run(["docker", "volume", "rm", *self.created], capture_output=True, timeout=120)

    def mounts(self):
        data, nested, models = self.volumes
        return ["--mount", f"type=volume,src={data},dst=/data", "--mount", f"type=volume,src={nested},dst=/data/nested",
                "--mount", f"type=volume,src={models},dst=/models"]

    def docker(self, *args, check=True):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=600)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result

    # The users and capabilities of cli::ownership's probe_argv, list_argv
    # and repair_argv, by mode.
    USERS = {"--probe-access": ["--user", "10001:10001", "--cap-drop", "ALL"],
             "--list-root-owned": ["--user", "0:0", "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE"],
             "--repair-ownership": ["--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                                    "--cap-add", "DAC_OVERRIDE"]}

    def helper(self, image, mode, *roots):
        user = self.USERS[mode]
        return self.docker("run", "--rm", *user, "--security-opt", "no-new-privileges", "--network", "none",
                           "--read-only", "--log-driver", "none", *self.mounts(), "--entrypoint", "/bin/sh", image,
                           "-c", self.ENTRY.read_text(encoding="utf-8"), "provision-daemon-models", mode, *roots,
                           check=False)

    def owners(self, image):
        listing = self.docker("run", "--rm", *self.mounts(), "--entrypoint", "find", image, "/data", "/models",
                              "-printf", "%U:%G %p\\n").stdout.decode()
        return dict(line.split(" ", 1)[::-1] for line in listing.splitlines())

    # The mounts and roots cli::ownership gives each helper, from the file
    # ownership::tests checks against the product's argv.
    SHARED = json.loads((ROOT / "rust/shim/src/cli/ownership/helpers.json").read_text(encoding="utf-8"))

    def product(self, helper):
        """One helper's mounts (this run's volumes standing in for state,
        nested and models) and roots, exactly as the shared file has them."""
        names = dict(zip(("state", "nested", "models"), self.volumes))
        mounts = []
        for spec in self.SHARED["helpers"][helper]["mounts"]:
            fields = [f"src={names[f[4:]]}" if f.startswith("src=") else f for f in spec.split(",")]
            mounts += ["--mount", ",".join(fields)]
        return mounts, self.SHARED["helpers"][helper]["roots"]

    def run_helper(self, image, mode, helper):
        # The flags of cli::ownership's helper_argv, pinned by its argv tests.
        mounts, roots = self.product(helper)
        return self.docker("run", "--rm", *self.USERS[mode], "--security-opt", "no-new-privileges", "--network", "none",
                           "--read-only", "--log-driver", "none", "--no-healthcheck", "--pull", "never",
                           "--pids-limit", "256", *mounts, "--entrypoint", "/bin/sh", image,
                           "-c", self.ENTRY.read_text(encoding="utf-8"), "provision-daemon-models", mode, *roots,
                           check=False)

    def test_the_repair_gives_root_owned_trees_to_10001_and_a_10001_container_can_use_them(self):
        # The mounts and roots are the product's (helpers.json, which
        # ownership::tests checks): the probe walks the top-level mounts, then
        # the nested writable one; the list and the repair walk only the
        # top-level mount of their part, the nested one mounted read-only.
        for index, image in enumerate(self.IMAGES):
            with self.subTest(image=image):
                if index:
                    self.fresh()
                self.docker("pull", "-q", image)
                # The tools the helpers use exist in this image family.
                tools = self.docker("run", "--rm", "--entrypoint", "/bin/sh", image, "-c",
                                    "command -v find chown sed od stat && find --version | head -1").stdout.decode()
                self.assertIn("GNU findutils", tools)
                self.docker("run", "--rm", *self.mounts(), "--entrypoint", "/bin/sh", image, "-euc", """
                    mkdir -p /data/rootdir/deep /data/userdir /data/owner-ro /models/m
                    echo s > /data/rootdir/deep/f; chmod 600 /data/rootdir/deep/f; chmod 700 /data/rootdir /data/rootdir/deep
                    echo u > /data/userdir/rootfile; chmod 600 /data/userdir/rootfile; chgrp 4 /data/userdir/rootfile
                    chown 10001:10001 /data/userdir; chmod 700 /data/userdir
                    echo r > /data/readable; chmod 644 /data/readable
                    echo o > /data/owner-ro/f; chown -R 10001:10001 /data/owner-ro; chmod 500 /data/owner-ro
                    echo t > /data/target; chown 1234:1234 /data/target; chmod 644 /data/target
                    ln -s target /data/link
                    echo n > /data/nested/kept; chmod 600 /data/nested/kept
                    echo m > /models/m/w; chmod 600 /models/m/w; chmod 700 /models/m
                    echo v > /models/readable; chmod 644 /models/readable
                    chown 10001:10001 /data /models""")
                before = self.run_helper(image, "--probe-access", "probe")
                self.assertEqual(before.returncode, 0, before.stderr)
                fields = ownership_records(before.stdout)
                unusable = [fields[i + 2] for i in range(2, len(fields) - 2) if fields[i] == "U"]
                for path in ("/data/rootdir", "/data/userdir/rootfile", "/models/m"):
                    self.assertIn(path, unusable)
                # Under /data a root-owned file must be writable too (the
                # daemon rewrites some in place); a model only readable.
                self.assertIn("/data/readable", unusable)
                self.assertNotIn("/models/readable", unusable)
                # The nested volume is walked by the probe as its own root:
                # the product refuses it, never repairs it.
                self.assertIn("/data/nested", unusable)
                self.assertIn("/data/owner-ro", fields)  # detail only (O): its owner can fix it
                self.assertEqual(fields[fields.index("/data/owner-ro") - 1], "O")

                undo, records = {}, {}
                for part in ("state", "models"):
                    listed = self.run_helper(image, "--list-root-owned", part)
                    self.assertEqual(listed.returncode, 0, listed.stderr)
                    fields = ownership_records(listed.stdout)
                    undo.update({fields[i + 2]: fields[i + 1] for i in range(2, len(fields) - 3, 3) if fields[i] == "L"})
                    if part == "state":
                        self.assertEqual(self.owners(image)["/data/rootdir"], "0:0", "the listing changes nothing")
                    repaired = self.run_helper(image, "--repair-ownership", part)
                    self.assertEqual(repaired.returncode, 0, repaired.stderr)
                    fields = ownership_records(repaired.stdout)
                    records.update({fields[i + 2]: fields[i + 1] for i in range(2, len(fields) - 3, 3) if fields[i] == "C"})
                    self.assertNotIn("F", fields)
                # Every changed path is reported with the group it had, and
                # nothing else was changed: the nested mount, another owner's
                # file and the link's target keep their owners.
                self.assertEqual(records, {"/data/rootdir": "0", "/data/rootdir/deep": "0", "/data/rootdir/deep/f": "0",
                                           "/data/userdir/rootfile": "4", "/data/link": "0", "/data/readable": "0",
                                           "/models/m": "0", "/models/m/w": "0", "/models/readable": "0"})
                self.assertEqual(undo, records, "the undo list written first names exactly what changed")
                owners = self.owners(image)
                self.assertEqual(owners["/data/nested/kept"], "0:0")
                self.assertEqual(owners["/data/nested"], "0:0")
                self.assertEqual(owners["/data/target"], "1234:1234")
                self.assertEqual(owners["/data/link"], "10001:10001")
                self.assertEqual(owners["/data/rootdir/deep/f"], "10001:10001")

                after = self.run_helper(image, "--probe-access", "probe")
                fields = ownership_records(after.stdout)
                left = [fields[i + 2] for i in range(2, len(fields) - 2) if fields[i] == "U"]
                self.assertTrue(left and all(p.startswith("/data/nested") for p in left), left)
                # Started as 10001 with no capabilities, the account reads every
                # file and writes in every directory outside the nested mount.
                used = self.docker("run", "--rm", "--user", "10001:10001", "--cap-drop", "ALL", "--network", "none",
                                   *self.mounts(), "--entrypoint", "/bin/sh", image, "-euc", """
                    for d in /data/rootdir /data/rootdir/deep /data/userdir /models /models/m; do
                        touch "$d/.w" && rm "$d/.w"
                    done
                    echo again > /data/readable
                    cat /data/rootdir/deep/f /data/userdir/rootfile /models/m/w >/dev/null && echo usable""", check=False)
                self.assertEqual(used.returncode, 0, used.stderr)
                self.assertIn(b"usable", used.stdout)

    def test_with_chown_alone_the_walk_fails_visibly(self):
        image = self.IMAGES[0]
        self.docker("pull", "-q", image)
        self.docker("run", "--rm", *self.mounts(), "--entrypoint", "/bin/sh", image, "-euc",
                    "mkdir -p /models/z/q; echo a > /models/z/q/f; chmod 700 /models/z /models/z/q; chown 10001 /models/z")
        result = self.docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                             "--security-opt", "no-new-privileges", "--network", "none", "--read-only", *self.mounts(),
                             "--entrypoint", "/bin/sh", image, "-c", self.ENTRY.read_text(encoding="utf-8"),
                             "provision-daemon-models", "--repair-ownership", "/models", check=False)
        self.assertEqual(result.returncode, 71, result.stderr)
        self.assertNotIn("end", ownership_records(result.stdout))


if __name__ == "__main__":
    unittest.main()
