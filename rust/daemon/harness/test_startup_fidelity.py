"""Keep serialization fixtures independent of unspecified top-k ties."""
from __future__ import annotations

import json
import tempfile
import unittest
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    def test_float_spelling_fixture_has_no_dense_ranking_ties(self):
        import run
        from tokenizers import Tokenizer
        from embedding_fixture import create, fixture_weights

        rows = []
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.side_effect = lambda statement, parameters: rows.append(parameters)
        with patch("pseudolife_memory.storage.postgres.PostgresStorage"), \
                patch("psycopg.connect", return_value=connection), \
                patch.object(run, "assert_disposable"):
            fixture = run.JsonFloatTies()
            fixture.prepare_template("fixture-dsn")

        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model"
            create(model, dimension=1024)
            token_ids = Tokenizer.from_file(str(model / "tokenizer.json")).encode("memory").ids
        weights = torch.from_numpy(fixture_weights(max(token_ids) + 1, 1024))
        query = F.normalize(weights[token_ids].mean(dim=0), dim=0)
        matrix = F.normalize(torch.tensor([json.loads(row[1]) for row in rows]), dim=1)
        scores = (matrix @ query).tolist()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(left > right for left, right in zip(scores, scores[1:])), scores)
        self.assertTrue(all(score > 0.25 for score in scores), scores)
        self.assertEqual([row[2] for row in rows], [float(token) for token in fixture.timestamps])
        for order in permutations(range(len(rows))):
            ranked = torch.topk(torch.tensor(scores)[list(order)], len(rows)).indices.tolist()
            self.assertEqual([order[index] for index in ranked], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
