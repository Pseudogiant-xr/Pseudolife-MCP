"""Classify documentation changes for the hosted CI scope jobs."""
from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable, Mapping
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from ops.ci_batch_passed import BATCH_PREFIX, MASTER, ApiError, github_get

from .parity_scope import is_docs, is_documentation_path

ROOT = Path(__file__).resolve().parents[2]
CENSUS = "rust/producer-census.json"
SHA = re.compile(r"[0-9a-f]{40}")
DOC_GUARDS = (
    "tests/test_release_ux.py", "tests/test_llms_txt.py", "tests/test_atlas_currency.py",
    "tests/test_eval_evidence.py", "tests/test_i18n_readme.py", "tests/test_claude_md_citations.py",
    "tests/test_extractor_model_lists.py",
)
# Some readers resolve directories or snapshots instead of naming the file.
DIRECTORY_READERS = {
    "changelog.d/": ("tests/test_changelog_fragments.py",),
    "docs/i18n/": ("tests/test_plugin_packaging.py",),
    "docs/guide/": ("tests/test_plugin_packaging.py",),
    "examples/": ("tests/test_plugin_packaging.py",),
}
READER_FILE = re.compile(r"(?:tests/(?:[a-zA-Z0-9_-]+/)*test_[a-zA-Z0-9_]+\.py|rust/test_producer_census\.py)")
TEST_TARGET = re.compile(READER_FILE.pattern + r"(?:::test_[A-Za-z0-9_]+)?")


def reader_targets(file: str, names: list[str]) -> set[str]:
    """Use individual tests for direct literals, or the file for shared readers."""
    source = ast.parse((ROOT / file).read_bytes(), filename=file)
    nodes = list(ast.walk(source))
    docstrings = {id(body[0].value) for node in nodes
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and (body := node.body) and isinstance(body[0], ast.Expr)
                  and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)}
    # A path overwritten by a fixture does not read the checkout document.
    writes = {id(part) for node in nodes if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr in ("write_text", "write_bytes")
              for part in ast.walk(node.func.value)}
    tests = [node for node in source.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name.startswith("test_")]
    targets = set()
    for node in nodes:
        if (not isinstance(node, ast.Constant) or id(node) in docstrings or id(node) in writes
                or not isinstance(node.value, (str, bytes))):
            continue
        if not any(name in str(node.value).casefold() for name in names):
            continue
        owner = next((test for test in tests
                      if min(item.lineno for item in (test, *test.decorator_list)) <= node.lineno <= test.end_lineno), None)
        if owner is None or any(isinstance(ref, ast.Name) and ref.id == owner.name for ref in nodes):
            return {file}
        targets.add(f"{file}::{owner.name}")
    return targets


def documentation_only(paths: Iterable[str], *, allow_generated_census: bool = False) -> bool:
    """True for a nonempty docs diff, optionally with the batch's census."""
    docs = False
    for path in paths:
        if is_docs(path):
            docs = True
        elif not (allow_generated_census and path == CENSUS):
            return False
    return docs


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, timeout=45).decode("utf-8")


def changed_paths(base: str, head: str = "HEAD") -> tuple[list[str], bool]:
    # Keep both sides of renames and inspect both tree modes. A type change
    # or deletion can otherwise hide a symlink or gitlink behind a docs path.
    raw = git("diff", "--raw", "-z", "--no-renames", base, head, "--")
    if not raw:
        return [], True
    if not raw.endswith("\0"):
        raise ValueError("incomplete raw Git diff")
    records = iter(raw[:-1].split("\0"))
    paths = []
    regular = True
    for metadata, path in zip(records, records, strict=True):
        if not metadata.startswith(":") or not path:
            raise ValueError("malformed raw Git diff record")
        old_mode, new_mode, _, _, _ = metadata[1:].split()
        regular &= {old_mode, new_mode} <= {"000000", "100644", "100755"}
        paths.append(path)
    return paths, regular


