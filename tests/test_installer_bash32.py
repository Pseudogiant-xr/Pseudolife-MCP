"""The installers' shell files stay runnable by bash 3.2, macOS's /bin/bash.

Pull requests run on Linux, whose bash is 5.x, and only an integration batch
reaches the macOS lane. So this file checks, on any host, the constructs bash
3.2 gets wrong, by a static scan:

- bash 3.2 finds the end of a command substitution ``$( ... )`` by counting
  parentheses, and it parses the body only when the substitution runs. An
  unparenthesised ``case`` pattern's ``)`` therefore ends the substitution
  early: ``bash -n`` passes, and the run fails or, inside double quotes,
  silently computes something else. A here-document inside ``$( )`` whose
  text holds an unbalanced quote or parenthesis is misread the same way.
- ``"${name[@]}"`` of an empty array is an unbound-variable error under
  ``set -u`` before bash 4.4.
- The syntax bash 4 added (``|&``, ``&>>``, ``;&``, ``mapfile``, ``declare
  -A`` and the rest of ``BASH4_ONLY``) is a parse error or an unknown command.

When a bash 3.2 is available (``PSEUDOLIFE_TEST_BASH32`` names one, or the
``bash`` on PATH is 3.2, as on macOS), each file also has to pass its
``bash -n``. That catches what 3.2 rejects at parse time; the command
substitution class above it does not catch, which is why the scan exists.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SHELL_FILES = sorted({*ROOT.glob("ops/install*.sh"), *ROOT.glob("ops/preflight*.sh")})

# Syntax and builtins bash 3.2 does not have; each a regex over code lines
# (comment lines dropped) and what it is.
BASH4_ONLY = [
    (r"(?:declare|local|typeset)\s+-[a-zA-Z]*A", "an associative array (declare -A)"),
    (r"(?:declare|local|typeset)\s+-[a-zA-Z]*n\b", "a name reference (declare -n)"),
    (r"(?:declare|local|typeset)\s+-[a-zA-Z]*g\b", "declare -g"),
    (r"\b(?:mapfile|readarray)\b", "mapfile / readarray"),
    (r"\bcoproc\b", "coproc"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?(?:,,?|\^\^?)\}", "a case-changing expansion (${x,,} / ${x^^})"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?@[QEPAa]\}", "a transformation expansion (${x@Q})"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*\[-[0-9]+\]\}", "a negative array subscript"),
    (r"&>>", "&>> (append both streams)"),
    (r"\|&", "|& (pipe both streams)"),
    (r";;&|(?<!;);&", "a case fall-through terminator (;& / ;;&)"),
    (r"\bwait\s+-n\b", "wait -n"),
    (r"%\([^)]*\)T", "printf %(...)T"),
    (r"\bexec\s+\{[A-Za-z_]", "exec {fd}> (an allocated descriptor)"),
    (r"\[\[\s+-v\b|\btest\s+-v\b|\[\s+-v\b", "-v (is the variable set)"),
    (r"\$\{?(?:BASHPID|EPOCHSECONDS|EPOCHREALTIME)\b", "a bash 4+ variable"),
    (r"\bshopt\s+-s\s+(?:globstar|lastpipe|autocd|direxpand)\b", "a bash 4+ shell option"),
]


def _word_start(text: str, i: int) -> bool:
    return i == 0 or text[i - 1] in " \t\n;&|()"


def _skip_single(text: str, i: int) -> int:
    """``text[i]`` is ``'``: the index after its closing quote."""
    end = text.find("'", i + 1)
    return len(text) if end < 0 else end + 1


def _skip_double(text: str, i: int, found: list) -> int:
    """``text[i]`` is ``"``: the index after its closing quote, recording any
    command substitution inside it."""
    k = i + 1
    while k < len(text):
        c = text[k]
        if c == "\\":
            k += 2
        elif c == '"':
            return k + 1
        elif text.startswith("$(", k) and not text.startswith("$((", k):
            k = _comsub(text, k, found)
        elif c == "`":
            end = text.find("`", k + 1)
            k = len(text) if end < 0 else end + 1
        else:
            k += 1
    return k


def _comsub(text: str, start: int, found: list) -> int:
    """``text[start:]`` opens ``$(``. Finds its end the way bash 3.2 does (the
    first unquoted ``)`` that balances, comments skipped, a here-document's
    text read as code), records ``(start, body)`` and every substitution nested
    in it, and returns the index after the ``)``."""
    depth, k = 1, start + 2
    while k < len(text):
        c = text[k]
        if c == "\\":
            k += 2
            continue
        if c == "'":
            k = _skip_single(text, k)
            continue
        if c == '"':
            k = _skip_double(text, k, found)
            continue
        if c == "`":
            end = text.find("`", k + 1)
            k = len(text) if end < 0 else end + 1
            continue
        if c == "#" and _word_start(text, k):
            end = text.find("\n", k)
            k = len(text) if end < 0 else end
            continue
        if text.startswith("$(", k) and not text.startswith("$((", k):
            k = _comsub(text, k, found)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                found.append((start, text[start + 2:k]))
                return k + 1
        k += 1
    found.append((start, text[start + 2:]))
    return k


