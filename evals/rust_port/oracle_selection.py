"""Bind new parity captures to immutable event commits, never a moving ref."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SELECTION_ENV = "PSEUDOLIFE_PORT_ORACLE_SELECTION"


def commit(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None or value == "0" * 40:
        raise ValueError("oracle selection requires a full nonzero commit SHA")
    return value


def git(root, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=root, text=True, timeout=10).strip()


def merge_base(root, base, head):
    bases = git(root, "merge-base", "--all", commit(base), commit(head)).splitlines()
    if len(bases) != 1:
        raise ValueError("oracle selection requires one actual merge base")
    return commit(bases[0])


def select(root, event_name, event, checkout_head):
    checkout_head = commit(checkout_head)
    if event_name == "pull_request":
        request = event["pull_request"]
        base, head = commit(request["base"]["sha"]), commit(request["head"]["sha"])
        oracle = merge_base(root, base, head)
    elif event_name == "push":
        head = oracle = commit(event["after"])
        base = None
        if head != checkout_head:
            raise ValueError("push after and checkout head differ")
    elif event_name == "workflow_dispatch":
        head = oracle = commit(event["inputs"]["frozen_head"])
        base = None
        if head != checkout_head:
            raise ValueError("dispatch frozen head and checkout head differ")
    else:
        raise ValueError("unsupported oracle selection event")
    # An object that is absent or is not a commit is a setup error, not parity.
    if git(root, "rev-parse", oracle + "^{commit}") != oracle:
        raise ValueError("selected oracle commit unavailable")
    return {"event": event_name, "checkout_head": checkout_head,
            "base_head": base, "candidate_head": head, "oracle_head": oracle}


def selected_oracle(root=ROOT):
    raw = os.environ.get(SELECTION_ENV)
    if not raw:
        raise RuntimeError("select the event oracle before preparing or capturing new parity")
    observed = json.loads(raw)
    name = observed["event"]
    event = ({"pull_request": {"base": {"sha": observed["base_head"]},
                               "head": {"sha": observed["candidate_head"]}}}
             if name == "pull_request" else {"after": observed["candidate_head"]}
             if name == "push" else {"inputs": {"frozen_head": observed["candidate_head"]}})
    checked = select(root, name, event, observed["checkout_head"])
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
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    actual = git(args.root, "rev-parse", "HEAD")
    if actual != args.checkout_head:
        raise ValueError("event checkout head and actual checkout differ")
    selection = select(args.root, args.event_name,
                       json.loads(args.event_path.read_text(encoding="utf-8")), actual)
    encoded = json.dumps(selection, sort_keys=True, separators=(",", ":"))
    with args.out.open("x", encoding="utf-8") as output:
        output.write(encoded + "\n")
    if args.github_env is not None:
        with args.github_env.open("a", encoding="utf-8") as environment:
            environment.write(SELECTION_ENV + "=" + encoded + "\n")
    print(encoded, flush=True)


if __name__ == "__main__":
    main()
