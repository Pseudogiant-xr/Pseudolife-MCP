"""Check the native schema inputs and the daemon metadata derived from them."""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import gen_schema_sql as gen


class SchemaGeneration(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.storage = Path(directory.name)
        self.sql = self.storage / "schema.sql"
        self.plan = self.storage / "schema_plan.json"
        self.metadata = self.storage / "schema_meta.rs"
        for artifact in (self.sql, self.plan, self.metadata):
            shutil.copyfile(gen.STORAGE / artifact.name, artifact)

    def check(self, expected_error: str):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(gen.main(["--check"], storage=self.storage), 1)
        self.assertIn(expected_error, stderr.getvalue())

    def test_committed_schema_is_current(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(gen.main(["--check"], storage=gen.STORAGE), 0)

    def test_committed_plan_and_sql_fingerprint(self):
        plan = gen.load_schema_plan(self.storage)
        _, _, ddl = self.sql.read_text(encoding="utf-8").partition("\n")
        self.assertEqual(gen.plan_fingerprint(plan, ddl), gen.PLAN_SHA256)

    def test_version_must_be_a_number_in_the_plan(self):
        plan = gen.load_schema_plan(self.storage)
        plan["schema_version"] = str(plan["schema_version"])
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        self.check("schema_version must be a positive i64 integer")

    def test_dimension_must_be_a_number_in_the_plan(self):
        plan = gen.load_schema_plan(self.storage)
        plan["embedding_dimension"] = str(plan["embedding_dimension"])
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        self.check("embedding_dimension must be a positive i32 integer")

    def test_version_mismatch_in_sql_header(self):
        plan = gen.load_schema_plan(self.storage)
        _, _, ddl = self.sql.read_text(encoding="utf-8").partition("\n")
        self.sql.write_text(f"-- schema_version {plan['schema_version'] + 1}\n{ddl}", encoding="utf-8")
        self.check("schema.sql header must be")

    def test_version_mismatch_in_metadata(self):
        version = gen.load_schema_plan(self.storage)["schema_version"]
        text = self.metadata.read_text(encoding="utf-8")
        self.metadata.write_text(text.replace(f"i64 = {version};", f"i64 = {version + 1};"),
                                 encoding="utf-8")
        self.check("stale schema_meta.rs")

    def test_dimension_mismatch_in_sql(self):
        dimension = gen.load_schema_plan(self.storage)["embedding_dimension"]
        text = self.sql.read_text(encoding="utf-8")
        self.sql.write_text(text.replace(f"vector({dimension})", f"vector({dimension + 1})", 1),
                            encoding="utf-8")
        self.check("embedding dimensions")

    def test_dimension_mismatch_in_metadata(self):
        dimension = gen.load_schema_plan(self.storage)["embedding_dimension"]
        text = self.metadata.read_text(encoding="utf-8")
        self.metadata.write_text(text.replace(f"i32 = {dimension};", f"i32 = {dimension + 1};"),
                                 encoding="utf-8")
        self.check("stale schema_meta.rs")

    def test_dimension_mismatch_in_embedding_refusal(self):
        plan = gen.load_schema_plan(self.storage)
        dimension = plan["embedding_dimension"]
        refusal = plan["embedding_refusal"]
        for template in ("vector({})", "{}-d"):
            with self.subTest(template=template):
                plan["embedding_refusal"] = refusal.replace(
                    template.format(dimension), template.format(dimension + 1), 1,
                )
                self.plan.write_text(json.dumps(plan), encoding="utf-8")
                self.check("embedding_refusal must name embedding_dimension")

    def test_version_mismatch_in_final_stamp(self):
        plan = gen.load_schema_plan(self.storage)
        plan["steps"][-1]["parameters"] = [str(plan["schema_version"] + 1)]
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        self.check("must end by stamping schema_version")

    def test_metadata_must_be_committed(self):
        self.metadata.unlink()
        self.check("stale schema_meta.rs")

    def test_generation_only_replaces_edited_metadata(self):
        inputs = (self.sql.read_bytes(), self.plan.read_bytes())
        expected = self.metadata.read_bytes()
        self.metadata.write_bytes(expected + b"// hand edit\n")
        self.check("stale schema_meta.rs")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(gen.main([], storage=self.storage), 0)
        self.assertEqual(self.metadata.read_bytes(), expected)
        self.assertEqual((self.sql.read_bytes(), self.plan.read_bytes()), inputs)


if __name__ == "__main__":
    unittest.main()
