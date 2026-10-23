"""Bind new parity captures to immutable event commits, never a moving ref."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

from .oracle_pins import require_pinned_head

ROOT = Path(__file__).resolve().parents[2]
SELECTION_ENV = "PSEUDOLIFE_PORT_ORACLE_SELECTION"


VERSION_FILES = {
    "plugin/release.json", "server.json", "ops/docker-compose.yml",
    "pseudolife_memory/compose/docker-compose.yml",
    "rust/shim/Cargo.toml", "rust/daemon/Cargo.toml", "rust/Cargo.lock",
    "rust/shim/src/lifecycle.rs", "rust/shim/tests/auth_lifecycle_http.rs",
}
# The native crates a cut moves, by file, the first one required. The lock's
# daemon entry is optional: cuts made before the daemon crate carried the
# release version left it at 0.0.0, and they must still be recognised.
CARGO_PACKAGES = {
    "rust/shim/Cargo.toml": ("pseudolife-stdio",),
    "rust/daemon/Cargo.toml": ("pseudolife-daemon",),
    "rust/Cargo.lock": ("pseudolife-stdio", "pseudolife-daemon"),
}
DOCUMENTS = {
    "CHANGELOG.md", "CONTRIBUTING.md", "README.md", "SECURITY.md",
    "llms.txt", "llms-full.txt", "docs/atlas/atlas.json",
}


def tree(root, head):
    raw = subprocess.check_output(
        ["git", "ls-tree", "-r", "-z", head], cwd=root, timeout=10)
    entries = {}
    for record in raw.split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            entries[path.decode("utf-8")] = tuple(metadata.split())
    return entries


def blob(root, entry):
    return subprocess.check_output(
        ["git", "cat-file", "blob", entry[2].decode("ascii")], cwd=root, timeout=10)


def version_metadata_matches(path, before, after, previous, current):
    if path in ("rust/shim/src/lifecycle.rs", "rust/shim/tests/auth_lifecycle_http.rs"):
        return before.replace(previous.encode(), current.encode()) == after
    if path in CARGO_PACKAGES:
        old, new = tomllib.loads(before.decode("utf-8")), tomllib.loads(after.decode("utf-8"))
        packages = old["package"] if path.endswith(".lock") else [old["package"]]
        expected = before
        for index, name in enumerate(CARGO_PACKAGES[path]):
            members = [package for package in packages if package["name"] == name]
            if len(members) != 1:
                if not members and index:
                    continue
                return False
            if members[0]["version"] != previous:
                if index == 0:
                    return False
                continue
            members[0]["version"] = current
            # Replace only this package's canonical Cargo stanza, not
            # dependencies that happen to have the same version number.
            pattern = (rb'(?m)^(name = "' + re.escape(name.encode()) + rb'"\r?\nversion = ")'
                       + re.escape(previous.encode()) + rb'("\r?$)')
            expected = re.sub(pattern, lambda match: match[1] + current.encode() + match[2], expected, count=1)
        return old == new and expected == after
    if path.endswith(".json"):
        old, new = json.loads(before), json.loads(after)
        if old.get("version") == previous:
            old["version"] = current
        if path == "server.json":
            for package in old.get("packages", []):
                if package.get("version") == previous:
                    package["version"] = current
        return old == new and before.replace(previous.encode(), current.encode()) == after
    # Compose's only release field is the daemon image tag. An equal version
    # string in an environment value or address is not release metadata.
    pattern = rb"(?m)^([ \t]*image:[ \t]+pseudolife-daemon:)" + re.escape(previous.encode()) + rb"([ \t]*\r?$)"
    return re.sub(pattern, lambda match: match[1] + current.encode() + match[2], before) == after


def release_cut(root, base, head):
    """Return release evidence only when Python behavior and tests are unchanged.

    Only the named native version literals and package metadata may accompany
    a cut. Documentation may be folded, but unknown paths, executable docs,
    original tests, instruments and dependency changes retain the merge base.
    """
    before, after = tree(root, base), tree(root, head)
    metadata = "pyproject.toml"
    old_entry, new_entry = before.get(metadata), after.get(metadata)
    if old_entry == new_entry or not old_entry or not new_entry:
        return None
    if old_entry[:2] != (b"100644", b"blob") or new_entry[:2] != old_entry[:2]:
        return None
    old_bytes, new_bytes = blob(root, old_entry), blob(root, new_entry)
    old, new = tomllib.loads(old_bytes.decode("utf-8")), tomllib.loads(new_bytes.decode("utf-8"))
    old_version = old.get("project", {}).pop("version", None)
    new_version = new.get("project", {}).pop("version", None)
    if (old != new or old_version == new_version
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) is None
                   for value in (old_version, new_version))):
        return None
    previous, current = old_version.encode("ascii"), new_version.encode("ascii")
    if old_bytes.replace(previous, current) != new_bytes:
        return None
    for path in before.keys() | after.keys():
        left, right = before.get(path), after.get(path)
        if left == right or path == metadata:
            continue
        # Only ordinary documentation files may be added, removed or rewritten.
        if (path in DOCUMENTS
                or path.startswith(("docs/", "changelog.d/", "rust/")) and path.endswith(".md")):
            if all(entry is None or entry[:2] == (b"100644", b"blob") for entry in (left, right)):
                continue
        if (path in VERSION_FILES and left and right
                and left[:2] == right[:2] == (b"100644", b"blob")
                and version_metadata_matches(path, blob(root, left), blob(root, right), old_version, new_version)):
            continue
        return None
    return {"base_version": old_version, "version": new_version}


def commit(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None or value == "0" * 40:
        raise ValueError("oracle selection requires a full nonzero commit SHA")
    return value


def git(root, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=root, encoding="utf-8", timeout=10).strip()


def merge_base(root, base, head):
    bases = git(root, "merge-base", "--all", commit(base), commit(head)).splitlines()
    if len(bases) != 1:
        raise ValueError("oracle selection requires one actual merge base")
    return commit(bases[0])


def select(root, event_name, event, checkout_head, *, github_ref=None):
    checkout_head = commit(checkout_head)
    release = None
    if event_name == "pull_request":
        request = event["pull_request"]
        base, head = commit(request["base"]["sha"]), commit(request["head"]["sha"])
        oracle = merge_base(root, base, head)
        release = release_cut(root, oracle, head)
        if release is not None:
            oracle = head
    elif event_name == "merge_group":
        # A queued group is a pull request against its own base: the group's
        # base commit (master, or the queue entry ahead of it) is the oracle,
        # and the group head, which is the checkout, is the candidate.
        group = event["merge_group"]
        base, head = commit(group["base_sha"]), commit(group["head_sha"])
        if head != checkout_head:
            raise ValueError("merge group head and checkout head differ")
        oracle = merge_base(root, base, head)
        release = release_cut(root, oracle, head)
        if release is not None:
            oracle = head
    elif event_name == "push":
        head = oracle = commit(event["after"])
        base = None
        if head != checkout_head:
            raise ValueError("push after and checkout head differ")
    elif event_name == "schedule":
        if github_ref != "refs/heads/master":
            raise ValueError("schedule requires refs/heads/master")
        head = oracle = checkout_head
        base = None
    elif event_name == "workflow_dispatch":
        if github_ref != "refs/heads/master":
            raise ValueError("dispatch requires refs/heads/master")
        head = oracle = commit(event["inputs"]["frozen_head"])
        base = None
        if head != checkout_head:
            raise ValueError("dispatch frozen head and checkout head differ")
    else:
        raise ValueError("unsupported oracle selection event")
    # An object that is absent or is not a commit is a setup error, not parity.
    if git(root, "rev-parse", oracle + "^{commit}") != oracle:
        raise ValueError("selected oracle commit unavailable")
    return {"event": event_name, "github_ref": github_ref, "checkout_head": checkout_head,
            "base_head": base, "candidate_head": head, "oracle_head": oracle,
            **({"release_cut": release} if release is not None else {})}


def selected_oracle(root=ROOT):
    raw = os.environ.get(SELECTION_ENV)
    if not raw:
        raise RuntimeError("select the event oracle before preparing or capturing new parity")
    observed = json.loads(raw)
    name = observed["event"]
    event = ({"pull_request": {"base": {"sha": observed["base_head"]},
                               "head": {"sha": observed["candidate_head"]}}}
             if name == "pull_request"
             else {"merge_group": {"base_sha": observed["base_head"],
                                   "head_sha": observed["candidate_head"]}}
             if name == "merge_group" else {"after": observed["candidate_head"]}
             if name == "push" else {} if name == "schedule"
             else {"inputs": {"frozen_head": observed["candidate_head"]}})
    checked = select(root, name, event, observed["checkout_head"], github_ref=observed.get("github_ref"))
    if observed != checked:
        raise ValueError("oracle selection differs from its immutable event binding")
    return checked


def stale_pin_report(root, historical_head, paths):
    try:
        result = subprocess.run(["git", "diff", "--quiet", historical_head, "--", *paths],
                                cwd=root, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {"historical_pin": historical_head, "informational_only": True,
                "comparison_available": False, "source_matches_historical_pin": False}
    return {"historical_pin": historical_head, "informational_only": True,
            "comparison_available": result.returncode in (0, 1),
            "source_matches_historical_pin": result.returncode == 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME"))
    parser.add_argument("--event-path", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--checkout-head", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--github-ref", default=os.environ.get("GITHUB_REF"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    actual = git(args.root, "rev-parse", "HEAD")
    if actual != args.checkout_head:
        raise ValueError("event checkout head and actual checkout differ")
    selection = select(args.root, args.event_name,
                       json.loads(args.event_path.read_text(encoding="utf-8")), actual,
                       github_ref=args.github_ref)
    # PR captures deliberately use their merge base. Check the originating
    # production change too, before it can become a successor PR's oracle.
    for head in dict.fromkeys((selection["candidate_head"], actual)):
        require_pinned_head(args.root, head)
    encoded = json.dumps(selection, sort_keys=True, separators=(",", ":"))
    with args.out.open("x", encoding="utf-8") as output:
        output.write(encoded + "\n")
    if args.github_env is not None:
        with args.github_env.open("a", encoding="utf-8") as environment:
            environment.write(SELECTION_ENV + "=" + encoded + "\n")
    print(encoded, flush=True)


if __name__ == "__main__":
    main()
