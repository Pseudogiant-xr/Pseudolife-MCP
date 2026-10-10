"""Keep surprise serialization fixtures independent of unspecified top-k ties."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import torch
import torch.nn.functional as F

import startup_cases as startup


class SurpriseFixtureTests(unittest.TestCase):
    def test_surprise_fixture_has_strictly_descending_dense_scores(self):
        rows = []
        startup.seed(SimpleNamespace(insert_entry=rows.append), "surprise-real-text-rounding")
        matrix = torch.stack([torch.as_tensor(row["embedding"]) for row in rows])
        query = torch.zeros(matrix.shape[1])
        query[0] = 1.0
        scores = (F.normalize(matrix, dim=1) @ query).tolist()
        self.assertTrue(all(left > right for left, right in zip(scores, scores[1:])), scores)
        self.assertEqual([round(score, 4) for score in scores], [1.0, 0.8, 0.6, 0.0])
        self.assertEqual([round(row["surprise"], 4) for row in rows],
                         [0.5, 0.0001, 0.1232, 0.875])


if __name__ == "__main__":
    unittest.main()
