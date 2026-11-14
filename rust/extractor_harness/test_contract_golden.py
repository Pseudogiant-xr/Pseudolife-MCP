"""The contract suite checks the native shim against committed snapshots."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import run

NATIVE = Path("native-shim")


class Started(Exception):
    pass


def expected_keys(platform):
    launchers = ("exe", "batch") if platform == "windows" else ("script",)
    keys = {f"differential/{launcher}/{kind}" for launcher in launchers for kind in ("claude", "codex")}
    keys |= {f"tools/{launcher}" for launcher in launchers}
    keys |= {f"health/{kind}/{reason}" for kind in ("claude", "codex") for reason in run.HEALTH_REASONS}
    keys |= {f"managed/{kind}" for kind in ("claude", "codex")}
    return keys


class GoldenCoverage(unittest.TestCase):
    def test_every_platform_golden_holds_every_case_the_suite_observes(self):
        prompt = run.PROMPT_FILE.read_text(encoding="utf-8")
        goldens = sorted(run.GOLDENS.glob("contract.*.json"))
        self.assertEqual({path.name for path in goldens}, {"contract.linux.json", "contract.windows.json"})
        for path in goldens:
            platform = path.name.split(".")[1]
            cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
            with self.subTest(golden=path.name):
                self.assertEqual(set(cases), expected_keys(platform))
                for key, value in cases.items():
                    kind = key.rsplit("/", 1)[1]
                    if key.startswith("differential/"):
                        self.assertEqual([r[0] for r in value["responses"]], [c[0] for c in run.corpus(kind, prompt)], key)
                    elif key.startswith("tools/"):
                        self.assertEqual(len(value["responses"]), len(run.TOOL_REPLIES), key)
                    elif key.startswith("managed/"):
                        self.assertEqual(len(value["responses"]), 2, key)
                self.assertEqual(run.host_paths(cases), [])


class NativeOnly(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r6-contract-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def starts(self, slice_, mode):
        started = []

        @contextlib.contextmanager
        def server(binary, *args, **kwargs):
            started.append(binary)
            raise Started
            yield

        with patch.object(run, "server", server), patch.object(run, "CONTRACT", run.Contract(mode)), \
                patch.object(run.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")), \
                patch.object(run.autostart_oracle, "write_config", lambda *args, **kwargs: None):
            with self.assertRaises(Started):
                slice_(NATIVE, Path(tempfile.mkdtemp(dir=self.root)) / slice_.__name__)
        return started

    def test_checking_starts_no_python_shim(self):
        for slice_ in (run.differential, run.tools_differential, run.health_differential, run.configuration):
            for mode in ("check", "candidate"):
                with self.subTest(slice=slice_.__name__, mode=mode):
                    self.assertEqual(self.starts(slice_, mode), [NATIVE])

    def test_recording_still_starts_the_python_shim_first(self):
        self.assertEqual(self.starts(run.differential, "record"), [None])


class Tokens(unittest.TestCase):
    def setUp(self):
        self.models = run.listed_models()

    def test_listed_models_and_the_prompt_become_tokens_and_unlisted_ones_stay(self):
        listed = self.models[0]
        text = (f'[{{"id": "{listed}", "object": "model"}}, {{"id": "{self.models[1]}", "object": "model"}}, '
                f'{{"id": "extractor", "object": "model"}}] {listed}-x claude-future PROMPT')
        self.assertEqual(run.tokens(text, "PROMPT", self.models),
                         f'[{{"id": "{run.MODEL_TOKEN}", "object": "model"}}, {{"id": "extractor", "object": "model"}}] '
                         f'{listed}-x claude-future {run.PROMPT_TOKEN}')

    def test_a_model_release_leaves_the_snapshot_unchanged(self):
        one = '[{"id": "claude-a", "object": "model"}]'
        two = '[{"id": "claude-a", "object": "model"}, {"id": "claude-b", "object": "model"}]'
        self.assertEqual(run.tokens(one, "P", ["claude-a"]), run.tokens(two, "P", ["claude-a", "claude-b"]))

    def test_a_long_string_is_stored_as_its_digest(self):
        long = "é" * (run.LONG + 1)
        stored = run.tokens(long, "P", [])
        self.assertTrue(stored.startswith(f"<{run.LONG + 1} chars sha256:"))
        self.assertNotEqual(stored, run.tokens(long[:-1] + "e", "P", []))

    def test_a_difference_is_named_at_its_path(self):
        golden = {"responses": [["models", 200, "a" * 500], ["missing", 404, '{"error": "not found"}']]}
        seen = {"responses": [["models", 200, "a" * 500], ["missing", 404, '{"error": "not found here"}']]}
        self.assertEqual(len(run.snapshot_diff(golden, seen)), 1)
        self.assertTrue(run.snapshot_diff(golden, seen)[0].startswith("/responses/1/2: golden "))


class Recording(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r6-contract-golden-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.golden = self.root / "contract.test.json"
        self.binary = self.root / "native"
        self.binary.write_bytes(b"native")

    def contract(self, mode, observed):
        contract = run.Contract(mode)
        contract.observed = observed
        return contract

    def finish(self, contract, write=False):
        with patch.object(run, "contract_golden", lambda: self.golden), patch("sys.stdout"):
            return contract.finish(self.binary, write=write)

    def test_a_snapshot_naming_a_host_path_is_never_recorded(self):
        observed = {"health/claude/x": [500, f"could not read {Path.home() / 'x'}"]}
        with self.assertRaises(AssertionError) as caught:
            self.finish(self.contract("record", observed))
        self.assertIn("host path", str(caught.exception))
        self.assertFalse(self.golden.exists())

    def test_checking_names_the_departing_case(self):
        self.finish(self.contract("record", {"a": [200, "x"], "b": [200, "y"]}))
        with self.assertRaises(AssertionError) as caught:
            self.finish(self.contract("check", {"a": [200, "x"], "b": [200, "z"]}))
        self.assertIn("b/1: golden", str(caught.exception))
        self.assertNotIn("a/", str(caught.exception))

    def test_a_candidate_dry_run_names_the_change_and_write_records_only_it(self):
        self.finish(self.contract("record", {"a": [200, "x"], "b": [200, "y"]}))
        before = self.golden.read_bytes()
        with self.assertRaises(SystemExit) as caught:
            self.finish(self.contract("candidate", {"a": [200, "x"], "b": [200, "z"]}))
        self.assertIn("1 changed", str(caught.exception))
        self.assertEqual(self.golden.read_bytes(), before)
        self.finish(self.contract("candidate", {"a": [200, "x"], "b": [200, "z"]}), write=True)
        written = json.loads(self.golden.read_text(encoding="utf-8"))
        self.assertEqual(written["cases"], {"a": [200, "x"], "b": [200, "z"]})
        self.assertEqual(set(written[run.candidate_record.PROVENANCE_KEY]), {"b"})


if __name__ == "__main__":
    unittest.main()
