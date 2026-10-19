"""Guard: no new sub-second wall-clock budget may decide a test verdict.

A test that waits a fixed real-time interval and then checks that something
happened (or that it finished within the interval) passes on a quiet machine
and fails on a loaded CI runner. Hosted Windows runners stall for hundreds of
milliseconds routinely, so every sub-second budget of this kind eventually
flakes, and widening it only moves the threshold. The durable fixes gate on
an event or an ordering (a readiness signal, a gate the fixture holds, the
order two things were recorded in), or run Rust timers on tokio's paused
clock.

This guard reads source only. It scans ``tests/``, every Rust ``tests``
directory and ``*_tests.rs`` / ``tests.rs`` module under ``rust/``, and the
parity harnesses (``rust/cli_harness/``, ``rust/daemon/harness/``) for:

* a sleep shorter than one second outside a loop (a sleep inside a
  ``while``, a ``loop`` or a counted ``for`` is a poll, and a poll only
  delays the verdict; a ``for`` over a literal list of cases is not a loop
  here);
* a ``timeout`` shorter than one second that the test waits on, outside a
  loop (Python ``timeout=<1`` keywords; Rust ``timeout(..)``,
  ``recv_timeout(..)`` and ``wait_timeout(..)`` with a sub-second
  ``Duration`` whose result is not checked as an error);
* a deadline ``Instant::now() + <sub-second Duration>`` outside a loop;
* an assertion comparing an elapsed time with a sub-second bound.

A hit that is a negative check (it asserts nothing happened in the interval,
which a slower machine only makes easier to pass), a minimum (the interval
only has to be at least that long), a product timing input whose verdict does
not depend on the machine's speed, or a poll the scanner cannot see, goes into
``tests/wall_clock_budget_allowlist.txt`` with a one-line reason, under the
test function that holds it. A budget a slow machine can fail is converted,
not allowlisted; the few that need a product hook first are marked
``debt:``.
"""

from __future__ import annotations

import ast
import functools
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "tests" / "wall_clock_budget_allowlist.txt"
LIMIT_SECONDS = 1.0


@dataclass(frozen=True)
class Hit:
    path: str  # repository-relative, forward slashes
    function: str  # the enclosing test or helper function
    line: int
    kind: str
    text: str  # the stripped source line

    @property
    def key(self) -> tuple[str, str, str]:
        # File, function and source text, not the line number, so an edit
        # elsewhere in the file does not invalidate an entry, while a copy of
        # an allowlisted line in another function is still caught.
        return (self.path, self.function, self.text)


# --------------------------------------------------------------------------
# File selection
# --------------------------------------------------------------------------


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def python_sources() -> list[Path]:
    roots = [ROOT / "tests", ROOT / "rust" / "cli_harness", ROOT / "rust" / "daemon" / "harness"]
    found: set[Path] = set()
    for base in roots:
        found.update(p for p in base.rglob("*.py") if "goldens" not in p.parts)
    return sorted(found)


def _is_rust_test_file(path: Path) -> bool:
    parts = path.relative_to(ROOT / "rust").parts
    if "target" in parts:
        return False
    if "tests" in parts[:-1] or path.name == "tests.rs" or path.name.endswith("_tests.rs"):
        return True
    return parts[0] == "cli_harness" or parts[:2] == ("daemon", "harness")


def rust_sources() -> list[Path]:
    return sorted(p for p in (ROOT / "rust").rglob("*.rs") if _is_rust_test_file(p))


# --------------------------------------------------------------------------
# Python scanner (AST)
# --------------------------------------------------------------------------


def _number(node: ast.AST | None) -> float | None:
    if (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)):
        return float(node.value)
    return None


def _sub_second(value: float | None) -> bool:
    return value is not None and 0 < value < LIMIT_SECONDS


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


_ELAPSED = re.compile(r"elapsed|duration|timing|took|spent|latency", re.IGNORECASE)


def _mentions_elapsed(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and _ELAPSED.search(sub.id):
            return True
        if isinstance(sub, ast.Attribute) and _ELAPSED.search(sub.attr):
            return True
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Sub):
            for side in (sub.left, sub.right):
                if isinstance(side, ast.Call) and _call_name(side) in {
                        "monotonic", "perf_counter", "time"}:
                    return True
    return False


