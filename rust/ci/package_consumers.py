"""Inventory checkout consumers of the frozen Python package and its metadata.

Run python rust/ci/package_consumers.py to see path, line, kind and class.
If a new dependency is intentional, add a narrowly scoped glob and kind with
a reason to package_consumers.toml. Unused rules only appear as report notes.
The batch harness runs the guard through its existing rust/ci pytest selection.
For the manual package-absent check, run package_absent.py --help.

This is a static inventory of recognizable source and command forms. Computed
module names, commands assembled at runtime and indirect imports need runtime
evidence. Citations, ordinary prose, and recorded output are outside the scan.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
from textwrap import dedent
import tomllib


ROOT = Path(__file__).resolve().parents[2]
RULES = "rust/ci/package_consumers.toml"
PACKAGE = "pseudolife_memory"
ACTIVE = "active dependency"
LEGACY = "intentional legacy compatibility"
TESTING = "retained test tooling"
HISTORY = "historical evidence"
EVALS = "awaits the retained-evals decision"
MODULE = rf"{PACKAGE}(?:\.[a-zA-Z_]\w*)*(?![\w.])"
PACKAGE_PATH = rf"(?<![\w.-]){PACKAGE}[\\/]+[\w.*-]"
METADATA_PATH = r"(?<![\w.-])(?:pyproject\.toml|requirements\.lock\.txt)(?![\w.-])"
PACKAGE_COMMAND = re.compile(
    rf"(?<!\S)-m\s+{MODULE}|['\"]-m['\"](?:\.into\(\))?\s*,\s*['\"]{MODULE}['\"]"
    rf"|\b(?:cat|cp|type|python[23]?|python\.exe)\s+['\"]?[^'\"\s]*{PACKAGE_PATH}"
)
INSTALL = re.compile(
    r"\b(?:pip[3x]?|uv\s+pip)\s+install\b[^#\n]*?"
    r"(?:\s|=)['\"]?(?:\.(?:\[[^]]+\])?|/app|\$(?:root|gate)\[[^]]+\]"
    r"|pseudolife-mcp(?:\[[^]]+\])?(?:[<>=!~][^'\"\s]+)?)"
    r"(?=['\"\s;&|]|$)"
)
BUILD = re.compile(r"\bpython[23]?\s+-m\s+build(?:\s|$)")
PYPI = re.compile(r"\bpypi\.org/(?:pypi|project)/pseudolife-mcp(?:[/'\"]|$)")
PYTHON_RECIPE = re.compile(rf"(?m)^\s*(?:from\s+{MODULE}\s+import\b|import\s+{MODULE})")
QUOTED_PACKAGE = re.compile(rf"""["']{PACKAGE}["']""")
DOC_SCRIPT = re.compile(r"\bpython[23]?\s+(?:\./)?([\w./-]+\.py)(?=[\s\x60'\";]|$)")
DOC_SUFFIXES = {".md", ".rst"}
DOC_NAMES = {"llms.txt", "llms-full.txt"}
SOURCE_SUFFIXES = {".py", ".rs", ".sh", ".ps1", ".yml", ".yaml", ".toml"}
CONFIG_NAMES = {"server.json", "launch.json", ".mcp.json"}
KINDS = {"command", "docker-copy", "documentation", "metadata", "path",
         "python-import", "rust-include", "workflow-install"}
CATEGORIES = (ACTIVE, TESTING, LEGACY, HISTORY, EVALS)
PYTHON_BIN = r"""(?<![\w.-])(?:python(?:[23](?:\.\d+)?)?(?:\.exe)?|\$\w*(?:python|_py)\w*)['"]?"""
INLINE_PYTHON = re.compile(rf"""{PYTHON_BIN}[ \t]+-c[ \t]+(?P<quote>['"])
    (?P<code>(?:\\.|(?!(?P=quote)).)*)(?P=quote)""", re.I | re.S | re.X)
HEREDOC_PYTHON = re.compile(rf"""{PYTHON_BIN}[ \t]+(?:-[ \t]*)?<<-?[ \t]*['"]?
    (?P<end>[a-z_]\w*)['"]?[^\n]*\n(?P<code>.*?)^[ \t]*(?P=end)[ \t]*$""",
    re.I | re.M | re.S | re.X)


@dataclass(frozen=True, order=True)
class Consumer:
    path: str
    line: int
    kind: str

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line} ({self.kind})"