def is_direct_push(env: Mapping[str, str], after: str) -> bool:
    """Confirm that the tip is not an integration batch merge."""
    if not env.get("GITHUB_TOKEN"):
        raise ValueError("no token for the push's associated pull requests")
    get = github_get(env["GITHUB_TOKEN"], env["GITHUB_API_URL"])
    pulls = get(f"/repos/{env['GITHUB_REPOSITORY']}/commits/{after}/pulls")
    # Ambiguous or unrecognised replies cannot authorize the shortcut.
    if not isinstance(pulls, list) or len(pulls) > 1:
        raise ValueError("ambiguous associated pull requests")
    if not pulls:
        headers = git("cat-file", "commit", after).partition("\n\n")[0]
        if sum(line.startswith("parent ") for line in headers.splitlines()) > 1:
            # An empty association on a merge may also be a batch whose
            # pull request is not visible through the API yet.
            raise ValueError("merge has no associated pull request")
        return True
    pull = pulls[0]
    if (not isinstance(pull, dict) or not isinstance(pull.get("head"), dict)
            or not isinstance(pull.get("base"), dict) or pull["base"].get("ref") != "master"
            or pull.get("merge_commit_sha") != after
            or not isinstance(pull.get("merged_at"), str) or not pull["merged_at"]):
        raise ValueError("unreadable associated pull request")
    ref = pull["head"].get("ref")
    if not isinstance(ref, str) or not ref:
        raise ValueError("missing associated pull request head ref")
    # Leave even a failed or mismatched batch merge to the existing batch
    # logic. It independently checks the tree and job results.
    return not ref.startswith(BATCH_PREFIX)


def push_paths(env: Mapping[str, str]) -> tuple[str, list[str]]:
    """Read the exact regular documentation paths of a direct master push."""
    try:
        payload = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if (env.get("GITHUB_EVENT_NAME") != "push" or env.get("GITHUB_REF") != MASTER
                or payload.get("ref") != MASTER or payload.get("forced") is not False):
            raise ValueError("not an unforced master update")
        before, after = payload.get("before"), payload.get("after")
        if (not all(isinstance(sha, str) and SHA.fullmatch(sha) and sha.strip("0")
                    for sha in (before, after))
                or before == after or after != env.get("GITHUB_SHA") or after != git("rev-parse", "HEAD").strip()):
            raise ValueError("no exact before/after range at this checkout")
        try:
            git("cat-file", "-e", f"{before}^{{commit}}")
        except subprocess.CalledProcessError:
            # A multi-commit push can start beyond checkout's depth of two.
            # Fetch the event's exact tip, which may have moved again since.
            git("fetch", "--no-tags", "--depth=1", "origin", before)
        paths, regular = changed_paths(before, after)
        if not paths:
            raise ValueError("empty changed-file range")
        if regular and all(map(is_documentation_path, paths)) and is_direct_push(env, after):
            return "docs", paths
        return "other", []
    except (ApiError, OSError, subprocess.SubprocessError, ValueError, KeyError, AttributeError) as error:
        print(f"Docs-only push scope unavailable: {error}", file=sys.stderr)
        return "unknown", []


def doc_tests(paths: list[str]) -> list[str]:
    """Select the standing guards and tracked tests naming touched basenames."""
    names = sorted({PurePosixPath(path).name.casefold() for path in paths})
    scan = subprocess.run(
        ["git", "grep", "-l", "-z", "-i", "-F", "-f", "-", "--", ":(glob)tests/**/test_*.py"],
        cwd=ROOT, input=("\n".join(names) + "\n").encode("utf-8"), capture_output=True, timeout=45)
    if scan.returncode not in (0, 1) or (scan.returncode == 0 and not scan.stdout.endswith(b"\0")):
        raise ValueError("could not enumerate tests naming the changed documents")
    files = set(scan.stdout.decode("utf-8").rstrip("\0").split("\0")) if scan.stdout else set()
    if not all(READER_FILE.fullmatch(test) for test in files):
        raise ValueError("unrecognized reader test path")
    readers = set(DOC_GUARDS)
    for prefix, tests in DIRECTORY_READERS.items():
        if any(path.startswith(prefix) for path in paths):
            readers.update(tests)
    if CENSUS in paths or any(path.startswith(("docs/", "examples/")) for path in paths):
        readers.add("rust/test_producer_census.py")
    for file in sorted(files - readers):
        readers.update(reader_targets(file, names))
    return sorted(readers)


