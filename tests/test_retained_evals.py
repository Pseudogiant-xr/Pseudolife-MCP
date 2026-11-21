"""Model-free contracts of the retained Rust quality drivers."""
import ast
from collections import deque
from contextlib import contextmanager
from fnmatch import fnmatchcase
import gzip
import json
from pathlib import Path
import re
import subprocess
import tomllib

import pytest

from evals import lme_rejudge, longmemeval_bench as lme
from evals.retained_artifacts import read_json, record
from evals.retained_daemon import BenchError, drain_dream


class Bank:
    def __init__(self, extractor):
        self.extractor = extractor
        self.operations = []
        self.pending = 0
        self.cursor = 0

    def store(self, text):
        self.operations.append(("store", text))
        self.pending += 1

    def tool(self, name, **args):
        self.operations.append((name, args))
        if name == "memory_dream":
            count, self.pending = self.pending, 0
            self.cursor += count
            return {"pulled": count, "extractor": "primary", "cursor": self.cursor}
        assert name == "memory_history"
        return {"versions": [{"value": "old"}, {"value": "new"}]}

    def search(self, query, top_k):
        self.operations.append(("search", query, top_k))
        return {"entries": [{"text": "live raw" if self.extractor else "control raw"}],
                "cortex": ([{"entity": "build", "attribute": "tool", "value": "new"}]
                           if self.extractor else [])}


def question():
    return {"question_id": "q1", "question_type": "knowledge-update", "question": "Which tool?",
            "answer": "new", "question_date": "2024/01/03 (Wed) 00:00",
            "haystack_dates": ["2024/01/02 (Tue) 00:00", "2024/01/01 (Mon) 00:00"],
            "haystack_sessions": [[{"role": "user", "content": "new"}],
                                  [{"role": "user", "content": "old"}]]}


def test_longmemeval_uses_isolated_no_dream_control_and_drains_each_session():
    banks = []

    @contextmanager
    def open_bank(extractor):
        bank = Bank(extractor)
        banks.append(bank)
        yield bank

    row = lme.extract_question(question(), open_bank)

    active, control = banks
    assert [step[0] for step in active.operations] == [
        "store", "memory_dream", "memory_dream", "store", "memory_dream", "memory_dream",
        "search", "memory_history",
    ]
    assert active.operations[0][1].endswith("user: old")
    assert active.operations[3][1].endswith("user: new")
    assert [step[0] for step in control.operations] == ["store", "store", "search"]
    assert row["contexts"]["rag_control"] == "control raw"
    assert row["contexts"]["rag"] == "live raw"
    assert "old" in row["contexts"]["cortex"]
    assert "live raw" in row["contexts"]["hybrid"]


def test_dream_retries_a_held_batch_and_counts_only_acknowledged_work():
    replies = iter([
        {"pulled": 2, "extractor_failed": True, "hold_phase": "extract", "extractor": "primary"},
        {"pulled": 2, "claims": 1, "cursor": 3.0, "extractor": "primary"},
        {"pulled": 0, "extractor": "primary"},
    ])

    def tool(name, **args):
        assert name == "memory_dream" and args == {"action": "run", "limit": 100}
        return next(replies)

    assert drain_dream(tool, stored=2)["claims"] == 1
    with pytest.raises(StopIteration):
        next(replies)


@pytest.mark.parametrize("reply", [
    {"pulled": 0, "error": "offline"},
    {"pulled": 0, "extractor": "fallback"},
    {"pulled": 0, "extractor": "primary"},
    {"pulled": 1, "cursor": 1, "extractor": "primary", "quarantined": 1},
])
def test_dream_does_not_treat_an_incomplete_cell_as_measured(reply):
    with pytest.raises(BenchError):
        drain_dream(lambda name, **args: reply, stored=1)


class Instrument:
    def __init__(self, name, reply):
        self.name = name
        self.reply = reply
        self.calls = []

    def provenance(self):
        return {"served_model": self.name}

    def verify(self):
        return None

    def chat(self, system, user, *, max_tokens):
        self.calls.append((system, user))
        return self.reply(user)