@dataclass(frozen=True)
class Rule:
    glob: str
    kind: str
    category: str
    reason: str

    def matches(self, hit: Consumer) -> bool:
        return hit.kind == self.kind and fnmatchcase(hit.path, self.glob)


def module_name(name: str | None) -> bool:
    return bool(name and re.fullmatch(MODULE, name))


def python_hits(source: str):
    """Recognize imports, import helpers, and strings that carry paths or commands."""
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    docstrings = {
        node.body[0].value
        for node in nodes
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and ast.get_docstring(node) is not None
    }
    loaders = {"__import__", "importlib.import_module"}
    for node in nodes:
        if isinstance(node, ast.Import):
            if any(module_name(alias.name) for alias in node.names):
                yield node.lineno, "python-import"
            for alias in node.names:
                if alias.name == "importlib" and alias.asname:
                    loaders.add(f"{alias.asname}.import_module")
        elif isinstance(node, ast.ImportFrom):
            if not node.level and module_name(node.module):
                yield node.lineno, "python-import"
            if node.module == "importlib" and not node.level:
                loaders.update(alias.asname or alias.name for alias in node.names
                               if alias.name == "import_module")

    for node in nodes:
        if isinstance(node, ast.Call):
            if isinstance(node.func, (ast.Name, ast.Attribute)) and ast.unparse(node.func) in loaders:
                argument = node.args[0] if node.args else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "name"), None)
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    if module_name(argument.value):
                        yield node.lineno, "python-import"
            if (isinstance(node.func, ast.Attribute) and node.func.attr in {"joinpath", "join"}
                    or isinstance(node.func, ast.Name) and node.func.id == "Path"):
                if any(isinstance(arg, ast.Constant) and arg.value == PACKAGE for arg in node.args):
                    yield node.lineno, "path"
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if isinstance(node.right, ast.Constant) and node.right.value == PACKAGE:
                yield node.right.lineno, "path"
        elif isinstance(node, (ast.List, ast.Tuple)):
            for flag, name in zip(node.elts, node.elts[1:]):
                if (isinstance(flag, ast.Constant) and flag.value == "-m"
                        and isinstance(name, ast.Constant) and isinstance(name.value, str)
                        and module_name(name.value)):
                    yield flag.lineno, "command"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings:
            for match in PYTHON_RECIPE.finditer(node.value):
                yield node.lineno + node.value.count("\n", 0, match.start()), "python-import"
            yield from literal_hits(node.lineno, node.value, "command")
    if any(isinstance(node, ast.ImportFrom) and node.module == "setuptools" for node in nodes):
        for node in nodes:
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value.startswith(PACKAGE)):
                yield node.lineno, "metadata"


def literal_hits(line: int, value: str, command_kind: str):
    if PACKAGE_COMMAND.search(value) or INSTALL.search(value):
        yield line, command_kind
    elif re.search(PACKAGE_PATH, value):
        yield line, "path"
    if re.search(METADATA_PATH, value) or PYPI.search(value):
        yield line, "metadata"


def docker_sources(arguments: str) -> list[str]:
    arguments = re.sub(r"^(?:--\S+\s+)+", "", arguments)
    if arguments.startswith("["):
        return json.loads(arguments)[:-1]
    return shlex.split(arguments, comments=True)[:-1]