class _PythonScanner(ast.NodeVisitor):
    def __init__(self, path: str, lines: list[str]):
        self.path = path
        self.lines = lines
        self.loops = 0
        self.functions: list[str] = []
        self.hits: list[Hit] = []

    def _hit(self, node: ast.AST, kind: str) -> None:
        line = node.lineno
        function = ".".join(self.functions) or "<module>"
        self.hits.append(Hit(self.path, function, line, kind, self.lines[line - 1].strip()))

    def _loop(self, node: ast.AST) -> None:
        # ``for mode in ("a", "b"):`` walks test cases, it does not poll.
        cases = isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(
            node.iter, (ast.Tuple, ast.List, ast.Set))
        step = 0 if cases else 1
        self.loops += step
        self.generic_visit(node)
        self.loops -= step

    visit_While = _loop
    visit_For = _loop
    visit_AsyncFor = _loop

    def _function(self, node: ast.AST) -> None:
        # A nested function body starts a fresh loop context: a helper
        # defined inside a loop is not itself a poll.
        saved, self.loops = self.loops, 0
        self.functions.append(getattr(node, "name", "<lambda>"))
        self.generic_visit(node)
        self.functions.pop()
        self.loops = saved

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function
    visit_Lambda = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if not self.loops:
            if _call_name(node) == "sleep" and node.args and _sub_second(_number(node.args[0])):
                self._hit(node, "sleep")
            if any(keyword.arg == "timeout" and _sub_second(_number(keyword.value))
                   for keyword in node.keywords):
                self._hit(node, "timeout")
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        test = node.test
        if isinstance(test, ast.Compare) and _mentions_elapsed(test.left) and any(
                isinstance(op, (ast.Lt, ast.LtE)) and _sub_second(_number(right))
                for op, right in zip(test.ops, test.comparators)):
            self._hit(node, "elapsed-assert")
        self.generic_visit(node)


def scan_python_text(path: str, text: str) -> list[Hit]:
    scanner = _PythonScanner(path, text.splitlines())
    with warnings.catch_warnings():
        # Other files' invalid escape sequences are not this guard's finding.
        warnings.simplefilter("ignore")
        tree = ast.parse(text, filename=path)
    scanner.visit(tree)
    return scanner.hits


# --------------------------------------------------------------------------
# Rust scanner (rustfmt-shaped text)
# --------------------------------------------------------------------------

_MILLIS = re.compile(r"Duration::from_millis\(\s*([0-9_]+)\s*\)")
_SECS_F = re.compile(r"Duration::from_secs_f(?:32|64)\(\s*(0?\.[0-9]+)\s*\)")
_LOOP_HEADER = re.compile(
    r"^\s*(?:'?[A-Za-z_]+:\s*)?(?:loop\b|while\b|for\b)|=>\s*loop\b|\bloop\s*\{\s*$")
_CASE_LOOP = re.compile(r"^\s*for\b.*\bin\s+(?:\[|&\[|\()")
_ONE_LINE_POLL = re.compile(r"\b(?:while|loop)\b[^;]*\{")
_FN_HEADER = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)")
_ERROR_CHECK = re.compile(r"\.is_err\(\)|\bErr\(_\)\s*=>|\.expect_err\(|\.unwrap_err\(\)")


def _sub_second_literal(line: str) -> bool:
    for match in _MILLIS.finditer(line):
        if 0 < int(match.group(1).replace("_", "")) < 1000:
            return True
    return any(_sub_second(float(match.group(1))) for match in _SECS_F.finditer(line))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _enclosing(lines: list[str], index: int) -> tuple[bool, str]:
    """(inside a loop body, name of the enclosing fn) for line ``index``."""
    in_loop = bool(_ONE_LINE_POLL.search(lines[index]))
    level = _indent(lines[index])
    for back in range(index - 1, -1, -1):
        line = lines[back]
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped == "{":
            continue  # a lone brace belongs to the header wrapped above it
        indent = _indent(line)
        if indent >= level:
            continue
        level = indent
        function = _FN_HEADER.search(line)
        if function:
            return in_loop, function.group(1)
        if not _CASE_LOOP.search(line) and _LOOP_HEADER.search(line):
            in_loop = True
    return in_loop, "<module>"


def _statement(lines: list[str], index: int) -> str:
    """Line ``index`` with the lines of its statement around it: back to the
    previous line ending a statement or block, forward to its ``;``."""
    start = index
    while start > 0 and index - start < 6:
        previous = lines[start - 1].rstrip()
        if not previous or previous.endswith((";", "{", "}")):
            break
        start -= 1
    end = index
    while (end < len(lines) - 1 and end - index < 6
           and not lines[end].rstrip().endswith((";", "{", "}"))):
        end += 1
    return " ".join(line.strip() for line in lines[start:end + 1])


