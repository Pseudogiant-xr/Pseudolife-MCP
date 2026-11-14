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

    def test_the_goldens_were_recorded_against_todays_python_shims(self):
        # The Python extractor shims and the autostart writer are live code
        # the installers still run; a change to one asks for a new two-shim
        # recording, which is what the live comparison used to prove.
        for path in sorted(run.GOLDENS.glob("contract.*.json")):
            with self.subTest(golden=path.name):
                recorded = json.loads(path.read_text(encoding="utf-8")).get("python_sources")
                self.assertEqual(recorded, run.python_sources(),
                                 f"{', '.join(run.PYTHON_SOURCES)} changed since {path.name} was recorded: "
                                 "re-record it with `run.py --suite contract --record-goldens` on its OS")


class NativeOnly(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r6-contract-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def starts(self, slice_, mode):
        """Run a slice to completion with every shim start faked; the starts."""
        started = []
        scratch = Path(tempfile.mkdtemp(dir=self.root))

        @contextlib.contextmanager
        def server(binary, kind, root, *args, **kwargs):
            started.append(binary)
            plan = scratch / f"plan-{len(started)}.json"
            yield 1, plan, scratch / "journal.jsonl", None, scratch / "cli"

        with patch.object(run, "server", server), patch.object(run, "CONTRACT", run.Contract(mode)), \
                patch.object(run, "call", lambda *args, **kwargs: (200, b"{}")), \
                patch.object(run, "journal_rows", lambda path: []), \
                patch.object(run.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")), \
                patch.object(run.autostart_oracle, "write_config", lambda *args, **kwargs: None):
            slice_(NATIVE, scratch / slice_.__name__)
        return started

    def test_checking_starts_no_python_shim(self):
        for slice_ in (run.differential, run.tools_differential, run.health_differential, run.configuration):
            for mode in ("check", "candidate"):
                with self.subTest(slice=slice_.__name__, mode=mode):
                    started = self.starts(slice_, mode)
                    self.assertTrue(started)
                    self.assertEqual(set(started), {NATIVE})

    def test_recording_still_starts_both_shims(self):
        for slice_ in (run.differential, run.tools_differential, run.health_differential, run.configuration):
            with self.subTest(slice=slice_.__name__):
                started = self.starts(slice_, "record")
                self.assertEqual(started[:2], [None, NATIVE])
                self.assertEqual(started.count(None), started.count(NATIVE))


class Tokens(unittest.TestCase):
    @staticmethod
    def models_body(ids):
        return ('{"object": "list", "data": [' + ", ".join(f'{{"id": "{m}", "object": "model"}}' for m in ids) + "]}")

    def test_a_models_body_tokens_exactly_the_native_ordered_list(self):
        listed = run.provider_models("claude")
        launch = listed[3]
        body = self.models_body(dict.fromkeys([launch, *listed, "extractor", "bench"]))
        self.assertEqual(run.models_body(body, "claude"),
                         self.models_body([launch, "<claude listed models>", "extractor", "bench"]))
        # The launch model (the default) stays literal, so a changed default differs.
        other = self.models_body(dict.fromkeys([listed[0], *listed, "extractor", "bench"]))
        self.assertNotEqual(run.models_body(other, "claude"), run.models_body(body, "claude"))

    def test_a_dropped_reordered_or_foreign_model_is_not_a_token(self):
        listed = run.provider_models("claude")
        for ids in (listed[:-1], list(reversed(listed)), [*listed[:2], "gpt-6-sol", *listed[2:]]):
            with self.subTest(ids=ids):
                body = self.models_body(["claude-sonnet-5", *ids, "extractor", "bench"])
                self.assertEqual(run.models_body(body, "claude"), body)

    def test_the_prompt_becomes_a_token_and_model_ids_stay(self):
        text = "argv --model claude-sonnet-5 and PROMPT"
        self.assertEqual(run.tokens(text, "PROMPT"), f"argv --model claude-sonnet-5 and {run.PROMPT_TOKEN}")

    def test_a_long_string_is_stored_as_its_digest(self):
        long = "é" * (run.LONG + 1)
        stored = run.tokens(long, "P")
        self.assertTrue(stored.startswith(f"<{run.LONG + 1} chars sha256:"))
        self.assertNotEqual(stored, run.tokens(long[:-1] + "e", "P"))

    def test_any_users_home_is_a_host_path(self):
        # The guard's <user> placeholder in each user-home shape (tests/test_release_ux.py).
        for text in ("C:\\Users\\<user>\\x", "C:/Users/<user>/x", "/home/<user>/work", "/Users/<user>/x"):
            with self.subTest(text=text):
                self.assertTrue(run.host_paths({"value": text}))
        self.assertEqual(run.host_paths({"value": "fine /usr/bin and Users"}), [])

    def test_only_the_harness_directory_becomes_a_cwd_token(self):
        rows = run.journal_snapshot([{"cwd": os.getcwd()}, {"cwd": "<root>"}, {"cwd": tempfile.gettempdir()}])
        self.assertEqual([row["cwd"] for row in rows], ["<harness cwd>", "<root>", tempfile.gettempdir()])

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
