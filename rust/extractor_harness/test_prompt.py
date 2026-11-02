"""The native and Python extractors share the unchanged production prompt."""
from pathlib import Path
import ast
import hashlib
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SharedPrompt(unittest.TestCase):
    def test_shared_prompt_is_the_measured_production_prompt(self):
        expected = (ROOT / "evals/prompts/assistant_facts_provenance.txt").read_text(encoding="utf-8")
        tree = ast.parse((ROOT / "pseudolife_memory/memory/dream.py").read_text(encoding="utf-8"))
        assignments = {n.targets[0].id: n.value for n in tree.body
                       if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
        def evaluate(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return evaluate(assignments[node.id])
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return evaluate(node.left) + evaluate(node.right)
            raise AssertionError(ast.dump(node))
        actual = evaluate(assignments["_SYSTEM_PROMPT"])
        self.assertEqual(actual, expected)
        for kind in ("claude", "codex"):
            import importlib.util
            spec = importlib.util.spec_from_file_location(kind, ROOT / "evals" / f"{kind}_shim.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module._SYSTEM_PROMPT, actual)
        self.assertEqual(hashlib.sha256(actual.encode()).hexdigest(),
                         "45986ada8a24c716d0dadce0d031f7e1370c94a0e3be48d141f6b43189f7e57e")

    def test_native_model_lists_match_both_oracles(self):
        native = (ROOT / "rust/shim/src/extractor_shim/config.rs").read_text()
        for kind, prefix in (("claude", "claude-"), ("codex", "gpt-")):
            oracle = (ROOT / "evals" / f"{kind}_shim.py").read_text(encoding="utf-8")
            block = oracle.split('"/v1/models", "/models"', 1)[1].split("])]", 1)[0]
            pattern = rf'"({prefix}[a-z0-9.-]+)"'
            self.assertEqual(set(re.findall(pattern, block)), set(re.findall(pattern, native)))


if __name__ == "__main__":
    unittest.main()
