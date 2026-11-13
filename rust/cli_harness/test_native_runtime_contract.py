"""Shared Codex consent cases against the retained Python oracle.

Only the pure oracle function is loaded: no client home, credentials, package
initialization, CLI, network endpoint or installed registration is consulted.
The Rust unit selection cli::update::clients::tests::shared_consent_cases_match_oracle
reads the same independently specified cases.

The function runs outside its module, so every module-level name it reads is
found statically (each global its code and nested code load) and taken from
the same syntax tree: a plain assignment (with whatever it reads in turn) or
a standard-library import. Anything else refuses by name, so a new dependency
of the oracle fails here with its cause instead of a NameError on some case.
"""
from __future__ import annotations

import ast
import builtins
import dis
import functools
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ORACLE = ROOT / "pseudolife_memory/client_updates.py"
CASES = json.loads((Path(__file__).parent / "fixtures/native_codex_consent.json").read_text(encoding="utf-8"))


def _global_reads(code: types.CodeType) -> set[str]:
    names = {i.argval for i in dis.get_instructions(code) if i.opname in ("LOAD_GLOBAL", "LOAD_NAME")}
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _global_reads(const)
    return names


def _bindings(tree: ast.Module) -> dict[str, list[ast.stmt]]:
    bound: dict[str, list[ast.stmt]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        elif isinstance(node, ast.Import):
            names = [(a.asname or a.name).split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [a.asname or a.name for a in node.names]
        else:
            continue
        for name in names:
            bound.setdefault(name, []).append(node)
    return bound


def _plain_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        return all(a.name.split(".")[0] in sys.stdlib_module_names for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.level == 0 and (node.module or "").split(".")[0] in sys.stdlib_module_names
    return False


def load_isolated(source: str, filename: str, function: str):
    """`function` from `source`, run alone with only the module-level names it
    reads, each a plain assignment or a standard-library import."""
    tree = ast.parse(source, filename)
    bound = _bindings(tree)
    target = bound.get(function, [])
    if len(target) != 1 or not isinstance(target[0], ast.FunctionDef):
        raise LookupError(f"{filename}: {function} is not one module-level function")
    # Compile as the module does: its own __future__ imports, not this file's.
    futures = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    scope: dict = {}
    loaded: set[str] = set()

    def compiled(node: ast.stmt) -> types.CodeType:
        module = ast.Module(body=[*futures, node], type_ignores=[])
        return compile(module, filename, "exec", dont_inherit=True)

    def run(node: ast.stmt) -> None:
        exec(compiled(node), scope)

    def resolve(name: str, reader: str, chain: tuple[str, ...]) -> None:
        if name in loaded:
            return
        if name in chain:
            raise LookupError(f"{filename}: {name} refers to itself through {' -> '.join(chain)}")
        nodes = bound.get(name, [])
        if not nodes:
            if hasattr(builtins, name):
                return
            raise LookupError(
                f"{filename}: {reader} reads {name}, which is not a plain module-level "
                "binding (defined conditionally or not at all); give the isolated "
                "oracle a plain assignment or extend this loader")
        if len(nodes) != 1:
            raise LookupError(f"{filename}: {reader} reads {name}, which is bound "
                              f"{len(nodes)} times at module level; the isolated value is ambiguous")
        node = nodes[0]
        if _plain_import(node):
            run(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            reads = {n.id for n in ast.walk(node.value)
                     if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            for read in sorted(reads):
                resolve(read, name, chain + (name,))
            run(node)
        else:
            kind = type(node).__name__
            raise LookupError(
                f"{filename}: {reader} reads {name}, a module-level {kind}, not a plain "
                "assignment or standard-library import; loading it would run more of "
                "the module than the oracle, so make it a plain assignment or extend "
                "this loader deliberately")
        loaded.add(name)

    # The definition's own code covers its annotations, defaults and
    # decorators as well as the body and nested functions.
    for name in sorted(_global_reads(compiled(target[0]))):
        resolve(name, function, ())
    run(target[0])
    return scope[function]


@functools.cache
def _oracle():
    return load_isolated(ORACLE.read_text(encoding="utf-8"), str(ORACLE), "_codex_hook_manifest")


def test_the_isolated_oracle_refuses_a_global_it_cannot_load_plainly():
    source = ("import json\nfrom pseudolife_memory import helper\nLIMIT = 3\nPAIR = (LIMIT, json.dumps)\n"
              "def needs_import(x):\n    return helper(x)\n"
              "def needs_def(x):\n    def inner():\n        return other(x)\n    return inner()\n"
              "def other(x):\n    return x\n"
              "def missing(x):\n    return NOT_THERE\n"
              "if True:\n    LATE = 1\n"
              "def conditional(x):\n    return LATE\n"
              "def fine(x):\n    return PAIR[0] + len(json.dumps(x))\n"
              "class Kind:\n    pass\n"
              "def annotated(x: Kind) -> None:\n    return None\n")
    for function, needle in (("needs_import", "helper, a module-level ImportFrom"),
                             ("needs_def", "other, a module-level FunctionDef"),
                             ("missing", "NOT_THERE, which is not a plain module-level binding"),
                             ("conditional", "LATE, which is not a plain module-level binding"),
                             ("annotated", "Kind, a module-level ClassDef")):
        with pytest.raises(LookupError, match=needle):
            load_isolated(source, "synthetic.py", function)
    assert load_isolated(source, "synthetic.py", "fine")([1]) == 6


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_shared_consent_case_matches_python_oracle(case):
    actual = _oracle()(case["input"].encode("utf-8"))
    expected = case["normalized"]
    if expected is None:
        assert actual is None
    else:
        assert actual == json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False)