def test_answer_resume_and_independent_judge_use_the_saved_rust_contexts(tmp_path):
    source, alternative = tmp_path / "primary", tmp_path / "alternative"
    q1, q2 = question(), {**question(), "question_id": "q2_abs", "answer": "I don't know"}
    manifest = {"daemon": {"engine": "rust"}, "question_ids": ["q1", "q2_abs"]}

    @contextmanager
    def open_bank(active):
        yield Bank(active)

    def answer(user):
        return "new" if "build / tool:" in user else "I don't know"

    def grade(user):
        expected = user.split("Correct answer: ")[1].split("\n")[0]
        return "yes" if user.endswith(f"Model response: {expected}") else "no"

    lme.run_extract(source, manifest, [q1, q2], open_bank)
    old = read_json(source / "contexts" / "0000.json")
    with pytest.raises(BenchError, match="already records different"):
        record(source / "contexts" / "0000.json", {**old, "question": "altered"})

    answerer, judge = Instrument("answerer", answer), Instrument("judge", grade)
    assert lme.run_answer(source, answerer, judge)["n"] == 2
    assert len(answerer.calls) == len(judge.calls) == 8
    assert lme.run_answer(source, answerer, judge)["arms"]["cortex"]["accuracy"] == 0.5
    assert len(answerer.calls) == len(judge.calls) == 8

    with pytest.raises(BenchError, match="different judge model"):
        lme_rejudge.run(source, alternative, judge)
    second = Instrument("second", lambda _user: "no")
    assert lme_rejudge.run(source, alternative, second)["arms"]["cortex"]["accuracy"] == 0
    assert len(answerer.calls) == 8 and len(second.calls) == 8
    assert read_json(alternative / "comparison.json")["disagreements"]["cortex"] == 1


