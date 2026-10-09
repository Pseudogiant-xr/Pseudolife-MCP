"""Changelog fragments: each PR adds `changelog.d/<YYYY-MM-DD>-<slug>.md`
instead of editing CHANGELOG.md, so parallel PRs stop conflicting on the
same lines (maintainer decision 2026-10-09: every merge forced every other
open PR to forward and re-run CI over a CHANGELOG conflict). The release
cut folds the fragments into `## [Unreleased]` with
`python ops/assemble_changelog.py` and deletes them."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FRAGMENTS = REPO / "changelog.d"
NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9][a-z0-9-]*\.md$")
HEADING = re.compile(
    r"^### (Added|Changed|Deprecated|Removed|Fixed|Security|Measured"
    r"|Performance) \((\d{4}-\d{2}-\d{2}) — [^\n]+\)( \[#\d+\])?$")


def _assembler():
    spec = importlib.util.spec_from_file_location(
        "assemble_changelog", REPO / "ops" / "assemble_changelog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fragments():
    return sorted(p for p in FRAGMENTS.glob("*.md") if p.name != "README.md")


@pytest.mark.parametrize("path", _fragments(), ids=lambda p: p.name)
def test_fragment_is_one_dated_unreleased_entry(path):
    match = NAME.match(path.name)
    assert match, f"{path.name}: name it YYYY-MM-DD-<lowercase-slug>.md"
    text = path.read_text(encoding="utf-8")
    first = text.splitlines()[0] if text else ""
    heading = HEADING.match(first)
    assert heading, (f"{path.name}: first line must be "
                     "'### <Added|Changed|...> (YYYY-MM-DD — title)'")
    assert heading.group(2) == match.group(1), (
        f"{path.name}: heading date {heading.group(2)} differs from the "
        f"file name's {match.group(1)}")
    assert re.search(r"^- ", text, flags=re.M), f"{path.name}: needs a bullet"
    assert text.count("\n### ") == 0, f"{path.name}: one entry per fragment"
    assert not re.search(r"^## ", text, flags=re.M), (
        f"{path.name}: no '## ' release headers in a fragment")


def test_readme_explains_the_convention():
    readme = (FRAGMENTS / "README.md").read_text(encoding="utf-8")
    assert "ops/assemble_changelog.py" in readme


def _write(root: Path, changelog: str, fragments: dict[str, str]) -> None:
    (root / "changelog.d").mkdir()
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (root / "changelog.d" / "README.md").write_text("convention\n", encoding="utf-8")
    for name, text in fragments.items():
        (root / "changelog.d" / name).write_text(text, encoding="utf-8")


BASE = ("# Changelog\n\nIntro.\n\n## [Unreleased]\n\n"
        "### Fixed (2026-10-01 — an older entry)\n\n- old\n\n"
        "## [0.17.0] - 2026-10-05 — release\n\n- shipped\n")
OLD = "### Added (2026-10-08 — older fragment)\n\n- one\n"
NEW = "### Fixed (2026-10-09 — newer fragment)\n\n- two\n"


def test_assemble_folds_fragments_newest_first_and_deletes_them(tmp_path):
    _write(tmp_path, BASE, {"2026-10-08-older.md": OLD, "2026-10-09-newer.md": NEW})
    folded = _assembler().assemble(tmp_path)
    assert folded == 2
    text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("## [Unreleased]\n", 1)[1].split("## [0.17.0]", 1)[0]
    assert unreleased.index("newer fragment") < unreleased.index("older fragment")
    assert unreleased.index("older fragment") < unreleased.index("an older entry")
    assert "## [0.17.0] - 2026-10-05" in text
    assert [p.name for p in (tmp_path / "changelog.d").iterdir()] == ["README.md"]


def test_assemble_without_fragments_changes_nothing(tmp_path):
    _write(tmp_path, BASE, {})
    assert _assembler().assemble(tmp_path) == 0
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == BASE


def test_changelog_text_includes_pending_fragments(tmp_path):
    _write(tmp_path, BASE, {"2026-10-09-newer.md": NEW})
    text = _assembler().changelog_text(tmp_path)
    assert "an older entry" in text and "newer fragment" in text


def test_assemble_refuses_a_changelog_without_unreleased(tmp_path):
    _write(tmp_path, "# Changelog\n\n## [0.17.0]\n", {"2026-10-09-newer.md": NEW})
    with pytest.raises(SystemExit):
        _assembler().assemble(tmp_path)


@pytest.mark.parametrize("heading", [
    "### Measured (2026-10-10 — an eval result)",
    "### Performance (2026-10-10 — a speedup)",
    "### Fixed (2026-10-10 — a bug) [#189]",
])
def test_heading_accepts_the_changelogs_house_kinds(heading):
    assert HEADING.match(heading), heading


def test_assemble_matches_the_heading_line_not_prose(tmp_path):
    prose = "# Changelog\n\nEntries go under ## [Unreleased]\nuntil a cut.\n\n"
    _write(tmp_path, prose + BASE.split("\n\n", 2)[2], {"2026-10-09-newer.md": NEW})
    _assembler().assemble(tmp_path)
    text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.startswith(prose)
    assert text.index("newer fragment") > text.index("## [Unreleased]\n\n")


def test_assemble_writes_lf_line_endings(tmp_path):
    _write(tmp_path, BASE, {"2026-10-09-newer.md": NEW})
    _assembler().assemble(tmp_path)
    assert b"\r\n" not in (tmp_path / "CHANGELOG.md").read_bytes()
