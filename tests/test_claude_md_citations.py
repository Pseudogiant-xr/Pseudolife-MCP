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
# (letters, digits, spaces and a few punctuation marks), not code. A citation
# inside a Python string (``ops/integrate.py``) carries escaped quotes.
QUOTED = re.compile(
    r"""(?<!["'`/\\\w])CLAUDE\.md(?:'s)?[,:]?\s*\(?\s*\\?["“]([A-Za-z][A-Za-z0-9 ,'/-]{1,88})\\?["”]""")
ANCHOR = re.compile(r"CLAUDE\.md#([a-z0-9-]+)")
# A comment or list continuation: newline, indentation, an optional comment
# marker, then text. Joined with one space so a wrapped citation reads whole.
CONTINUATION = re.compile(r"\n[ \t]*(?:#|//|\*|--|<!--)?[ \t]*")


def _tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    files = []
    for rel in out.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not rel or rel in SKIP or rel.startswith(SKIP_DIRS):
            continue
        files.append(ROOT / rel)
    return files


def _flatten(text: str) -> str:
    return CONTINUATION.sub(" ", text)


def _names() -> tuple[list[str], list[str]]:
    text = CLAUDE_MD.read_text(encoding="utf-8")
    headings = [re.sub(r"\s*\(.*\)\s*$", "", h).strip().lower()
                for h in re.findall(r"^#{1,3}\s+(.+?)\s*$", text, re.M)]
    bold = [b.strip(" :").lower() for b in re.findall(r"\*\*([^*]+)\*\*", " ".join(text.split()))]
    return headings, bold


def _anchor(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading).strip("-")


def _cited(name: str, headings: list[str], bold: list[str]) -> bool:
    n = name.strip(" :").lower()
    return any(h == n or h.startswith(n) for h in headings) or any(b == n or b.startswith(n) for b in bold)


def _citations() -> list[tuple[str, str]]:
    found = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "CLAUDE.md" not in text:
            continue
        flat = _flatten(text)
        rel = path.relative_to(ROOT).as_posix()
        found.extend((rel, m) for m in QUOTED.findall(flat))
        found.extend((rel, "#" + m) for m in ANCHOR.findall(flat))
    return found


def test_every_cited_claude_md_heading_exists():
    headings, bold = _names()
    anchors = {_anchor(h) for h in headings}
    stale = []
    for rel, name in _citations():
        if name.startswith("#"):
            if name[1:] not in anchors:
                stale.append(f"{rel}: CLAUDE.md{name}")
        elif not _cited(name, headings, bold):
            stale.append(f'{rel}: CLAUDE.md "{name}"')
    assert not stale, "citations of CLAUDE.md names that do not exist:\n  " + "\n  ".join(stale)


def test_the_citation_scan_sees_the_known_citations():
    """The scan must find the workflow and tool citations it exists for, so a
    regex regression cannot pass by finding nothing."""
    cited = {(rel, name) for rel, name in _citations()}
    assert (".github/workflows/rust.yml", "Docs-only changes") in cited
    assert (".github/workflows/rust.yml", "Disk space") in cited
    assert ("ops/integrate.py", "Dev machines") in cited
    assert ("CONTRIBUTING.md", "#running-tests") in cited


@pytest.mark.parametrize("name", ["Running tests", "Shipping checklist", "Dev machines", "Disk space",
                                  "Stopping processes", "Review discipline", "Docs-only changes",
                                  "Install and update experience", "Sharing the machine"])
def test_protected_names_stay(name):
    headings, bold = _names()
    assert _cited(name, headings, bold), f"CLAUDE.md lost the cited name {name!r}"


def test_claude_md_stays_under_its_word_ceiling():
    words = len(CLAUDE_MD.read_text(encoding="utf-8").split())
    assert words <= WORD_CEILING, f"CLAUDE.md is {words} words; the reshape ceiling is {WORD_CEILING}"
