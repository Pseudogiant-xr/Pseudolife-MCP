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
PARITY_JOBS = ("parity-checks", "parity-selfhosted")
SETUP_ACTIONS = frozenset({
    "actions/checkout@v4", "actions/setup-python@v5", "actions/cache@v4",
    "actions/upload-artifact@v4", "actions/download-artifact@v4",
    "dtolnay/rust-toolchain@stable", "Swatinem/rust-cache@v2",
})
# These are routing, generation, build, image-lane or manual-stress entry
# points, not an open-ended exemption for Python in another job.
JOB_PROGRAMS = {
    "scope": {"rust/ci/parity_scope.py", "ops/ci_batch_passed.py"},
    "rust": {"rust/ci/pr_precheck.py", "rust/ci/pr_precheck_run.py",
             "rust/daemon/harness/gen_schema_sql.py", "rust/producer_census.py", "rust/ci/inventory.py"},
    "fixtures": {"rust/daemon/harness/schema_ci.py"},
    "parity-pool": {"rust/ci/linux_pool.py"},
    "daemon-image": {"rust/daemon/container_inputs.py", "rust/daemon/test_container.py",
                     "rust/daemon/container_probe.py", "rust/daemon/harness/image_model_proof.py"},
    "stress-plan": {"rust/ci/stress.py"},
    "stress-build": {"rust/ci/stress.py"},
    "stress": {"rust/ci/stress.py", "rust/cli_harness/lease_ci.py"},
    "stress-report": {"rust/ci/stress.py", "rust/ci/stress_history.py"},
}
JOB_MODULES = {"scope": {"ops.ci_push_passed"}, "stress": {"evals.rust_port.oracle_selection"}}


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


def _default_directory(value, inherited):
    defaults = value.get("defaults", {})
    if not isinstance(defaults, dict) or not isinstance(defaults.get("run", {}), dict):
        raise SelectionError("line 1: unreadable working-directory defaults")
    return defaults.get("run", {}).get("working-directory", inherited)


def _numbered_lines(step):
    if not isinstance(step, dict):
        raise SelectionError("a workflow step is not a mapping")
    run = step.get("run", "")
    if not isinstance(run, str):
        raise SelectionError("a workflow run command is not text")
    pending, start = "", 1
    for number, raw in enumerate(run.splitlines(), 1):
        # A shell comment cannot continue its next physical line. If the
        # preceding command continued into this comment, its newline ends it.
        comment = raw.lstrip().startswith("#")
        if comment and not pending:
            continue
        if not pending:
            start = number
        line = pending + ("" if comment else raw.strip())
        if not comment and line.endswith(("`", "\\")):
            pending = line[:-1] + " "
            continue
        pending = ""
        if line and not line.startswith("#"):
            try:
                yield start, line, shlex.split(line)
            except ValueError as error:
                # Run bodies can be Python or another language. A shell
                # lexer failure without a test token is a classified skip.
                if _test_bearing(line):
                    raise SelectionError(f"line {start}: cannot parse workflow command: {error}") from None
    if pending and _test_bearing(pending):
        raise SelectionError(f"line {start}: unfinished workflow command continuation")


def _lines(step):
    for _, _, words in _numbered_lines(step):
        yield words


def _test_bearing(text):
    return bool(re.search(r"\brust[/\\][^\s\"']*\.(?:py|sh|ps1)\b|pytest\b|(?:^|\s)-m\s*unittest\b", text))


def _compound(text):
    # File-descriptor duplication is one command's reporting, not another
    # shell command. _pytest validates the supported redirection separately.
    text = re.sub(r"(?<!\S)[12]>&[12](?=\s|$)", "", text)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    for index, token in enumerate(lexer):
        if token and set(token) <= set(";&|"):
            if index == 0 and token == "&" and text.startswith("& "):
                continue  # PowerShell's call operator, not a bash separator.
            return True
    return False


