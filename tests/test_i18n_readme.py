"""Guards for the translated README front doors (docs/i18n/).

Design (2026-07-17): translations carry narrative, never guard-pinned
numbers; commands are NEVER translated, so fenced code blocks must stay
byte-identical to the committed English source — that turns the worst
drift class (broken install commands in a language we can't skim) into a
RED. Each translation declares which source version it was synced from.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "i18n" / "README.source.md"
LANGS = ["zh", "ja", "ko", "pt-br", "es"]


def _code_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-zA-Z]*\r?\n(.*?)```", text, re.S)


def _norm(blocks: list[str]) -> list[str]:
    # CRLF/LF must not fail the byte-identity contract
    return [b.replace("\r\n", "\n").strip() for b in blocks]


def test_source_exists_with_version_and_blocks():
    text = SRC.read_text(encoding="utf-8")
    assert re.search(r"<!-- i18n-source: v\d+ ", text), "source needs a version marker"
    assert len(_code_blocks(text)) >= 2, "regex sanity — source carries the command blocks"


@pytest.mark.parametrize("lang", LANGS)
def test_translation_synced_and_commands_identical(lang):
    src_text = SRC.read_text(encoding="utf-8")
    version = re.search(r"<!-- i18n-source: (v\d+) ", src_text).group(1)

    p = ROOT / "docs" / "i18n" / f"README.{lang}.md"
    assert p.is_file(), f"missing translation docs/i18n/README.{lang}.md"
    text = p.read_text(encoding="utf-8")

    # declared sync against the CURRENT source version
    assert f"<!-- i18n-sync: {version} -->" in text, (
        f"{p.name} is synced to an older source — re-translate or bump its marker")

    # commands are language-invariant: fenced blocks byte-identical to source
    assert _norm(_code_blocks(text)) == _norm(_code_blocks(src_text)), (
        f"{p.name}: fenced code blocks differ from README.source.md — "
        "commands must never be translated or edited per-language")

    # The version a READER sees must agree with the one the machine sees.
    # Each translation states its sync version twice: the HTML comment above,
    # and a human-readable line near the top ("synced: v6 (2026-07-29)",
    # "已同步:v6 …", "同期バージョン: v6 …"). Only the comment was pinned, so
    # on the v5 -> v6 bump the visible line silently kept saying v5 — a
    # contradiction shown to every non-English reader and invisible to this
    # file. The phrasing differs per language; the `vN (YYYY-MM-DD)` stamp
    # does not, so match on that rather than on any one translation's wording.
    stamp = re.search(r"<!-- i18n-source: (v\d+ \(\d{4}-\d{2}-\d{2}\))",
                      src_text).group(1)
    assert stamp in text, (
        f"{p.name}: the human-readable sync line disagrees with "
        f"<!-- i18n-sync: {version} --> — it should read {stamp!r}. Bump both, "
        "or a reader is told a different version than the guard enforces.")

    # every reader can find the canonical English docs
    assert "../../README.md" in text


def test_main_readme_language_bar():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for lang in LANGS:
        assert f"docs/i18n/README.{lang}.md" in readme, (
            f"README language bar missing link to {lang}")
