"""The startup plan must follow the entire oracle, not only SCHEMA_SQL."""
import importlib.util
import unittest
import itertools
import json
import types
import ast
from contextlib import nullcontext
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gen_schema_sql", HERE / "gen_schema_sql.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


class SchemaGeneration(unittest.TestCase):
    def test_full_source_identity_is_recorded(self):
        source = gen.SOURCE.read_bytes()
        normal = gen.render(source)
        changed = gen.render(source + b"\n# generation freshness control\n")
        self.assertNotEqual(normal["schema_plan.json"], changed["schema_plan.json"])

    def test_committed_data_is_current(self):
        for name, wanted in gen.render(gen.SOURCE.read_bytes()).items():
            self.assertEqual((gen.TARGET.parent / name).read_bytes(), wanted, name)

    def test_unknown_upgrade_statement_is_refused(self):
        source = gen.SOURCE.read_bytes().replace(
            b"    return {}", b"    unexpected_migration()\n    return {}")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            gen.render(source)

    def test_helper_docstring_cannot_hide_an_executable_statement(self):
        for helper in ("_refuse_on_embedding_dim_mismatch", "_backfill_trace_invalidations"):
            tree = ast.parse(gen.SOURCE.read_bytes())
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == helper)
            function.body[0] = ast.parse("cur.execute('SELECT 424242 AS unexpected_probe')").body[0]
            with self.assertRaisesRegex(ValueError, "unsupported"):
                gen.render(ast.unparse(tree).encode())

    def test_embedding_probe_statement_cannot_be_an_early_return(self):
        tree = ast.parse(gen.SOURCE.read_bytes())
        helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                      and n.name == "_refuse_on_embedding_dim_mismatch")
        helper.body[1] = ast.Return(value=helper.body[1].value)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            gen.render(ast.unparse(tree).encode())

    def test_return_is_allowed_only_at_the_end_of_ensure_schema(self):
        tree = ast.parse(gen.SOURCE.read_bytes())
        helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ensure_schema")
        helper.body.insert(1, ast.Return(value=ast.Dict(keys=[], values=[])))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            gen.render(ast.unparse(tree).encode())

    def test_generated_base_sql_matches_the_imported_oracle(self):
        oracle = types.ModuleType("schema_oracle")
        exec(compile(gen.SOURCE.read_bytes(), "schema.py", "exec"), oracle.__dict__)
        actual = gen.render(gen.SOURCE.read_bytes())["schema.sql"].decode().split("\n", 1)[1]
        self.assertEqual(actual, oracle.SCHEMA_SQL)

    def test_chained_or_expression_assignment_targets_are_refused(self):
        for scope in ("module", "backfill"):
            for target in ("chained", "attribute", "subscript"):
                with self.subTest(scope=scope, target=target):
                    tree = ast.parse(gen.SOURCE.read_bytes())
                    if scope == "module":
                        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                                          and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "SCHEMA_SQL")
                    else:
                        helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                                      and n.name == "_backfill_trace_invalidations")
                        assignment = helper.body[1]
                    if target == "chained":
                        assignment.targets.append(ast.Name(id="unexpected_alias", ctx=ast.Store()))
                    else:
                        expression = "holder.sql" if target == "attribute" else "holder['sql']"
                        assignment.targets = ast.parse(expression + " = ''").body[0].targets
                    with self.assertRaisesRegex(ValueError, "unsupported"):
                        gen.render(ast.unparse(tree).encode())

    def test_later_chained_sql_reassignment_cannot_be_silently_omitted(self):
        tree = ast.parse(gen.SOURCE.read_bytes())
        index = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                     and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "SCHEMA_SQL")
        hidden = ast.parse("SCHEMA_SQL = unexpected_alias = SCHEMA_SQL + '\\nCREATE TABLE hidden_migration(id INTEGER);'").body[0]
        tree.body.insert(index + 1, hidden)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            gen.render(ast.unparse(tree).encode())

    def test_unsupported_module_binding_forms_are_refused(self):
        for binding in ("SCHEMA_SQL: str = SCHEMA_SQL + '\\nCREATE TABLE hidden(id INTEGER);'",
                        "SCHEMA_SQL *= 2", "SCHEMA_META_VERSION += 1", "holder.sql += SCHEMA_SQL"):
            with self.subTest(binding=binding):
                tree = ast.parse(gen.SOURCE.read_bytes())
                index = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                             and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "SCHEMA_SQL")
                tree.body.insert(index + 1, ast.parse(binding).body[0])
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    gen.render(ast.unparse(tree).encode())

    def test_every_branch_matches_oracle_statement_order_and_parameters(self):
        oracle = types.ModuleType("schema_oracle")
        exec(compile(gen.SOURCE.read_bytes(), "schema.py", "exec"), oracle.__dict__)
        plan = json.loads(gen.render(gen.SOURCE.read_bytes())["schema_plan.json"])
        for trace, constraint, foreign_key in itertools.product((False, True), repeat=3):
            class Cursor:
                rowcount = 0

                def __init__(self):
                    self.calls = []
                    self.sql = ""

                def execute(self, sql, params=()):
                    self.calls.append((sql, tuple(params)))
                    self.sql = sql
                    return self

                def fetchone(self):
                    if "atttypmod" in self.sql:
                        return None
                    if "to_regclass('public.memory_trace_invalidations')" in self.sql:
                        return ("memory_trace_invalidations" if trace else None,)
                    if "entries_dream_state_check" in self.sql:
                        return (1,) if constraint else None
                    if "entries_episode_id_fkey" in self.sql:
                        return (1,) if foreign_key else None
                    return None

            cursor = Cursor()
            connection = types.SimpleNamespace(transaction=lambda: nullcontext(),
                                               cursor=lambda: nullcontext(cursor))
            oracle.ensure_schema(connection)
            wanted = cursor.calls
            cursor = Cursor()
            variables = {}
            last = None

            def predicate(p):
                if "not" in p:
                    return not predicate(p["not"])
                if "variable" in p:
                    return variables[p["variable"]]
                return last is not None if p["last"] == "row" else last[0] is not None

            def run(steps):
                nonlocal last
                for step in steps:
                    if step["op"] == "embedding_guard":
                        oracle._refuse_on_embedding_dim_mismatch(cursor)
                    elif step["op"] == "sql":
                        sql = step["sql"]
                        for i in range(len(step["parameters"])):
                            sql = sql.replace(f"${i + 1}", "%s")
                        cursor.execute(sql, step["parameters"])
                        last = cursor.fetchone()
                    elif step["op"] == "base_schema":
                        cursor.execute(oracle.SCHEMA_SQL)
                        last = cursor.fetchone()
                    elif step["op"] == "remember":
                        variables[step["name"]] = predicate(step["test"])
                    else:
                        run(step["then"] if predicate(step["test"]) else step["else"])

            run(plan["steps"])
            self.assertEqual(wanted, cursor.calls, (trace, constraint, foreign_key))


if __name__ == "__main__":
    unittest.main()