def _classify_line(job, text, words, *, directory="."):
    launcher = words[0] if words else ""
    python = launcher in {"python", "python3", "python.exe", "py", "py.exe", "pytest"}
    called_python = launcher == "&" and len(words) > 1 and "python" in words[1].lower()
    embedded_python = bool(re.search(
        r"(?:^|[;&|{}])\s*(?:&\s*)?(?:python3?|py|pytest|\$(?:env:)?\w*python\w*)\b(?!\s*=)", text, re.I))
    shell = launcher in {"bash", "sh", "pwsh", "powershell"}
    script = any(re.search(r"\.(?:sh|ps1)$", word, re.I) for word in words)
    if not (_test_bearing(text) or python or called_python or embedded_python or shell or script):
        return
    if _compound(text):
        raise SelectionError("compound test-bearing commands need an explicit classification")
    if shell or script:
        raise SelectionError("unclassified shell script invocation; declare its test coverage before gating")
    literal = words[:1] in (["python"], ["python3"])
    pip = words[:4] in (["python", "-m", "pip", "install"], ["python3", "-m", "pip", "install"])
    # The existing runtime-setup step uses its just-created interpreter only
    # to install packages. This does not license variable test launchers.
    pip = pip or (words[:2] == ["&", "$python"] and words[2:5] == ["-m", "pip", "install"])
    if pip:
        return
    if literal and words[1:3] == ["-m", "venv"]:
        return
    if directory not in (".", "./", "${{ github.workspace }}"):
        raise SelectionError("test/script working-directory must be the explicit repository root; "
                             "put it in a root-directory step without cd or Set-Location")
    if _is_pytest(words):
        if job not in ("rust", *PARITY_JOBS):
            raise SelectionError("pytest is not classified as a test runner in this job")
        _pytest(words, filtered=job != "rust")
        return
    if "pytest" in words:
        raise SelectionError("unsupported pytest launcher")
    if not literal:
        if (job == "rust" and words[:3] == ["&", "$env:PRECHECK_PYTHON", "rust/ci/pr_precheck_run.py"]):
            program, arguments = words[2], words[3:]
        else:
            raise SelectionError("unsupported Python launcher for a Rust script or unittest command")
    elif len(words) >= 2:
        program, arguments = words[1], words[2:]
    else:
        raise SelectionError("incomplete Python command")
    if program == "-m" and arguments[:1] and arguments[0] in JOB_MODULES.get(job, set()):
        return
    if any(symbol in program for symbol in ("$", "{", "}", "`")):
        raise SelectionError("non-literal Python script argument; declare its literal path before gating")
    if job in PARITY_JOBS and (program in WRAPPERS or program in NATIVE_PROGRAMS):
        return
    if job in PARITY_JOBS and program.startswith("rust/") and PurePosixPath(program).name.startswith("test_"):
        if arguments:
            raise SelectionError("unsupported direct unittest arguments; declare the literal file without options")
        return
    if program not in JOB_PROGRAMS.get(job, set()):
        raise SelectionError(f"unclassified Rust script or unittest command {program!r}")
    if program == "rust/ci/pr_precheck_run.py":
        if "--only" not in arguments or arguments[arguments.index("--only") + 1:][:1] != ["goldens"]:
            raise SelectionError("PR precheck driver is classified only for --only goldens")
    if job == "fixtures" and ("--build-only" not in arguments or "--no-build" in arguments):
        raise SelectionError("fixture wrapper is classified only for --build-only")
    if program == "rust/ci/stress.py":
        expected = {"stress-plan": "plan", "stress-build": "archive", "stress": "run", "stress-report": "report"}[job]
        if arguments[:1] != [expected]:
            raise SelectionError(f"stress driver is classified only for {expected}")
    if program == "rust/daemon/test_container.py" and arguments:
        raise SelectionError("image unittest entry point is classified without arguments")
    if any(arguments[index:index + 2] == ["-m", "unittest"] for index in range(len(arguments))):
        raise SelectionError("unclassified unittest arguments to a workflow helper")


def _audit_jobs(workflow):
    try:
        jobs = workflow["jobs"]
        if not isinstance(jobs, dict):
            raise ValueError("jobs is not a mapping")
    except (KeyError, TypeError, ValueError) as error:
        raise SelectionError(f"cannot read rust.yml jobs: {error}") from None
    result = {}
    for job, value in jobs.items():
        if not isinstance(value, dict) or "uses" in value:
            raise SelectionError(f"rust.yml job {job!r}, step 'reusable job', line 1: unclassified uses job")
        result[job] = []
        for index, step in enumerate(_steps(workflow, job), 1):
            label = step.get("name", step.get("id", f"step {index}")) if isinstance(step, dict) else f"step {index}"
            where = f"rust.yml job {job!r}, step {label!r} (#{index})"
            if not isinstance(step, dict):
                raise SelectionError(f"{where}, line 1: step is not a mapping")
            if "uses" in step:
                if not isinstance(step["uses"], str) or step["uses"] not in SETUP_ACTIONS or "run" in step:
                    raise SelectionError(f"{where}, line 1: unclassified uses step {step['uses']!r}")
                if not isinstance(step.get("with", {}), dict):
                    raise SelectionError(f"{where}, line 1: uses inputs are not a mapping")
                if any(key in step.get("with", {}) for key in ("script", "run", "command")):
                    raise SelectionError(f"{where}, line 1: executable uses inputs are not classified")
                continue
            try:
                directory = step.get("working-directory", _default_directory(
                    value, _default_directory(workflow, ".")))
                for number, text, words in _numbered_lines(step):
                    location = f"{where}, line {number}"
                    try:
                        if words[:1] and words[0].lower() in {"cd", "pushd", "set-location", "push-location"}:
                            directory = None  # Never infer state from an imperative shell command.
                        _classify_line(job, text, words, directory=directory)
                    except SelectionError as error:
                        raise SelectionError(f"{location}: {error}: {text}") from None
                    result[job].append((location, words))
            except SelectionError as error:
                if str(error).startswith("rust.yml job "):
                    raise
                raise SelectionError(f"{where}, {error}") from None
    return result


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
    audited = _audit_jobs(workflow)
    rust_paths = set()
    rust_locations = {}
    for where, words in audited.get("rust", []):
        if _is_pytest(words):
            for path in _pytest(words):
                rust_paths.add(path)
                rust_locations[path] = where
    if not rust_paths:
        raise SelectionError("the Rust job declares no readable pytest selections")
    missing = sorted(rust_paths - set(listed))
    if missing:
        raise SelectionError("RUST_HARNESS omits Rust-job paths: " + ", ".join(
            f"{path} ({rust_locations[path]})" for path in missing))
    classifications = _classification(read(CLASSIFICATION))
    pytest_paths = set(listed)
    additional = []
    parity_steps = [step for job in PARITY_JOBS if job in audited for step in _steps(workflow, job)]
    for step in parity_steps:
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
            if words and words[0] not in ("python", "python3") and any(word in ("python", "python3") for word in words):
                raise SelectionError("unsupported Python launcher in parity-checks")
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
                if len(words) != 2:
                    raise SelectionError("unsupported direct unittest arguments; declare the literal file without options")
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
    for step in parity_steps:
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
