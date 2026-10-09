"""Fold changelog fragments into CHANGELOG.md at a release cut.

Each pull request adds one `changelog.d/<YYYY-MM-DD>-<slug>.md` file holding
its `### <Kind> (YYYY-MM-DD — title)` entry, instead of editing CHANGELOG.md,
so parallel pull requests no longer conflict on the same lines. At the
version cut, run:

    python ops/assemble_changelog.py

It inserts every fragment at the top of `## [Unreleased]`, newest first (by
file name, which starts with the date), then deletes the fragment files.
`changelog_text()` returns CHANGELOG.md plus any pending fragments, for the
tests that look up CHANGELOG claims before a release has folded them in.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNRELEASED = "## [Unreleased]\n"


def fragments(root: Path = ROOT) -> list[Path]:
    """Pending fragments, newest first."""
    folder = root / "changelog.d"
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.glob("*.md") if p.name != "README.md"),
                  key=lambda p: p.name, reverse=True)


def changelog_text(root: Path = ROOT) -> str:
    """CHANGELOG.md followed by every pending fragment."""
    parts = [(root / "CHANGELOG.md").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in fragments(root)]
    return "\n".join(parts)


def assemble(root: Path = ROOT) -> int:
    """Fold pending fragments into `## [Unreleased]`; return how many."""
    pending = fragments(root)
    if not pending:
        return 0
    path = root / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    if UNRELEASED not in text:
        raise SystemExit("CHANGELOG.md has no '## [Unreleased]' heading")
    head, tail = text.split(UNRELEASED, 1)
    entries = "".join(p.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
                      for p in pending)
    path.write_text(head + UNRELEASED + "\n" + entries + tail.lstrip("\n"),
                    encoding="utf-8")
    for fragment in pending:
        fragment.unlink()
    return len(pending)


if __name__ == "__main__":
    count = assemble()
    print(f"folded {count} changelog fragment(s) into CHANGELOG.md")
    sys.exit(0)
