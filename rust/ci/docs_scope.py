"""Classify documentation changes for the hosted CI scope jobs."""
from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from ops.ci_batch_passed import BATCH_PREFIX, MASTER, ApiError, github_get

from .parity_scope import is_docs

ROOT = Path(__file__).resolve().parents[2]
CENSUS = "rust/producer-census.json"
SHA = re.compile(r"[0-9a-f]{40}")


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


def push_scope(env: Mapping[str, str]) -> str:
    """Return 'docs', 'other' for a classified push, or 'unknown'."""
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
        if regular and documentation_only(paths) and is_direct_push(env, after):
            return "docs"
        return "other"
    except (ApiError, OSError, subprocess.SubprocessError, ValueError, KeyError, AttributeError) as error:
        print(f"Docs-only push scope unavailable: {error}", file=sys.stderr)
        return "unknown"


def report_census(env: Mapping[str, str]) -> None:
    # --ci-check reports drift for a push that changes only docs. Its
    # currency gate still applies to batch PRs and census/inventory changes.
    result = subprocess.check_output([sys.executable, str(ROOT / "rust/producer_census.py"), "--ci-check"],
                                     cwd=ROOT, env=env, timeout=60, encoding="utf-8")
    print(result, end="")
    with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(f"### Producer census\n\n{result}\n")


def rust_scope(env: Mapping[str, str]) -> tuple[bool, bool]:
    """Return whether to run full lanes and whether to run Rust lanes."""
    event = env.get("EVENT_NAME")
    try:
        if event == "push" and push_scope(env) == "docs":
            report_census(env)
            return True, False
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
    parser.add_argument("workflow", choices=("ci", "rust"))
    args = parser.parse_args()
    if args.workflow == "ci":
        print(push_scope(os.environ))
    else:
        full, rust = rust_scope(os.environ)
        result = f"full={str(full).lower()}\nrust={str(rust).lower()}\n"
        print(result, end="")
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(result)


if __name__ == "__main__":
    main()