_HEREDOC = re.compile(r"<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def command_substitutions(text: str) -> list[tuple[int, str]]:
    """Every ``$( ... )`` in a shell file as ``(offset, body)``, each body cut
    where bash 3.2 cuts it. Single-quoted text, comments and here-document
    text outside a substitution are skipped."""
    found: list[tuple[int, str]] = []
    pending: list[tuple[str, bool]] = []
    k = 0
    while k < len(text):
        c = text[k]
        if c == "\n" and pending:
            # Skip each pending here-document's text up to its delimiter line.
            k += 1
            for delimiter, strip_tabs in pending:
                while k < len(text):
                    end = text.find("\n", k)
                    line = text[k:len(text) if end < 0 else end]
                    k = len(text) if end < 0 else end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
            pending = []
            continue
        if c == "\\":
            k += 2
        elif c == "'":
            k = _skip_single(text, k)
        elif c == '"':
            k = _skip_double(text, k, found)
        elif c == "`":
            end = text.find("`", k + 1)
            k = len(text) if end < 0 else end + 1
        elif c == "#" and _word_start(text, k):
            end = text.find("\n", k)
            k = len(text) if end < 0 else end
        elif text.startswith("$(", k) and not text.startswith("$((", k):
            k = _comsub(text, k, found)
        elif text.startswith("<<", k) and not text.startswith("<<<", k):
            match = _HEREDOC.match(text, k)
            if match:
                pending.append((match.group(2), text.startswith("<<-", k)))
                k = match.end()
            else:
                k += 2
        else:
            k += 1
    return sorted(found)


_CASE = re.compile(r"(?:^|[\s;&|(])case\s")
_ESAC = re.compile(r"(?:^|[\s;&|])esac\b")


def bash32_problems(text: str) -> list[str]:
    """What in a shell file bash 3.2 would misread or refuse, one line each."""
    problems = []
    for start, body in command_substitutions(text):
        line = text.count("\n", 0, start) + 1
        for case in _CASE.finditer(body):
            if not _ESAC.search(body, case.end()):
                problems.append(
                    f"line {line}: a case inside $( ) whose pattern has no leading '(': bash 3.2 ends "
                    "the substitution at the pattern's ')'; write each pattern as (pattern), or move "
                    "the case into a function")
                break
        for doc in _HEREDOC.finditer(body):
            # The document's text, read from the file (bash 3.2's cut may fall
            # inside it), must leave bash 3.2's scan where it began: no quote
            # left open, no parenthesis unbalanced.
            strip_tabs = body.startswith("<<-", doc.start())
            newline = text.find("\n", start + 2 + doc.end())
            content = []
            for doc_line in ([] if newline < 0 else text[newline + 1:].split("\n")):
                if (doc_line.lstrip("\t") if strip_tabs else doc_line) == doc.group(2):
                    break
                content.append(doc_line)
            seen: list = []
            _comsub("$(" + "\n".join(content) + "\n)", 0, seen)
            if seen[-1][1] != "\n".join(content) + "\n":
                problems.append(
                    f"line {line}: a here-document inside $( ) holds an unbalanced quote or parenthesis, "
                    "which bash 3.2 reads as code; build the text outside the substitution")
    code_lines = [(n, raw) for n, raw in enumerate(text.splitlines(), 1)
                  if not raw.lstrip().startswith("#")]
    for pattern, what in BASH4_ONLY:
        for n, raw in code_lines:
            if re.search(pattern, raw):
                problems.append(f"line {n}: {what} is bash 4 or later")
    if re.search(r"(?m)^\s*set\s+-[a-z]*u", text):
        empty = set(re.findall(r"(?m)(?:^|[\s;])(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)=\(\)", text))
        for name in sorted(empty):
            for match in re.finditer(r"\$\{" + name + r"\[[@*]\]\}", text):
                guard = "${" + name + "[@]+"
                before = text[max(0, match.start() - len(guard) - 1):match.start()]
                if guard not in before:
                    n = text.count("\n", 0, match.start()) + 1
                    problems.append(
                        f"line {n}: ${{{name}[@]}} of an array that can be empty is an unbound "
                        f"variable under set -u before bash 4.4; write ${{{name}[@]+\"${{{name}[@]}}\"}}")
    return problems


def test_the_installer_shell_files_are_found():
    names = {path.name for path in INSTALLER_SHELL_FILES}
    assert {"install.sh", "install-native.sh", "install-hook.sh", "preflight.sh"} <= names


@pytest.mark.parametrize("path", INSTALLER_SHELL_FILES, ids=lambda p: p.name)
def test_installer_shell_file_has_no_bash4_construct(path):
    assert bash32_problems(path.read_text(encoding="utf-8")) == []


# The checker against the constructs it decides on, and their near misses.
@pytest.mark.parametrize("snippet", [
    'x="$(printf a | while read -r l; do\n    case "$l" in\n        a|b) echo hit ;;\n    esac\ndone)"\n',
    "x=$(case a in a) echo hit;; esac)\n",
    'x="$(f "$(case a in a) echo y;; esac)")"\n',
    "x=$(cat <<E\na ) b\nE\n)\n",
    "x=$(cat <<'E'\nit's\nE\n)\n",
    "set -u\nargs=()\ncmd \"${args[@]}\"\n",
    "echo a |& cat\n",
    "echo a &>> log\n",
    "declare -A seen\n",
    "mapfile -t lines < f\n",
    'echo "${name,,}"\n',
    "case $x in a) echo ;& b) echo ;; esac\n",
    # Each still found next to the harmless text of the near misses below.
    'x="$(echo "just in case"; case a in a) echo y;; esac)"\n',
    "x=$(echo 'just in case'; case a in a) echo y;; esac)\n",
    "x=$(\n  # in case the lookup fails\n  case a in a) echo y;; esac)\n",
    # The cut falls after a quoted "esac", which is no esac at all.
    'x=$(case a in "esac") echo y;; esac)\n',
    'echo "use mapfile"; mapfile -t lines < f\n',
    "mapfile -t lines < f  # mapfile\n",
    "printf '%(%s)T' -1\n",
    "printf '%s;%(%s)T' -1\n",
    "printf \\\n  '%(%s)T' -1\n",
    'x="$(echo "${msg:-in case}"; case a in a) echo y;; esac)"\n',
    "echo ${name^^}\n",
    "set -u\n# args=(a)\nargs=()\ncmd \"${args[@]}\"\n",
])
def test_the_check_refuses_what_bash32_misreads(snippet):
    assert bash32_problems(snippet), snippet


@pytest.mark.parametrize("snippet", [
    'case "$(uname -s)" in Linux) echo l ;; esac\n',
    'x="$(printf a | while read -r l; do\n    case "$l" in\n        (a|b) echo hit ;;\n    esac\ndone)"\n',
    "x=$(case a in (a) echo hit;; esac)\n",
    "f() { case $1 in a) echo hit ;; esac; }\nx=$(f a)\n",
    "x=$(\n  # a ) comment, and it's fine\n  echo ok)\n",
    "x=$(cat <<E\nplain text\nE\n)\n",
    'x="$(cat <<E\n# a \\`quoted\\` (balanced) line\nkey: ["disabled"]\nE\n)"\n',
    "cat <<EOF\nit's (fine) here: $(echo no case)\nEOF\n",
    "echo 'a $(case x in' \n",
    "y=$((1 + (2 * 3)))\n",
    "set -u\nargs=()\ncmd ${args[@]+\"${args[@]}\"}\necho ${#args[@]}\n",
    "args=()\ncmd \"${args[@]}\"\n",
    'echo "${x%,}" "${x#^}"\n',
    "# declare -A only in a comment\n",
    # Only code decides: the same words in quoted text or a comment are text.
    'x="$(echo "just in case it fails")"\n',
    "x=$(echo 'just in case it fails')\n",
    "x=$(\n  # in case the lookup fails\n  echo ok)\n",
    "x=$(cat <<'E'\njust in case\nE\n)\n",
    'echo "use mapfile"\n',
    "echo 'a |& b' \"c &>> d\"\n",
    "echo ok  # mapfile and declare -A would be bash 4\n",
    'echo "%(%s)T"\n',
    "printf a; echo '%(%s)T'\n",
    'x="$(echo "${msg:-just in case it fails}")"\n',
    "x=$(echo ${msg:-in case of rain})\n",
    'echo "${1:-use mapfile}"\n',
    "cat <<'E'\nmapfile it's\nE\n",
    "set -u\n# args=()\nargs=(a)\ncmd \"${args[@]}\"\n",
])
def test_the_check_passes_what_bash32_reads_correctly(snippet):
    assert bash32_problems(snippet) == [], snippet


def _bash32() -> str | None:
    named = os.environ.get("PSEUDOLIFE_TEST_BASH32")
    candidates = [named] if named else [shutil.which("bash")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            version = subprocess.run([candidate, "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
                                     capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if version == "3.2":
            return candidate
        if named:
            pytest.fail(f"PSEUDOLIFE_TEST_BASH32={named} is bash {version or 'unknown'}, not 3.2")
    return None


@pytest.mark.parametrize("path", INSTALLER_SHELL_FILES, ids=lambda p: p.name)
def test_installer_shell_file_parses_under_bash32(path):
    bash32 = _bash32()
    if bash32 is None:
        pytest.skip("no bash 3.2 here (set PSEUDOLIFE_TEST_BASH32 to one)")
    result = subprocess.run([bash32, "-n", str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