def embedded_python_hits(source: str):
    for pattern in (INLINE_PYTHON, HEREDOC_PYTHON):
        for match in pattern.finditer(source):
            line_start = source.rfind("\n", 0, match.start()) + 1
            if source[line_start:match.start()].lstrip().startswith("#"):
                continue
            code = match["code"]
            if PACKAGE not in code:
                continue
            start = source.count("\n", 0, match.start("code"))
            try:
                yield from ((start + line, kind) for line, kind in python_hits(dedent(code))
                            if kind == "python-import")
            except SyntaxError:
                # Shell interpolation can leave the original text incomplete.
                for statement in PYTHON_RECIPE.finditer(code):
                    yield start + code.count("\n", 0, statement.start()) + 1, "python-import"


def logical_lines(source: str, comment: str):
    """Join shell/Docker/PowerShell continuations and skip full-line comments."""
    pending = ""
    start = 1
    for line, text in enumerate(source.splitlines(), 1):
        text = text.strip()
        if not text or text.startswith(comment):
            continue
        if not pending:
            start = line
        if text.endswith(("\\", "\x60")):
            pending += text[:-1] + " "
        else:
            yield start, pending + text
            pending = ""
    if pending:
        yield start, pending


RUST_CHAR = r"'(?:\\.|[^'\\\n])'"
RUST_QUOTED = r'r(#+)"(.*?)"\1|"(?:\\.|[^"\\])*"'
RUST_STRING = re.compile(f"{RUST_CHAR}|{RUST_QUOTED}", re.S)
RUST_COMMENT_OR_STRING = re.compile(rf"/\*.*?\*/|//[^\n]*|{RUST_CHAR}|{RUST_QUOTED}", re.S)


def rust_hits(source: str, python_scripts: frozenset[str]):
    source = RUST_COMMENT_OR_STRING.sub(
        lambda m: " " + "\n" * m[0].count("\n") if m[0].startswith("/") else m[0], source)
    # Includes can contain nested macros or span more than one source line.
    includes = re.compile(
        r'\binclude_(?:str|bytes)!\s*\(\s*(?:concat!\s*\(\s*env!\s*\("[^"]+"\)\s*,\s*)?'
        r'"([^"]+)"')
    include_strings = set()
    for match in includes.finditer(source):
        value = match[1]
        if re.search(PACKAGE_PATH, value) or re.search(METADATA_PATH, value):
            include_strings.add(match.start(1) - 1)
            yield source.count("\n", 0, match.start()) + 1, "rust-include"
    for match in RUST_STRING.finditer(source):
        if not match[0].startswith("'") and match.start() not in include_strings:
            line = source.count("\n", 0, match.start()) + 1
            value = match[2] if match[2] is not None else match[0][1:-1]
            yield from literal_hits(line, value, "command")
            if any(script[1] in python_scripts for script in DOC_SCRIPT.finditer(value)):
                yield line, "command"
    for match in re.finditer(rf"\.join\(\s*{QUOTED_PACKAGE.pattern}\s*\)", source):
        yield source.count("\n", 0, match.start()) + 1, "path"
    for match in PACKAGE_COMMAND.finditer(source):
        yield source.count("\n", 0, match.start()) + 1, "command"


def scan(path: str, source: str, python_scripts: frozenset[str] = frozenset()) -> list[Consumer]:
    source = source.replace("\r\n", "\n")
    parts = PurePosixPath(path)
    doc = parts.suffix in DOC_SUFFIXES or parts.name in DOC_NAMES
    workflow = path.startswith(".github/workflows/")
    docker = "Dockerfile" in parts.name
    if path.startswith(PACKAGE + "/") or parts.suffix in {".log", ".jsonl"}:
        return []
    if parts.suffix == ".py":
        hits = list(python_hits(source))
    elif parts.suffix == ".rs":
        hits = list(rust_hits(source, python_scripts))
    elif doc:
        hits = [(line, "documentation") for line, text in enumerate(source.splitlines(), 1)
                if PACKAGE_COMMAND.search(text) or INSTALL.search(text)
                or any(match[1] in python_scripts for match in DOC_SCRIPT.finditer(text))]
    elif workflow or docker or parts.suffix in SOURCE_SUFFIXES or parts.name in CONFIG_NAMES:
        hits = list(embedded_python_hits(source))
        for line, text in logical_lines(source, "#"):
            copy = re.match(r"(?i)(?:COPY|ADD)\s+(.*)", text) if docker else None
            if copy:
                for value in docker_sources(copy[1]):
                    if value.strip("/\\") == PACKAGE or re.search(PACKAGE_PATH, value) or re.search(METADATA_PATH, value):
                        hits.append((line, "docker-copy"))
                continue
            if workflow and (INSTALL.search(text) or BUILD.search(text)):
                hits.append((line, "workflow-install"))
                continue
            hits.extend(literal_hits(line, text, "command"))
            if parts.name == "pyproject.toml" and re.search(rf"""["']{MODULE}(?::\w+)?["']""", text):
                hits.append((line, "metadata"))
    else:
        hits = []
    return sorted({Consumer(path, line, kind) for line, kind in hits})


