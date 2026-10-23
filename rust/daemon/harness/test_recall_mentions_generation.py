"""The committed recall `_mentions` tables and golden must be what the
generators produce from the Python 3.11 oracle today."""
import importlib.util
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


unicode_gen = load("gen_recall_unicode")
golden_gen = load("gen_recall_mentions_goldens")


class RecallMentionsGeneration(unittest.TestCase):
    def test_committed_tables_are_current(self):
        self.assertEqual(unicode_gen.TARGET.read_text(encoding="utf-8"), unicode_gen.generated())

    def test_committed_golden_is_current(self):
        self.assertEqual(golden_gen.TARGET.read_text(encoding="utf-8"), golden_gen.render())

    def test_golden_covers_both_outcomes(self):
        rows = json.loads(golden_gen.TARGET.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(rows), 3000)
        self.assertGreater(sum(row[2] for row in rows), 1000)
        self.assertGreater(sum(not row[2] for row in rows), 1000)


if __name__ == "__main__":
    unittest.main()