def docs_selection(env: Mapping[str, str]) -> tuple[str, list[str]]:
    """Only emit an affirmative shortcut with a complete test selection."""
    try:
        event = env.get("GITHUB_EVENT_NAME") or env.get("EVENT_NAME")
        if event == "push":
            decision, paths = push_paths(env)
            if decision != "docs":
                return decision, []
        elif event == "pull_request":
            sha = env.get("GITHUB_SHA")
            if not env.get("HEAD_REF") or not sha or sha != git("rev-parse", "HEAD").strip():
                raise ValueError("pull request checkout identity is unavailable")
            paths, regular = changed_paths("HEAD^1")
            docs = [path for path in paths if path != CENSUS]
            if not regular or not docs or not all(map(is_documentation_path, docs)):
                return "other", []
        else:
            return "other", []
        return "docs", doc_tests(paths)
    except (OSError, subprocess.SubprocessError, ValueError, SyntaxError) as error:
        print(f"Docs-only selection unavailable: {error}", file=sys.stderr)
        return "unknown", []


def push_scope(env: Mapping[str, str]) -> str:
    """Expose the push verdict without test arguments."""
    return docs_selection(env)[0]


def report_census(env: Mapping[str, str]) -> None:
    # --ci-check reports drift for a push that changes only docs. Its
    # currency gate still applies to batch PRs and census/inventory changes.
    result = subprocess.check_output([sys.executable, str(ROOT / "rust/producer_census.py"), "--ci-check"],
                                     cwd=ROOT, env=env, timeout=60, encoding="utf-8")
    print(result, end="")
    with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(f"### Producer census\n\n{result}\n")


def rust_scope(env: Mapping[str, str], *, docs: bool | None = None) -> tuple[bool, bool]:
    """Return whether to run full lanes and whether to run Rust lanes."""
    event = env.get("EVENT_NAME")
    try:
        if docs is None:
            docs = docs_selection(env)[0] == "docs"
        if docs:
            if event == "push":
                report_census(env)
                return True, False
            return bool(env.get("HEAD_REF", "").startswith(BATCH_PREFIX)), False
        if event == "pull_request":
            base = "HEAD^1"
        elif event == "merge_group" and env.get("MERGE_GROUP_BASE_REF") == MASTER:
            # Master's current tip includes every unmerged queue entry in
            # the diff. The event's base_sha can name an entry ahead of us.
            git("fetch", "--no-tags", "--depth=1", "origin", MASTER)
            base = "FETCH_HEAD"
        else:
            return True, True
        paths, regular = changed_paths(base)
        if paths:
            head_ref = env.get("HEAD_REF")
            ordinary = event == "pull_request" and head_ref and not head_ref.startswith(BATCH_PREFIX)
            return not ordinary, not (regular and documentation_only(paths, allow_generated_census=True))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        print(f"Rust scope unavailable: {error}", file=sys.stderr)
    return True, True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", choices=("ci", "rust", "tests"))
    args = parser.parse_args()
    if args.workflow == "tests":
        tests = json.loads(os.environ["DOC_TESTS"])
        if (not isinstance(tests, list) or not all(isinstance(path, str) and TEST_TARGET.fullmatch(path) for path in tests)
                or not set(DOC_GUARDS) <= set(tests)):
            parser.error("invalid docs test selection")
        raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", "-m", "not real_model", *tests],
                                         cwd=ROOT))
    decision, tests = docs_selection(os.environ)
    if args.workflow == "ci":
        result = f"decision={decision}\ndocs_tests={json.dumps(tests)}\n"
    else:
        full, rust = rust_scope(os.environ, docs=decision == "docs")
        optimized = decision == "docs" and not rust
        result = f"full={str(full).lower()}\nrust={str(rust).lower()}\ndocs={str(optimized).lower()}\n"
    print(result, end="")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(result)


if __name__ == "__main__":
    main()