def tracked_files(root: Path):
    index = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root)
    for entry in index.decode("utf-8", "surrogateescape").split("\0"):
        if not entry:
            continue
        header, path = entry.split("\t", 1)
        mode, _, stage = header.split()
        if stage == "0" and mode in {"100644", "100755"} and not path.startswith(PACKAGE + "/"):
            yield path


def inventory(root: Path) -> list[Consumer]:
    commands = {}
    hits = []
    for path in tracked_files(root):
        target = root / path
        doc = target.suffix in DOC_SUFFIXES or target.name in DOC_NAMES
        if not (doc or target.suffix in SOURCE_SUFFIXES or target.name in CONFIG_NAMES
                or "Dockerfile" in target.name):
            continue
        if target.is_symlink():
            continue
        try:
            source = target.read_bytes()
        except FileNotFoundError:
            continue  # A tracked consumer can be removed before it leaves the index.
        if b"\x00" in source or not any(marker in source for marker in
                                       (b"python", b"pseudolife", b"pyproject.toml",
                                        b"requirements.lock.txt", b"pip", b"/app")):
            continue
        decoded = source.decode("utf-8-sig", "replace")
        if doc or target.suffix == ".rs":
            commands[path] = decoded
            continue
        try:
            hits.extend(scan(path, decoded))
        except SyntaxError as error:
            raise ValueError(f"Cannot scan {path}: {error.msg} at line {error.lineno}") from error
    scripts = frozenset(hit.path for hit in hits if hit.path.endswith(".py"))
    for path, source in commands.items():
        hits.extend(scan(path, source, scripts))
    return sorted(set(hits))


def load_rules(root: Path) -> list[Rule]:
    with (root / RULES).open("rb") as handle:
        entries = tomllib.load(handle)["rules"]
    rules = []
    for entry in entries:
        category, reason = entry["class"], entry["reason"]
        if category not in CATEGORIES or not reason.strip() or "\n" in reason:
            raise ValueError(f"Invalid classification or reason for {entry['globs']}")
        for glob in entry["globs"]:
            for kind in entry["kinds"]:
                if kind not in KINDS:
                    raise ValueError(f"Unknown consumer kind {kind!r} for {glob}")
                rules.append(Rule(glob, kind, category, reason))
    return rules


def classify(consumers: list[Consumer], rules: list[Rule]) -> tuple[list[Consumer], list[Rule]]:
    missing = []
    matched = set()
    for hit in consumers:
        rule = next((rule for rule in rules if rule.matches(hit)), None)
        if rule is None:
            missing.append(hit)
        else:
            matched.add(rule)
    return missing, [rule for rule in rules if rule not in matched]


def report(consumers: list[Consumer], rules: list[Rule]) -> int:
    missing, unused = classify(consumers, rules)
    for hit in consumers:
        rule = next((rule for rule in rules if rule.matches(hit)), None)
        label = f"{rule.category} — {rule.reason}" if rule else "UNCLASSIFIED"
        print(f"{hit.location}: {label}")
    for rule in unused:
        print(f"Unused rule: {rule.glob} ({rule.kind})")
    return int(bool(missing))


if __name__ == "__main__":
    raise SystemExit(report(inventory(ROOT), load_rules(ROOT)))