def _rust_kind(lines: list[str], index: int, in_loop: bool) -> str | None:
    if in_loop:
        return None
    line = lines[index]
    statement = _statement(lines, index)
    if re.search(r"Instant::now\(\)\s*\+\s*(?:std::time::)?Duration::from_(?:millis|secs_f)", statement):
        return "deadline"
    if "elapsed()" in statement and re.search(
            r"<=?\s*(?:std::time::)?Duration::from_(?:millis|secs_f)", statement):
        return "elapsed-assert"
    if re.search(r"\b(?:recv_|wait_)?timeout\s*\(", statement) and not re.search(
            r"set_(?:read|write)_timeout", line):
        return None if _ERROR_CHECK.search(statement) else "timeout"
    if re.search(r"\bsleep\s*\(", line):
        return "sleep"
    return None


def scan_rust_text(path: str, text: str) -> list[Hit]:
    lines = text.splitlines()
    hits: list[Hit] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("//") or not _sub_second_literal(line):
            continue
        in_loop, function = _enclosing(lines, index)
        kind = _rust_kind(lines, index, in_loop)
        if kind:
            hits.append(Hit(path, function, index + 1, kind, stripped))
    return hits


# --------------------------------------------------------------------------
# The tree and the allowlist
# --------------------------------------------------------------------------


@functools.cache
def all_hits() -> tuple[Hit, ...]:
    hits: list[Hit] = []
    for path in python_sources():
        hits.extend(scan_python_text(_rel(path), path.read_text(encoding="utf-8")))
    for path in rust_sources():
        hits.extend(scan_rust_text(_rel(path), path.read_text(encoding="utf-8")))
    return tuple(hits)


def load_allowlist(path: Path = ALLOWLIST) -> tuple[Counter, list[str]]:
    """Entries ``<path> | <function> | <source line> | <reason>``, one per
    flagged line (repeat an entry for each identical line in a function)."""
    entries: Counter = Counter()
    problems = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = [part.strip() for part in raw.split(" | ")]
        if len(parts) != 4 or not all(parts):
            problems.append(f"{path.name}:{number}: expected "
                            f"'<path> | <function> | <source line> | <reason>', got {raw!r}")
            continue
        entries[tuple(parts[:3])] += 1
    return entries, problems


def _describe(key: tuple[str, str, str], hits: tuple[Hit, ...]) -> str:
    lines = sorted(h.line for h in hits if h.key == key)
    where = ",".join(map(str, lines)) or "?"
    return f"  {key[0]}:{where} in {key[1]}: {key[2]}"


def test_no_sub_second_wall_clock_budget_decides_a_verdict():
    hits = all_hits()
    found = Counter(hit.key for hit in hits)
    allowed, problems = load_allowlist()
    assert not problems, "\n".join(problems)
    unlisted = [key for key in found if found[key] > allowed[key]]
    assert not unlisted, (
        "A test waits a fixed sub-second real-time interval, which a loaded CI "
        "runner can overrun. Wait on the event or ordering the test cares about "
        "instead (a gate the fixture holds, a readiness signal, a recorded "
        "order, tokio::time::pause for tokio timers), or, if a slower machine "
        "cannot fail it (a negative check, a minimum, a product input), add the "
        "line to tests/wall_clock_budget_allowlist.txt with its reason:\n"
        + "\n".join(_describe(key, hits) for key in unlisted))


def test_allowlist_has_no_stale_entries():
    found = Counter(hit.key for hit in all_hits())
    allowed, _ = load_allowlist()
    stale = [key for key in allowed if allowed[key] > found[key]]
    assert not stale, (
        "These allowlist entries no longer match a flagged line (converted, "
        "moved to another function, or edited); remove or update them:\n"
        + "\n".join(f"  {key[0]} in {key[1]}: {key[2]}" for key in stale))


# --------------------------------------------------------------------------
# The scanner itself, on the shapes it must and must not flag
# --------------------------------------------------------------------------

PY_FLAGGED = [
    ("sleep then check", "def t():\n    time.sleep(0.2)\n    assert done\n", "sleep"),
    ("async sleep", "async def t():\n    await asyncio.sleep(0.05)\n", "sleep"),
    ("timeout keyword", "def t():\n    thread.join(timeout=0.5)\n", "timeout"),
    ("elapsed bound", "def t():\n    assert time.monotonic() - t0 < 0.5\n", "elapsed-assert"),
    ("named elapsed", "def t():\n    assert elapsed <= 0.3\n", "elapsed-assert"),
    ("case loop is not a poll",
     "def t():\n    for mode in ('a', 'b'):\n        time.sleep(0.1)\n", "sleep"),
]
PY_CLEAN = [
    ("poll in while", "def t():\n    while not ready():\n        time.sleep(0.02)\n"),
    ("poll in counted for", "def t():\n    for _ in range(50):\n        time.sleep(0.02)\n"),
    ("one second or more", "def t():\n    time.sleep(1.0)\n    q.get(timeout=2)\n"),
    ("yield", "async def t():\n    await asyncio.sleep(0)\n"),
    ("variable budget", "def t():\n    time.sleep(delay)\n"),
    ("generous elapsed bound", "def t():\n    assert time.monotonic() - t0 < 2.0\n"),
]

