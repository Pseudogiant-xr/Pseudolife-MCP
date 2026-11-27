"""Mark a release-cut pull request so ci.yml runs its frozen-inclusive suite.

An ordinary pull request runs only the fast lanes; its integration batch
runs `test`. A release cut is always batched alone, and its batch is the
first run of the frozen-inclusive suite on the cut's head, so on 26 November
2026 the 0.26.0 cut learnt inside its batch, three hosted attempts in, that
the suite outgrew its time bound on a slow runner CPU. With this mark the
cut's own pull request runs `test` with the frozen files when it opens, and
on each later push, so the cut learns its time and failures before its batch
does. Only `test` reads the mark; the other lanes keep their fast-lane
decision.

A cut is what the parity oracle calls one
(evals/rust_port/oracle_selection.py ``release_cut``): only the release
version moves, in pyproject.toml and the named version files, beside
documentation. ci.yml's scope runs this on the pull request's merge commit
(``--base HEAD^1 --head HEAD``): its first parent is the base tip, so the
diff is the pull request's own net change, as everywhere else in scope.

    python3 -m ops.ci_release_cut --base HEAD^1 --head HEAD --github-output "$GITHUB_OUTPUT"

writes ``cut=true`` and ``include_frozen=true`` for a cut and nothing
otherwise. It needs only the standard library (scope has no setup-python).
It lives beside ops/ci_push_passed.py, not under rust/, because
rust/ci/parity_scope.py reads every rust/ file that imports evals.rust_port
as a parity harness, and this is a scope helper, not a harness.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.rust_port.oracle_selection import release_cut

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the base tip (the merge commit's first parent)")
    parser.add_argument("--head", required=True, help="the commit whose change is classified")
    parser.add_argument("--github-output", type=Path, help="append the outputs to this file")
    options = parser.parse_args(argv)
    cut = release_cut(ROOT, options.base, options.head)
    if cut is None:
        print("not a release cut")
        return 0
    print(f"a release cut ({cut['base_version']} -> {cut['version']}): "
          "`test` runs the frozen-inclusive suite on this head")
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as output:
            output.write("cut=true\ninclude_frozen=true\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
