"""Pins cache-path rejection and paired resident-memory attribution."""
import math
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import scaling
from .ci import repeated_attempts


class ScalingTests(unittest.TestCase):
    def test_cold_pool_is_larger_than_cache_unique_and_disjoint_from_warmup(self):
        pool = scaling.cold_pool()
        self.assertGreater(len(pool), scaling.CACHE_SIZE)
        texts = {q["query"] for q in pool}
        self.assertEqual(len(texts), len(pool))
        self.assertTrue(all(f"Project {topic} calibration" not in texts for topic in scaling.TOPICS))

    def test_generated_vectors_are_deterministic_nonzero_normalized(self):
        values = scaling.vector(1)
        self.assertEqual(values, scaling.vector(1))
        self.assertNotEqual(values, scaling.vector(2))
        self.assertEqual(len(values), 1024)
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in values)), 1)

    def test_missed_cache_arm_rejects_receipt(self):
        client = SimpleNamespace(call=lambda *args: {"entries": []})
        before, after = {"model_encode_calls": 4}, {"model_encode_calls": 4}
        with patch.object(scaling, "observe", side_effect=[before, after]), patch.object(
                scaling, "memory_tree", return_value={"rss_bytes": 123}):
            with self.assertRaisesRegex(RuntimeError, "embedding path"):
                scaling.search_arm(client, SimpleNamespace(verified_runtime_pid=1),
                                   [{"query": "unique"}], expected_encodes=1)

    def test_paired_delta_removes_empty_model_baseline_and_has_noise(self):
        runs = []
        for repeat in range(3):
            for size, hydrated, idle in [(2000, 110, 100), (20000, 240 + repeat, 200)]:
                runs.append({"repeat": repeat, "bank_size": size, "thread_policy": "one",
                    "idle_rss_bytes": [idle], "hydrated_rss_bytes": [hydrated],
                    "arms": {arm: {"latency_ms": [1 + repeat], "load_rss_bytes": [hydrated]}
                             for arm in ("warm", "cold")}})
        result = scaling.summarize(runs)
        delta = result["resident_scaling"][0]
        self.assertEqual(delta["vector_lower_bound_delta_bytes"], 18000 * 1024 * 4)
        self.assertEqual(delta["hydrated_rss_delta_bytes"]["median"], 131)
        self.assertEqual(delta["idle_adjusted_hydration_delta_bytes"]["median"], 31)
        self.assertEqual(delta["idle_adjusted_delta_control"]["noise_floor_abs"], 2)
        self.assertEqual(len(result["cells"]), 4)
        self.assertTrue(all(c["search_control"]["available"] for c in result["cells"]))

    def test_ci_control_requires_three_distinct_matched_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for attempt in range(1, 4):
                path = Path(directory) / f"{attempt}.json"
                path.write_text(json.dumps({"run": {"id": 1, "run_attempt": attempt, "head_sha": "head",
                    "workflow_id": 1, "path": ".github/workflows/ci.yml", "status": "completed",
                    "conclusion": "success", "html_url": "https://example.invalid/run/1",
                    "created_at": "2026-10-03T00:00:00Z", "run_started_at": "2026-10-03T00:00:01Z", "event": "push"},
                    "jobs": {"total_count": 1, "jobs": [{"run_id": 1, "run_attempt": attempt,
                    "head_sha": "head", "status": "completed", "conclusion": "success", "name": "linux",
                    "labels": ["ubuntu-latest", "private-machine"], "steps": [],
                    "started_at": "2026-10-03T00:00:01Z", "completed_at": "2026-10-03T00:00:02Z"}]}}))
                paths.append(path)
            with self.assertRaisesRegex(ValueError, "three"):
                repeated_attempts(paths[:2])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                repeated_attempts([paths[0], paths[0], paths[2]])
            runs = repeated_attempts(paths)
            self.assertEqual([r["run_attempt"] for r in runs], [1, 2, 3])
            self.assertNotIn("private-machine", json.dumps(runs))
            captured = json.loads(paths[2].read_text())
            captured["jobs"]["jobs"][0]["name"] = "different-matrix"
            paths[2].write_text(json.dumps(captured))
            with self.assertRaisesRegex(ValueError, "must match"):
                repeated_attempts(paths)


if __name__ == "__main__":
    unittest.main()