RS_FLAGGED = [
    ("sleep then check",
     "async fn t() {\n    tokio::time::sleep(Duration::from_millis(90)).await;\n    assert!(x);\n}\n",
     "sleep"),
    ("bounded wait unwrapped",
     "async fn t() {\n    tokio::time::timeout(Duration::from_millis(100), board.close())\n"
     "        .await\n        .unwrap();\n}\n", "timeout"),
    ("elapsed bound",
     "fn t() {\n    assert!(start.elapsed() < Duration::from_millis(120));\n}\n", "elapsed-assert"),
    ("deadline", "fn t() {\n    let d = Instant::now() + Duration::from_millis(100);\n}\n", "deadline"),
    ("case loop is not a poll",
     "fn t() {\n    for mode in [\"a\", \"b\"] {\n"
     "        std::thread::sleep(Duration::from_millis(50));\n    }\n}\n", "sleep"),
]
RS_CLEAN = [
    ("poll in loop",
     "async fn t() {\n    loop {\n        if ready() {\n            break;\n        }\n"
     "        tokio::time::sleep(Duration::from_millis(5)).await;\n    }\n}\n"),
    ("poll in wrapped while",
     "fn t() {\n    while read_stat()\n        .is_none()\n    {\n"
     "        std::thread::sleep(Duration::from_millis(1));\n    }\n}\n"),
    ("one-line poll",
     "fn t() {\n    while !done() { thread::sleep(Duration::from_millis(2)); }\n}\n"),
    ("negative wait",
     "async fn t() {\n    assert!(tokio::time::timeout(Duration::from_millis(50), rx.recv())\n"
     "        .await\n        .is_err());\n}\n"),
    ("product knob", "fn t() {\n    options.timing.probe = Duration::from_millis(40);\n}\n"),
    ("socket option", "fn t() {\n    s.set_read_timeout(Some(Duration::from_millis(200))).unwrap();\n}\n"),
    ("one second or more",
     "async fn t() {\n    tokio::time::timeout(Duration::from_secs(10), close()).await.unwrap();\n}\n"),
    ("commented out", "fn t() {\n    // tokio::time::sleep(Duration::from_millis(90)).await;\n}\n"),
]


@pytest.mark.parametrize("name,source,kind", PY_FLAGGED, ids=[c[0] for c in PY_FLAGGED])
def test_python_scanner_flags(name, source, kind):
    hits = scan_python_text("x.py", source)
    assert [hit.kind for hit in hits] == [kind]
    assert hits[0].function == "t"


@pytest.mark.parametrize("name,source", PY_CLEAN, ids=[c[0] for c in PY_CLEAN])
def test_python_scanner_passes(name, source):
    assert scan_python_text("x.py", source) == []


@pytest.mark.parametrize("name,source,kind", RS_FLAGGED, ids=[c[0] for c in RS_FLAGGED])
def test_rust_scanner_flags(name, source, kind):
    hits = scan_rust_text("x.rs", source)
    assert [hit.kind for hit in hits] == [kind]
    assert hits[0].function == "t"


@pytest.mark.parametrize("name,source", RS_CLEAN, ids=[c[0] for c in RS_CLEAN])
def test_rust_scanner_passes(name, source):
    assert scan_rust_text("x.rs", source) == []


def test_an_allowlisted_line_copied_into_another_function_is_still_flagged(tmp_path):
    source = ("def first():\n    time.sleep(0.05)\n\n"
              "def second():\n    time.sleep(0.05)\n")
    allow = tmp_path / "allow.txt"
    allow.write_text("x.py | first | time.sleep(0.05) | negative check\n", encoding="utf-8")
    allowed, problems = load_allowlist(allow)
    assert not problems
    found = Counter(hit.key for hit in scan_python_text("x.py", source))
    assert [key for key in found if found[key] > allowed[key]] == [
        ("x.py", "second", "time.sleep(0.05)")]


def test_an_entry_without_a_reason_is_refused(tmp_path):
    allow = tmp_path / "allow.txt"
    allow.write_text("x.py | first | time.sleep(0.05)\n", encoding="utf-8")
    _, problems = load_allowlist(allow)
    assert problems and "expected" in problems[0]
