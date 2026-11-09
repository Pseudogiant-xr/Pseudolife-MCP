"""Every tracked file that cites a CLAUDE.md heading cites one that exists.

Workflows, CI helpers, ops scripts, docs and skills quote CLAUDE.md sections
by name (``CLAUDE.md "Running tests"``, ``CLAUDE.md#running-tests``). Nothing
checked those names until the 2026-11-09 reshape, when a council review found
``evals/lme_rejudge.py`` citing a heading that had never existed and the
2026-10-19 compaction had renamed sections without a grep. A cited name must
be a heading of CLAUDE.md, or the opening words of one, or of a bold rule
lead, so a rename breaks here and not silently in a workflow comment.

The word ceiling keeps the reshaped file from growing back: the council's
measured cost was about 10k tokens of instructions loaded into every session
and subagent (5,448 + 2,060 words before the reshape).
"""
from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = ROOT / "CLAUDE.md"
WORD_CEILING = 4_800

# Files whose mentions of CLAUDE.md are history or generated copies, not citations.
SKIP = ("CLAUDE.md", "CHANGELOG.md", "llms.txt", "llms-full.txt", "tests/test_claude_md_citations.py")
SKIP_DIRS = ("changelog.d/", "docs/atlas/")

# ``CLAUDE.md "X"``, ``CLAUDE.md ("X"``, ``CLAUDE.md, "X"``, ``CLAUDE.md's "X"``,
# ``CLAUDE.md: "X"``; curly quotes too. Line breaks inside a citation are
# flattened first (see ``_flatten``). A ``"CLAUDE.md"`` string literal in code
# is a file name, not a citation, so a quote, path separator or word character
# right before ``CLAUDE`` disqualifies the match, and the cited name is prose
# (letters, digits, spaces and a few punctuation marks), not code. Markdown
# cites the file in a code span (`` `CLAUDE.md` ("Sharing the machine") ``),
# and a citation inside a Python string (``ops/integrate.py``) carries escaped
# quotes; both forms are read.
QUOTED = re.compile(
    r"""(?<!["'/\\\w])CLAUDE\.md`?(?:'s)?[,:]?\s*\(?\s*\\?["“]([A-Za-z][A-Za-z0-9 ,'/-]{1,88})\\?["”]""")
ANCHOR = re.compile(r"CLAUDE\.md#([a-z0-9-]+)")
# A comment or list continuation: newline, indentation, an optional comment
# marker, then text. Joined with one space so a wrapped citation reads whole.
CONTINUATION = re.compile(r"\n[ \t]*(?:#|//|\*|--|<!--)?[ \t]*")


def _mentioning_files() -> list[Path]:
    """Tracked files that mention CLAUDE.md at all (``git grep`` shortlists them, so
    the scan never reads a harness golden that cannot cite anything)."""
    out = subprocess.run(["git", "grep", "-lz", "--", "CLAUDE.md"], cwd=ROOT, capture_output=True)
    files = []
    for rel in out.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not rel or rel in SKIP or rel.startswith(SKIP_DIRS):
            continue
        files.append(ROOT / rel)
    return files


def _flatten(text: str) -> str:
    return CONTINUATION.sub(" ", text)


@functools.lru_cache(maxsize=None)
def _names() -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]:
    """Headings (parenthetical stripped), bold rule leads, and the anchors a
    link may use: GitHub's slug of the full heading and of the stripped one."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    raw = [h.strip() for h in re.findall(r"^#{1,3}\s+(.+?)\s*$", text, re.M)]
    headings = tuple(re.sub(r"\s*\(.*\)\s*$", "", h).lower() for h in raw)
    bold = tuple(b.strip(" :").lower() for b in re.findall(r"\*\*([^*]+)\*\*", " ".join(text.split())))
    anchors = frozenset(_anchor(h) for h in raw) | frozenset(_anchor(h) for h in headings)
    return headings, bold, anchors


def _anchor(heading: str) -> str:
    """GitHub's heading slug: lowercase, punctuation dropped, spaces to hyphens."""
    slug = re.sub(r"[^\w\- ]", "", heading.lower().replace("—", "-"))
    return re.sub(r" ", "-", slug.strip())


def _cited(name: str, headings: tuple[str, ...], bold: tuple[str, ...]) -> bool:
    n = name.strip(" :").lower()
    return any(h == n or h.startswith(n) for h in headings) or any(b == n or b.startswith(n) for b in bold)


@functools.lru_cache(maxsize=None)
def _citations() -> tuple[tuple[str, str], ...]:
    found = []
    for path in _mentioning_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        flat = _flatten(text)
        rel = path.relative_to(ROOT).as_posix()
        found.extend((rel, m) for m in QUOTED.findall(flat))
        found.extend((rel, "#" + m) for m in ANCHOR.findall(flat))
    return tuple(found)


def test_every_cited_claude_md_heading_exists():
    headings, bold, anchors = _names()
    stale = []
    for rel, name in _citations():
        if name.startswith("#"):
            if name[1:] not in anchors:
                stale.append(f"{rel}: CLAUDE.md{name}")
        elif not _cited(name, headings, bold):
            stale.append(f'{rel}: CLAUDE.md "{name}"')
    assert not stale, "citations of CLAUDE.md names that do not exist:\n  " + "\n  ".join(stale)


def test_anchors_follow_github_slugs():
    """A link to a heading with a parenthetical uses GitHub's slug of the whole
    heading; the stripped form is accepted too, since prose cites it that way."""
    _, _, anchors = _names()
    assert "shipping-checklist-any-change-that-lands-on-master" in anchors
    assert "shipping-checklist" in anchors
    assert "running-tests" in anchors


def test_the_citation_scan_sees_the_known_citations():
    """The scan must find the workflow and tool citations it exists for, so a
    regex regression cannot pass by finding nothing."""
    cited = set(_citations())
    assert (".github/workflows/rust.yml", "Docs-only changes") in cited
    assert (".github/workflows/rust.yml", "Disk space") in cited
    assert ("ops/integrate.py", "Dev machines") in cited
    assert ("CONTRIBUTING.md", "#running-tests") in cited
    # Markdown cites the file in a code span: `CLAUDE.md` ("Sharing the machine").
    assert ("docs/dev/dev-machines.md", "Sharing the machine") in cited


@pytest.mark.parametrize("name", ["Running tests", "Shipping checklist", "Dev machines", "Disk space",
                                  "Stopping processes", "Review discipline", "Docs-only changes",
                                  "Install and update experience", "Sharing the machine"])
def test_protected_names_stay(name):
    headings, bold, _ = _names()
    assert _cited(name, headings, bold), f"CLAUDE.md lost the cited name {name!r}"


def test_claude_md_stays_under_its_word_ceiling():
    words = len(CLAUDE_MD.read_text(encoding="utf-8").split())
    assert words <= WORD_CEILING, f"CLAUDE.md is {words} words; the reshape ceiling is {WORD_CEILING}"
