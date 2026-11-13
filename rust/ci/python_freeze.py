"""Refuse an ordinary pull request that changes the frozen Python package.

Python is frozen since 14 November 2026 (maintainer ruling): nothing under
``pseudolife_memory/`` changes again, and a defect found there is fixed in
Rust. The ``Python freeze`` workflow (python-freeze.yml) runs this on every pull request. It
decides on the paths of the pull request's own net change only: its merge
commit against the base parent, ``git diff --no-renames``, so a rename out
of the package counts as a deletion of its old path.

One prefix is exempt: ``pseudolife_memory/web/static/``, the tracked Console
build output the Rust daemon serves (ci.yml's ``frontend`` job rebuilds it),
under its exact spelling only. The package prefix itself is compared after
NFKC normalisation and case folding, because on a case-insensitive checkout
(Windows, a default macOS volume) ``Pseudolife_memory/x.py`` writes into the
real package. A diff that cannot be read fails closed.

This check is early feedback on the pull request; the gate is
``ops/integrate.py build``, which applies master's copy of this module to
what each member's merge changes on the batch branch, and ``open`` and
``merge``, which judge the whole batch again with fresh labels. An
integration batch (an exact ``integrate/`` head branch from this repository)
is exempt from this check for that reason, not because its members passed
it: a member's own run can be absent, queued or cancelled.

A maintainer-ruled exception (the expose security fix, the last release's
update bridge in ``update_cli.py``) passes through the exact label
``freeze-exception``, which only someone with triage access can apply. The
workflow reruns on labeled and unlabeled events, so removing the label
brings the refusal back. The decision reads only the diff's paths and that
label.

Standard library only: the job runs it with the runner's own Python.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unicodedata
import re
from pathlib import Path

FROZEN = "pseudolife_memory/"
EXEMPT = ("pseudolife_memory/web/static/",)
BATCH_PREFIX = "integrate/"
EXCEPTION_LABEL = "freeze-exception"


def _folded(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


# A Windows 8.3 short name for the package directory (PSEUDO~1), which a
# checkout with short names enabled writes into the real package.
SHORT_NAME = re.compile(r"pseudo~[0-9]+/")


def frozen_paths(paths: list[str]) -> list[str]:
    """The changed paths the freeze refuses, in order: anything whose folded
    form is under the package or its short name, unless it is under the
    exempt prefix exactly."""
    return [path for path in paths
            if (_folded(path).startswith(FROZEN) or SHORT_NAME.match(_folded(path)))
            and not path.startswith(EXEMPT)]


def changed_paths(root: Path, base: str, head: str) -> list[str] | None:
    """Every path ``head`` changes against ``base``, or None if git cannot say."""
    try:
        result = subprocess.run(["git", "diff", "--no-renames", "--name-only", "-z", base, head],
                                cwd=root, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [path for path in result.stdout.decode("utf-8", "surrogateescape").split("\0") if path]


def exempt(head_ref: str, head_repo: str, base_repo: str) -> bool:
    """An integration batch: an ordinal ``integrate/`` head from this repository."""
    return head_ref.startswith(BATCH_PREFIX) and bool(head_repo) and head_repo == base_repo


def labels_of(raw: str) -> list[str]:
    """The pull request's label names from their JSON list, or none if unreadable."""
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


def refusal(paths: list[str]) -> str:
    return ("Python is frozen since 14 November 2026: this pull request changes "
            + ", ".join(paths)
            + "; make the change in Rust instead (only pseudolife_memory/web/static/, the Console build, may change;"
            + f" a change the maintainer ruled an exception carries the label {EXCEPTION_LABEL}).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--head-repo", default="")
    parser.add_argument("--base-repo", default="")
    parser.add_argument("--base", default="HEAD^1", help="the base parent of the pull request's merge commit")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--labels", default="", help="the pull request's label names as a JSON list")
    options = parser.parse_args(argv)
    if exempt(options.head_ref, options.head_repo, options.base_repo):
        print(f"Python freeze: {options.head_ref} is an integration batch; its members passed this check")
        return 0
    paths = changed_paths(Path.cwd(), options.base, options.head)
    if paths is None:
        print(f"Python freeze: could not read the change ({options.base}..{options.head}); "
              "refusing rather than passing unchecked. Rerun the job; if it fails again, check the checkout depth.")
        return 1
    refused = frozen_paths(paths)
    if refused and EXCEPTION_LABEL in labels_of(options.labels):
        print(f"Python freeze: {', '.join(refused)} admitted by the {EXCEPTION_LABEL} label (a maintainer-ruled exception)")
        return 0
    if refused:
        print(f"::error title=Python is frozen::{refusal(refused)}")
        print(refusal(refused))
        return 1
    print(f"Python freeze: {len(paths)} changed paths, none under {FROZEN} outside {', '.join(EXEMPT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
