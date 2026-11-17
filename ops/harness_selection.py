"""Read a batch head's harness coverage without importing candidate code."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import fnmatch
import json
from pathlib import PurePosixPath
import re
import shlex

import yaml

WORKFLOW = ".github/workflows/rust.yml"
CLASSIFICATION = "rust/ci/harness-test-selections.json"
WRAPPERS = frozenset({"rust/daemon/harness/schema_ci.py", "rust/daemon/harness/startup_ci.py"})
NATIVE_PROGRAMS = frozenset({
    "rust/cli_harness", "rust/cli_harness/lease_ci.py",
    "rust/daemon/harness/run.py", "rust/daemon/harness/dream_cursors.py",
    "rust/daemon/harness/graph_store.py", "rust/daemon/harness/principals.py",
    "rust/daemon/harness/gen_graph_unicode.py",
})


class SelectionError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    runner: str
    paths: tuple[str, ...]
    start: str = ""
    pattern: str = ""

    def record(self):
        return {"runner": self.runner, "paths": list(self.paths),
                "start": self.start, "pattern": self.pattern}


@dataclass(frozen=True)
class Recipe:
    commands: tuple[Command, ...]
    requirements: tuple[str, ...] = ()

    def record(self):
        result = [command.record() for command in self.commands]
        if self.requirements:
            result[0]["requirements"] = list(self.requirements)
        return result

    def display(self):
        out = []
        for command in self.commands:
            if command.runner == "pytest":
                out.append("python -m pytest -q -p no:cacheprovider --require-databases " + " ".join(command.paths))
            elif command.runner == "unittest-discover":
                out.append(f"python -m unittest discover -s {command.start} -p {command.pattern}")
            else:
                out.append("python " + command.paths[0])
        return " && ".join(out)


def _path(value):
    if not isinstance(value, str):
        raise SelectionError("a harness path is not a string")
    file = value.split("::", 1)[0]
    path = PurePosixPath(file)
    if (not file.startswith("rust/") or "\\" in file or path.is_absolute()
            or ".." in path.parts or any(c.isspace() for c in file)):
        raise SelectionError(f"unsupported harness path {value!r}")
    return file


def literal_harness(source):
    try:
        tree = ast.parse(source)
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "RUST_HARNESS" for target in node.targets)]
        if len(values) != 1:
            raise ValueError("expected one RUST_HARNESS definition")
        paths = ast.literal_eval(values[0])
        if not isinstance(paths, (tuple, list)) or not paths:
            raise ValueError("expected a nonempty literal path sequence")
        for path in paths:
            _path(path)
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate harness path")
        return tuple(paths)
    except (SyntaxError, TypeError, ValueError) as error:
        raise SelectionError(f"cannot read the batch head's RUST_HARNESS: {error}") from None


def _steps(workflow, job):
    try:
        steps = workflow["jobs"][job]["steps"]
        if not isinstance(steps, list):
            raise ValueError("steps is not a list")
        return steps
    except (KeyError, TypeError, ValueError) as error:
        raise SelectionError(f"cannot read rust.yml {job}: {error}") from None


def _lines(step):
    if not isinstance(step, dict):
        raise SelectionError("a workflow step is not a mapping")
    run = step.get("run", "")
    if not isinstance(run, str):
        raise SelectionError("a workflow run command is not text")
    for line in run.replace("`\n", " ").replace("\\\n", " ").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                yield shlex.split(line)
            except ValueError as error:
                raise SelectionError(f"cannot parse workflow command: {error}") from None


def _pytest(words, *, filtered=False):
    index = words.index("pytest")
    if words[:index] not in (["python", "-m"], ["python3", "-m"], []):
        raise SelectionError("unsupported pytest launcher in rust.yml")
    arguments = words[index + 1:]
    if any(word.startswith(("--ignore", "--deselect", "--pyargs")) for word in arguments):
        raise SelectionError("pytest selection is filtered by ignore, deselect or pyargs")
    if not filtered and any(word in ("-k", "-m") for word in arguments):
        raise SelectionError("the Rust job's pytest selection is filtered; declare its complete selection")
    paths = []
    position = 0
    while position < len(arguments):
        word = arguments[position]
        position += 1
        if word.startswith("rust/"):
            _path(word)
            paths.append(word)
        elif word in {"-q", "-v", "-vv", "-ra", "--require-databases"}:
            continue
        elif word.startswith(("--durations=", "--junitxml=")) and word.partition("=")[2]:
            continue
        elif word in {"-p", "--junitxml", "-k", "-m"}:
            if position == len(arguments) or arguments[position].startswith("-"):
                raise SelectionError(f"pytest option {word} has no readable value")
            value = arguments[position]
            position += 1
            if word == "-p" and value != "no:cacheprovider":
                raise SelectionError("unknown pytest plugin; classify its selection before gating")
            if word == "--junitxml" and value == "(Join-Path":
                if position + 1 >= len(arguments) or not arguments[position + 1].endswith(")"):
                    raise SelectionError("unreadable pytest report path")
                position += 2
            elif word == "--junitxml" and value == "${{":
                if position + 1 >= len(arguments) or not arguments[position + 1].startswith("}}/"):
                    raise SelectionError("unreadable pytest report path")
                position += 2
        elif (word == ">" and position < len(arguments)
              and re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", arguments[position])
              and arguments[position + 1:] in ([], ["2>&1"])):
            position = len(arguments)  # Known parity mutation report redirection.
        else:
            raise SelectionError(f"unknown pytest argument {word}; classify the complete selection before gating")
    if not paths:
        raise SelectionError("pytest command names no literal Rust test path")
    for path in paths:
        _path(path)
    return tuple(paths)


def _is_pytest(words):
    return (words[:1] == ["pytest"] or words[:3] in
            (["python", "-m", "pytest"], ["python3", "-m", "pytest"]))


def _classification(source):
    try:
        value = json.loads(source)
        if set(value) != {"schema", "files"} or value["schema"] != 1 or not isinstance(value["files"], dict):
            raise ValueError("unsupported classification schema")
        for path, entry in value["files"].items():
            _path(path)
            if (not isinstance(entry, dict) or set(entry) - {"kind", "reason", "native_tests", "unit_tests", "requirements"}
                    or entry.get("kind") not in ("unit", "native", "mixed")
                    or not isinstance(entry.get("reason"), str) or not entry["reason"].strip()):
                raise ValueError(f"unclassified test file {path}")
            requirements = entry.get("requirements", [])
            if (not isinstance(requirements, list) or
                    not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
                            for name in requirements)):
                raise ValueError(f"invalid fixture requirements for {path}")
            if entry["kind"] == "mixed":
                selectors = [entry[key] for key in ("native_tests", "unit_tests") if key in entry]
                if (len(selectors) != 1 or not isinstance(selectors[0], list) or not selectors[0]
                        or not all(isinstance(name, str) for name in selectors[0])):
                    raise ValueError(f"mixed test file {path} needs one explicit selector list")
        return value["files"]
    except (TypeError, ValueError) as error:
        raise SelectionError(f"cannot read parity test classification: {error}") from None


def _test_names(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise SelectionError(f"cannot inspect mixed test file: {error}") from None
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef):
            names.extend(node.name + "::" + child.name for child in node.body
                         if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_"))
    return names


def _units(path, classifications, read):
    file = _path(path)
    if file not in classifications:
        raise SelectionError(f"parity test {file} has no candidate-dependency classification")
    entry = classifications[file]
    if entry["kind"] == "native":
        return ()
    if entry["kind"] == "unit":
        return (path,)
    names = _test_names(read(file))
    chosen = entry.get("unit_tests", entry.get("native_tests"))
    if not set(chosen) <= set(names):
        raise SelectionError(f"mixed classification for {file} names missing tests")
    selected = chosen if "unit_tests" in entry else [name for name in names if name not in chosen]
    return tuple(file + "::" + name for name in selected)


def _unittest_from_wrapper(path, source, tree_paths):
    """Recognise literal unittest discovery arrays in the two CI wrappers."""
    here = str(PurePosixPath(path).parent)

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id == "HERE":
            return here
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str" and len(node.args) == 1:
            return literal(node.args[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return literal(node.left) + "/" + literal(node.right)
        raise SelectionError(f"unsupported unittest selector expression in {path}")

    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise SelectionError(f"cannot parse unittest wrapper {path}: {error}") from None
    commands = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 4:
            continue
        if not (isinstance(node.elts[1], ast.Constant) and node.elts[1].value == "-m"
                and isinstance(node.elts[2], ast.Constant) and node.elts[2].value == "unittest"):
            continue
        words = [literal(item) for item in node.elts[1:]]
        if words[:3] != ["-m", "unittest", "discover"] or set(words[3::2]) != {"-s", "-p"} or len(words) != 7:
            raise SelectionError(f"unsupported unittest command in {path}")
        options = dict(zip(words[3::2], words[4::2]))
        start, pattern = options["-s"], options["-p"]
        _path(start)
        if "/" in pattern or "\\" in pattern:
            raise SelectionError(f"unsupported unittest discovery pattern in {path}")
        files = tuple(sorted(p for p in tree_paths if p.startswith(start + "/")
                             and fnmatch.fnmatch(PurePosixPath(p).name, pattern)))
        if not files:
            raise SelectionError(f"unittest discovery in {path} selects no files")
        commands.append(Command("unittest-discover", files, start, pattern))
    if not commands:
        raise SelectionError(f"no readable unittest commands in {path}")
    return commands


def recipe(read, tree_paths):
    """read(path) and tree_paths must refer to the same immutable commit."""
    try:
        workflow = yaml.safe_load(read(WORKFLOW))
    except (yaml.YAMLError, OSError, KeyError) as error:
        raise SelectionError(f"cannot read batch-head rust.yml: {error}") from None
    listed = literal_harness(read("ops/integrate.py"))
    rust_paths = set()
    for step in _steps(workflow, "rust"):
        for words in _lines(step):
            if _is_pytest(words):
                rust_paths.update(_pytest(words))
            elif "pytest" in words and words[:4] not in (["python", "-m", "pip", "install"],
                                                         ["python3", "-m", "pip", "install"]):
                raise SelectionError("unsupported pytest launcher in the Rust job")
    if not rust_paths:
        raise SelectionError("the Rust job declares no readable pytest selections")
    missing = sorted(rust_paths - set(listed))
    if missing:
        raise SelectionError("RUST_HARNESS omits Rust-job paths: " + ", ".join(missing))
    classifications = _classification(read(CLASSIFICATION))
    pytest_paths = set(listed)
    additional = []
    for step in _steps(workflow, "parity-checks"):
        for words in _lines(step):
            if _is_pytest(words):
                paths = _pytest(words, filtered=True)
                arguments = words[words.index("pytest") + 1:]
                if "-k" in arguments or "-m" in arguments:
                    if not all(classifications.get(_path(p), {}).get("kind") == "native" for p in paths):
                        raise SelectionError("filtered parity command lacks a native-only classification")
                    continue
                for path in paths:
                    if _path(path) in listed:
                        pytest_paths.add(path)
                    else:
                        pytest_paths.update(_units(path, classifications, read))
                continue
            if "pytest" in words and words[:4] not in (["python", "-m", "pip", "install"],
                                                       ["python3", "-m", "pip", "install"]):
                raise SelectionError("unsupported pytest launcher in parity-checks")
            if not words or words[0] not in ("python", "python3"):
                continue
            if words[1:3] in (["-m", "pip"], ["-m", "venv"]):
                continue
            if len(words) < 2:
                raise SelectionError("incomplete Python parity command")
            path = words[1]
            if path in WRAPPERS:
                for command in _unittest_from_wrapper(path, read(path), tree_paths):
                    for file in command.paths:
                        if file not in rust_paths and classifications.get(file, {}).get("kind") != "unit":
                            raise SelectionError(f"unittest file {file} has no candidate-free classification")
                    additional.append(command)
            elif path.startswith("rust/") and PurePosixPath(path).name.startswith("test_"):
                if classifications.get(path, {}).get("kind") != "unit":
                    raise SelectionError(f"direct unittest file {path} has no candidate-free classification")
                additional.append(Command("unittest-file", (path,)))
            elif path not in NATIVE_PROGRAMS:
                raise SelectionError(f"unknown Python parity command {path}; classify it before gating")
    available = set(tree_paths)
    for path in list(pytest_paths) + [path for command in additional for path in command.paths]:
        file = _path(path)
        if file not in available and not any(p.startswith(file.rstrip("/") + "/") for p in available):
            raise SelectionError(f"harness path {file} is absent from the batch head")
    commands = [Command("pytest", tuple(sorted(pytest_paths)))]
    commands.extend(sorted(set(additional), key=lambda c: (c.runner, c.paths, c.pattern)))
    # Candidate-free fixtures can need optional packages installed explicitly
    # in the parity job. Bind those to that same head's pins, not a persistent
    # environment or a second set of versions in the controller.
    needed = {name for command in commands for path in command.paths
              for name in classifications.get(_path(path), {}).get("requirements", [])}
    pins = {name: set() for name in needed}
    for step in _steps(workflow, "parity-checks"):
        for words in _lines(step):
            if words[:4] not in (["python", "-m", "pip", "install"], ["python3", "-m", "pip", "install"]):
                continue
            for word in words[4:]:
                match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.+!-]*)", word)
                if match and match[1] in pins:
                    pins[match[1]].add(word)
    for name, choices in pins.items():
        if len(choices) != 1:
            raise SelectionError(f"fixture dependency {name} needs one exact workflow pin in parity-checks")
    requirements = tuple(sorted(next(iter(choices)) for choices in pins.values()))
    return Recipe(tuple(commands), requirements)