def test_projected_gate_fixture_keeps_only_public_write_inputs(tmp_path):
    from evals import rebuild_contexts

    q = question()
    dump = {**q, "facts": [
        {"entity": "build", "attribute": "tool", "value": "new", "history": ["old", "new"],
         "embedding": [0.1, 0.9], "tx_time": 100},
        {"entity": "build", "attribute": "checks", "kind": "member", "value": "lint"},
        {"entity": "build", "attribute": "checks", "kind": "member", "value": "format"},
    ]}
    with gzip.open(tmp_path / "q1.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(dump, fh)
    fixture = rebuild_contexts.project(tmp_path, [q])
    cells = rebuild_contexts.fixture_cells(fixture, [q])
    assert cells["q1"] == [
        {"entity": "build", "attribute": "tool", "kind": "scalar", "values": ["old", "new"]},
        {"entity": "build", "attribute": "checks", "kind": "set", "values": ["lint", "format"]},
    ]
    with pytest.raises(BenchError, match="question"):
        rebuild_contexts.fixture_cells(fixture, [{**q, "question": "wrong corpus"}])


def test_gate_refuses_missing_or_mismatched_baseline_before_answering(tmp_path):
    from evals import regression_gate as gate

    manifest = {"engine": "rust", "policy": "test", "question_ids": ["q1"]}
    path = tmp_path / "baseline.json"
    with pytest.raises(BenchError, match="missing"):
        gate.baseline_for(path, manifest)
    record(path, {"manifest": {**manifest, "engine": "python"}, "revision": "0" * 40, "arms": {}})
    with pytest.raises(BenchError, match="baseline"):
        gate.baseline_for(path, manifest)


def test_gate_detects_row_level_replica_drift_even_when_averages_match():
    from evals import regression_gate as gate

    def row(qid, yes):
        return {"question_id": qid, "contexts": dict.fromkeys(lme.ARMS, "a"),
                **{f"{arm}_response": "a" for arm in lme.ARMS},
                **{f"{arm}_correct": yes for arm in lme.ARMS},
                **{f"{arm}_context_tokens": 1 for arm in lme.ARMS}}

    good = [row("q1", True), row("q2", False)]
    flipped = [row("q1", False), row("q2", True)]
    with pytest.raises(BenchError, match="replicates disagree"):
        gate.aggregate([good, flipped])


def test_gate_scores_regression_and_rejects_control_drift(tmp_path):
    from evals import regression_gate as gate

    @contextmanager
    def open_bank():
        yield Bank(True)

    identity = {"revision": "0" * 40, "binary_sha256": "1" * 64}
    manifest = {"engine": "rust", "question_ids": ["q1"]}
    fixed = {"q1": "frozen raw"}
    slots = {"q1": []}
    judge = Instrument("judge", lambda prompt: "yes" if prompt.endswith("response: new") else "no")
    all_correct = Instrument("answerer", lambda _prompt: "new")
    establish = tmp_path / "establish"
    assert gate.run(establish, manifest, identity, [question()], fixed, slots, open_bank,
                    all_correct, judge, repetitions=2, baseline=None) == 0
    baseline = gate.baseline_for(establish / "baseline-candidate.json", manifest)
    assert gate.run(tmp_path / "pass", manifest, identity, [question()], fixed, slots, open_bank,
                    all_correct, judge, repetitions=2, baseline=baseline) == 0

    regression = Instrument("answerer", lambda prompt: "new" if "frozen raw" in prompt else "old")
    assert gate.run(tmp_path / "regression", manifest, identity, [question()], fixed, slots, open_bank,
                    regression, judge, repetitions=2, baseline=baseline) == 1

    drift = Instrument("answerer", lambda _prompt: "old")
    with pytest.raises(BenchError, match="drifted on the pinned raw control"):
        gate.run(tmp_path / "drift", manifest, identity, [question()], fixed, slots, open_bank,
                 drift, judge, repetitions=2, baseline=baseline)


def test_ladder_keeps_the_naive_control_fixed_across_rungs():
    from evals import ladder_sweep as ladder

    contexts = [[pair["gold"]] for pair in ladder.PAIRS]
    naive = ladder.measure(contexts)
    assert naive["gold_recoverable"] == 1
    assert naive["stale_leak"] == 0
    control = ladder.control_measurement(contexts, {"engine": "rust"})
    assert ladder.check_control(control, {"engine": "rust"}) == naive
    with pytest.raises(BenchError, match="control"):
        ladder.check_control(control, {"engine": "python"})


def local_dependencies(root, path, tracked):
    source = (root / path).read_text(encoding="utf-8-sig")
    if path.suffix == ".ps1":
        code = re.sub(r"<#.*?#>", "", source, flags=re.DOTALL)
        names = re.findall(r"['\"][^'\"\r\n]*?\b([\w.-]+\.(?:py|ps1))['\"]",
                           "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#")))
        return {path.parent / name for name in names} & tracked

    def resolve(name, level=0):
        dirs = [path.parents[level - 1]] if level else [path.parent, Path(), Path("evals")]
        for directory in dirs:
            prefix = directory.joinpath(*name.split("."))
            if prefix.name and prefix.with_suffix(".py") in tracked:
                return prefix.with_suffix(".py")
            if prefix / "__init__.py" in tracked:
                return prefix / "__init__.py"
        return None

    dependencies = set()
    for node in ast.walk(ast.parse(source, filename=path.as_posix())):
        if isinstance(node, ast.Import):
            dependencies.update(resolve(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            dependencies.add(resolve(module, node.level))
            prefix = module + "." if module else ""
            dependencies.update(resolve(prefix + alias.name, node.level) for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            arg = node.args[0]
            if (name in {"__import__", "import_module", "run_module"}
                    and isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                dependencies.add(resolve(arg.value))
    return dependencies - {None}


def frozen_imports_from(root, kept, tracked):
    edges = {}
    violations = []
    for start in kept:
        pending = deque([(start,)])
        visited = set()
        while pending:
            chain = pending.popleft()
            path = chain[-1]
            if path.parts[0] == "evals" and path.name.startswith("_python_"):
                violations.append(" -> ".join(item.as_posix() for item in chain))
                break
            if path in visited:
                continue
            visited.add(path)
            if path not in edges:
                edges[path] = local_dependencies(root, path, tracked)
            pending.extend((*chain, dependency) for dependency in sorted(edges[path]))
    return violations


def test_kept_evals_do_not_import_frozen_implementations():
    root = Path(__file__).resolve().parents[1]
    tracking = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=True)
    tracked = {Path(path) for path in tracking.split("\0") if path}
    with (root / "rust/ci/package_consumers.toml").open("rb") as stream:
        rules = tomllib.load(stream)["rules"]
    retiring = [glob for rule in rules if rule.get("retire_with_package") for glob in rule["globs"]]
    kept = [path for path in sorted(tracked) if path.parts[0] == "evals" and path.suffix in {".py", ".ps1"}
            and not any(fnmatchcase(path.as_posix(), pattern) for pattern in retiring)]
    assert retiring and Path("evals/longmemeval_bench.py") in kept
    assert not frozen_imports_from(root, kept, tracked)


def test_frozen_import_check_follows_indirect_and_literal_dynamic_imports(tmp_path):
    sources = {"evals/active.py": "from evals import helper\n",
               "evals/helper.py": "import importlib\nimportlib.import_module('evals._python_old')\n",
               "evals/_python_old.py": ""}
    tracked = {Path(path) for path in sources}
    (tmp_path / "evals").mkdir()
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    assert frozen_imports_from(tmp_path, [Path("evals/active.py")], tracked) == [
        "evals/active.py -> evals/helper.py -> evals/_python_old.py",
    ]
