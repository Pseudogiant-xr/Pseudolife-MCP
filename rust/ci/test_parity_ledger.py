"""The parity register stays one file per table row (rust/parity/)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rust"))

import split_parity as sp  # noqa: E402

BASE = """\
# Behaviour parity register

## Parity rows

Intro prose before the table.

| Row | Phase | Status |
|---|---|---|
| HTTP-READS | 3 | deferred |
| PG-HYDRATE | 3 | ported |
| A body `{"error": "x|y"}` | 4 | deferred |

Prose after the table that names it.

## Complete Console registration checklist

Shared source: routes.py. The Rust column says what Rust answers.

| Method | Path | Phase | Status | Rust |
|---|---|---|---|---|
| GET | /api/search | 3 | ported | served |
| GET | /api/trace | 3 | deferred | 501 |
| GET | /api/maintainer | 3 | deferred | 501 |
| POST | /api/maintainer | 4 | deferred | 501 |

Closing paragraph one.

Closing paragraph two.

## Configuration sections

| Section | Fields | Status |
|---|---|---|
| `AppConfig` | 8 | deferred |
| `AppConfig` | 8 | deferred |

| `MaintainerConfig` | 3 | deferred |

## Low-priority observations

**History.** A bold-led paragraph stays prose.

### Hook producer reduction

| Case | Status |
|---|---|
| hook-a | deferred |
"""


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(root.rglob("*")) if p.is_file()}


def converted(tmp_path: Path, text: str = BASE) -> Path:
    root = tmp_path / "parity"
    assert sp.convert(sp.parse_ledger(text), None, root) == []
    return root


def test_the_committed_register_is_the_pointer_and_a_valid_directory():
    assert sp.check() == []


def test_the_committed_directory_renders_and_parses_back_to_itself():
    ledger = sp.load_dir()
    assert sp.parse_ledger(sp.render(ledger)) == ledger


def test_the_rendered_register_is_what_readers_get():
    text = sp.register_text()
    assert text.startswith("# Behaviour parity register\n")
    assert "| GET | /api/search |" in text and "| CONFIG-LOAD |" in text


def test_conversion_keeps_every_row_and_rerunning_changes_nothing(tmp_path):
    root = converted(tmp_path)
    ledger = sp.parse_ledger(BASE)
    assert sp.load_dir(root) == ledger
    files = snapshot(root)
    # Prose and table headers stay in order in the section file; rows leave.
    assert files["parity-rows/_section.md"] == (
        "## Parity rows\n\nIntro prose before the table.\n\n"
        "| Row | Phase | Status |\n|---|---|---|\n\nProse after the table that names it.\n")
    assert files["parity-rows/http-reads.md"] == (
        "| Row | Phase | Status |\n|---|---|---|\n| HTTP-READS | 3 | deferred |\n")
    assert "parity-rows/a-body-error-x-y.md" in files
    # A Method column keys on method and path together.
    console = sorted(n for n in files if n.startswith("complete-console-registration/"))
    assert console == ["complete-console-registration/_section.md",
                       "complete-console-registration/get-api-maintainer.md",
                       "complete-console-registration/get-api-search.md",
                       "complete-console-registration/get-api-trace.md",
                       "complete-console-registration/post-api-maintainer.md"]
    # A duplicated row is one entry; a row a blank line cut from its table joins it.
    assert sorted(n for n in files if n.startswith("configuration-sections/")) == [
        "configuration-sections/_section.md", "configuration-sections/appconfig.md",
        "configuration-sections/maintainerconfig.md"]
    assert files["configuration-sections/maintainerconfig.md"].startswith("| Section | Fields | Status |\n")
    assert "**History.**" in files["low-priority-observations/_section.md"]
    assert files["low-priority-observations/hook-producer-reduction/_section.md"] == "### Hook producer reduction\n\n| Case | Status |\n|---|---|\n"
    assert sp.convert(sp.parse_ledger(BASE), sp.parse_ledger(BASE), root) == []
    assert snapshot(root) == files


def test_render_puts_each_tables_rows_under_its_header(tmp_path):
    root = converted(tmp_path)
    text = sp.render(sp.load_dir(root))
    assert ("| Row | Phase | Status |\n|---|---|---|\n| A body `{\"error\": \"x|y\"}` | 4 | deferred |\n"
            "| HTTP-READS | 3 | deferred |\n| PG-HYDRATE | 3 | ported |\n\nProse after the table") in text
    assert "| `AppConfig` | 8 | deferred |\n| `MaintainerConfig` | 3 | deferred |\n" in text


def test_a_forward_merge_applies_only_the_branchs_own_changes(tmp_path):
    root = converted(tmp_path)
    console = root / "complete-console-registration"
    # Master, after the split: serves /api/trace and adds a route.
    trace = console / "get-api-trace.md"
    trace.write_text(trace.read_text(encoding="utf-8").replace("| deferred | 501 |", "| deferred | served |"),
                     encoding="utf-8", newline="\n")
    (console / "get-api-recall.md").write_text(
        "| Method | Path | Phase | Status | Rust |\n|---|---|---|---|---|\n| GET | /api/recall | 3 | deferred | 501 |\n",
        encoding="utf-8", newline="\n")
    # The branch, on the single file: serves the neighbouring row, closes
    # HTTP-READS, adds a hook case and leaves /api/trace as it found it.
    ours = BASE.replace("| GET | /api/maintainer | 3 | deferred | 501 |", "| GET | /api/maintainer | 3 | deferred | served |")
    ours = ours.replace("| HTTP-READS | 3 | deferred |\n", "")
    ours = ours.replace("| hook-a | deferred |\n", "| hook-a | deferred |\n| hook-b | ported |\n")
    assert sp.convert(sp.parse_ledger(ours), sp.parse_ledger(BASE), root) == []
    files = snapshot(root)
    assert "served" in files["complete-console-registration/get-api-trace.md"]
    assert "served" in files["complete-console-registration/get-api-maintainer.md"]
    assert "complete-console-registration/get-api-recall.md" in files
    assert "parity-rows/http-reads.md" not in files
    assert files["low-priority-observations/hook-producer-reduction/hook-b.md"].endswith("| hook-b | ported |\n")
    sp.load_dir(root)


def test_both_sides_editing_different_paragraphs_of_one_section_merge(tmp_path):
    root = converted(tmp_path)
    section = root / "complete-console-registration/_section.md"
    section.write_text(section.read_text(encoding="utf-8").replace("paragraph two.", "paragraph two, edited on master."),
                       encoding="utf-8", newline="\n")
    ours = BASE.replace("The Rust column says what Rust answers.", "The Rust column says what Rust answers today.")
    assert sp.convert(sp.parse_ledger(ours), sp.parse_ledger(BASE), root) == []
    text = section.read_text(encoding="utf-8")
    assert "answers today." in text and "edited on master." in text


def test_a_row_both_sides_changed_is_written_with_conflict_markers(tmp_path):
    root = converted(tmp_path)
    entry = root / "parity-rows/pg-hydrate.md"
    entry.write_text(entry.read_text(encoding="utf-8").replace("| 3 | ported |", "| 3 | deferred |"),
                     encoding="utf-8", newline="\n")
    ours = BASE.replace("| PG-HYDRATE | 3 | ported |", "| PG-HYDRATE | 4 | ported |")
    assert sp.convert(sp.parse_ledger(ours), sp.parse_ledger(BASE), root) == ["parity/parity-rows/pg-hydrate.md"]
    text = entry.read_text(encoding="utf-8")
    assert "<<<<<<< " in text and "| 4 | ported |" in text and "| 3 | deferred |" in text
    with pytest.raises(sp.LedgerError, match="conflict markers"):
        sp.load_dir(root)


def test_converting_with_no_base_over_the_directory_is_refused(tmp_path):
    root = converted(tmp_path)
    files = snapshot(root)
    ours = BASE.replace("| PG-HYDRATE | 3 | ported |", "| PG-HYDRATE | 3 | stale copy |")
    with pytest.raises(sp.LedgerError, match="no merge base is known"):
        sp.convert(sp.parse_ledger(ours), None, root)
    assert snapshot(root) == files


def run_main(monkeypatch, tmp_path, ledger_text, argv, base=None, unmerged=False):
    ledger = tmp_path / "PARITY.md"
    ledger.write_text(ledger_text, encoding="utf-8", newline="\n")
    monkeypatch.setattr(sp, "LEDGER", ledger)
    monkeypatch.setattr(sp, "ROOT_DIR", tmp_path / "parity")
    monkeypatch.setattr(sp, "merge_base", lambda: base)
    monkeypatch.setattr(sp, "register_unmerged", lambda: unmerged)
    return sp.main(argv)


def test_the_pointer_left_unmerged_is_refused_not_taken_as_nothing_to_do(monkeypatch, tmp_path, capsys):
    # A rebase, or the wrong side taken in a merge, leaves the pointer in the
    # working tree and the branch's single file only in the index.
    assert run_main(monkeypatch, tmp_path, BASE, ["--full"]) == 0
    files = snapshot(tmp_path / "parity")
    assert run_main(monkeypatch, tmp_path, sp.STUB, [], unmerged=True) == 1
    assert "git left it unmerged" in capsys.readouterr().err
    assert snapshot(tmp_path / "parity") == files


def test_text_between_the_title_and_the_first_section_is_refused():
    with pytest.raises(sp.LedgerError, match="between its title and the first section"):
        sp.parse_ledger(BASE.replace("# Behaviour parity register\n", "# Behaviour parity register\n\nAn intro.\n"))


def test_a_heading_inside_a_section_file_is_refused_with_the_layout(tmp_path):
    root = converted(tmp_path)
    section = root / "parity-rows/_section.md"
    section.write_text(section.read_text(encoding="utf-8") + "\n### A new subsection\n\nText.\n",
                       encoding="utf-8", newline="\n")
    with pytest.raises(sp.LedgerError, match="a heading starts a section, which is a directory"):
        sp.load_dir(root)


def test_a_byte_order_mark_and_trailing_blank_lines_are_accepted(tmp_path):
    root = converted(tmp_path)
    entry = root / "parity-rows/pg-hydrate.md"
    entry.write_text("﻿" + entry.read_text(encoding="utf-8") + "\n\n", encoding="utf-8", newline="\n")
    heading = root / "parity-rows/_section.md"
    heading.write_text(heading.read_text(encoding="utf-8").replace("## Parity rows\n", "## Parity rows  \n"),
                       encoding="utf-8", newline="\n")
    assert sp.load_dir(root) == sp.parse_ledger(BASE)


def test_markers_an_earlier_run_left_are_refused_before_merging_again(tmp_path):
    root = converted(tmp_path)
    entry = root / "parity-rows/pg-hydrate.md"
    entry.write_text(sp.conflict_text("a\n", "b\n"), encoding="utf-8", newline="\n")
    ours = BASE.replace("| PG-HYDRATE | 3 | ported |", "| PG-HYDRATE | 4 | ported |")
    with pytest.raises(sp.LedgerError, match="parity/parity-rows/pg-hydrate.md"):
        sp.convert(sp.parse_ledger(ours), sp.parse_ledger(BASE), root)


def test_rows_master_added_to_a_section_the_branch_renamed_are_named(tmp_path):
    root = converted(tmp_path)
    (root / "parity-rows/new-row.md").write_text(
        "| Row | Phase | Status |\n|---|---|---|\n| NEW-ROW | 4 | deferred |\n", encoding="utf-8", newline="\n")
    ours = BASE.replace("## Parity rows", "## Rows of the register")
    conflicts = sp.convert(sp.parse_ledger(ours), sp.parse_ledger(BASE), root)
    assert conflicts == ["parity/parity-rows/_section.md", "parity/parity-rows/new-row.md"]
    assert "parity/parity-rows/new-row.md" in (root / "parity-rows/_section.md").read_text(encoding="utf-8")
    assert (root / "rows-of-the-register/pg-hydrate.md").is_file()


def test_the_script_outside_a_merge_refuses_and_names_the_merge(monkeypatch, tmp_path, capsys):
    assert run_main(monkeypatch, tmp_path, BASE, []) == 1
    assert "run this during `git merge`" in capsys.readouterr().err
    assert not (tmp_path / "parity").exists()


def test_the_one_time_full_conversion_writes_the_pointer(monkeypatch, tmp_path):
    assert run_main(monkeypatch, tmp_path, BASE, ["--full"]) == 0
    assert (tmp_path / "PARITY.md").read_text(encoding="utf-8") == sp.STUB
    assert sp.load_dir(tmp_path / "parity") == sp.parse_ledger(BASE)
    assert run_main(monkeypatch, tmp_path, sp.STUB, []) == 0      # the pointer: nothing to do


def test_a_heading_reworded_past_its_name_is_an_edit_not_a_removal(tmp_path):
    long = "## Complete Console registration checklist"
    ours = BASE.replace(long, long + " (the 72 registrations)")
    root = converted(tmp_path)
    assert sp.convert(sp.parse_ledger(ours), sp.parse_ledger(BASE), root) == []
    section = root / "complete-console-registration"
    assert "(the 72 registrations)" in (section / "_section.md").read_text(encoding="utf-8")
    assert len(list(section.iterdir())) == 5


def test_a_section_master_removed_and_the_branch_edited_is_a_conflict(tmp_path):
    root = converted(tmp_path)
    nested = root / "low-priority-observations/hook-producer-reduction"
    for path in sorted(nested.iterdir()):
        path.unlink()
    nested.rmdir()
    ours = BASE.replace("| hook-a | deferred |", "| hook-a | ported |")
    conflicts = sp.convert(sp.parse_ledger(ours), sp.parse_ledger(BASE), root)
    assert "parity/low-priority-observations/hook-producer-reduction/hook-a.md" in conflicts


def test_a_register_with_conflict_markers_is_refused_with_the_fix():
    merged = "<<<<<<< HEAD\n" + BASE + "=======\n" + sp.STUB + ">>>>>>> origin/master\n"
    with pytest.raises(sp.LedgerError, match="git checkout --ours"):
        sp.parse_ledger(merged)


@pytest.mark.parametrize("name, text, error", [
    ("http-reads.md", "| Row | Phase | Status |\n|---|---|---|\n| HTTP-READS | 3 | x |\n| MORE | 3 | x |\n",
     "exactly one row"),
    ("wrong-name.md", "| Row | Phase | Status |\n|---|---|---|\n| HTTP-READS | 3 | x |\n", "belongs in http-reads.md"),
    ("http-reads.md", "| HTTP-READS | 3 | x |\n", "exactly one row"),
    ("note.md", "A paragraph.\n", "exactly one row"),
    ("http-reads.md", "| Row | Phase | State |\n|---|---|---|\n| HTTP-READS | 3 | x |\n",
     "header is not a table header in _section.md"),
])
def test_a_malformed_entry_file_is_refused_with_its_fix(tmp_path, name, text, error):
    root = converted(tmp_path)
    (root / "parity-rows" / name).write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(sp.LedgerError, match=error):
        sp.load_dir(root)


def test_two_rows_one_name_would_hold_are_refused_with_the_fix():
    twice = BASE.replace("| PG-HYDRATE | 3 | ported |\n", "| PG-HYDRATE | 3 | ported |\n| pg hydrate | 4 | x |\n")
    with pytest.raises(sp.LedgerError, match="two rows would be pg-hydrate.md"):
        sp.parse_ledger(twice)


def test_two_tables_one_section_with_one_header_are_refused():
    twice = BASE.replace("Closing paragraph one.",
                         "| Method | Path | Phase | Status | Rust |\n|---|---|---|---|---|\n| PUT | /api/x | 4 | deferred | 501 |")
    with pytest.raises(sp.LedgerError, match="share a header"):
        sp.parse_ledger(twice)


LINKED = BASE.replace(
    "Intro prose before the table.",
    "Intro prose: [the harness](cli_harness/README.md), [evidence](../evals/x.json), "
    "[the checklist](#complete-console-registration-checklist), [a hook](#hook-producer-reduction), "
    "[elsewhere](#no-such-heading) and [docs](https://example.com/a#b).").replace(
    "| PG-HYDRATE | 3 | ported |", "| PG-HYDRATE | 3 | ported ([spec](daemon/spec.md#x)) |")


def test_links_are_written_relative_to_each_file_and_read_back(tmp_path):
    root = converted(tmp_path, LINKED)
    files = snapshot(root)
    section = files["parity-rows/_section.md"]
    assert "[the harness](../../cli_harness/README.md)" in section
    assert "[evidence](../../../evals/x.json)" in section
    assert "[the checklist](../complete-console-registration/)" in section
    assert "[a hook](../low-priority-observations/hook-producer-reduction/)" in section
    assert "[elsewhere](#no-such-heading)" in section and "(https://example.com/a#b)" in section
    assert "([spec](../../daemon/spec.md#x))" in files["parity-rows/pg-hydrate.md"]
    # The structure and the rendered register keep the single file's links.
    ledger = sp.load_dir(root)
    assert ledger == sp.parse_ledger(LINKED)
    assert "[the harness](cli_harness/README.md)" in sp.render(ledger)


def test_a_nested_section_climbs_one_level_further(tmp_path):
    text = BASE.replace("| hook-a | deferred |", "| hook-a | deferred ([row](cli_harness/x.py), [up](#parity-rows)) |")
    root = converted(tmp_path, text)
    entry = snapshot(root)["low-priority-observations/hook-producer-reduction/hook-a.md"]
    assert "([row](../../../cli_harness/x.py), [up](../../parity-rows/))" in entry
    assert sp.load_dir(root) == sp.parse_ledger(text)


def test_a_link_written_relative_to_rust_in_a_row_file_is_refused_with_the_fix(tmp_path):
    root = converted(tmp_path)
    entry = root / "parity-rows/pg-hydrate.md"
    entry.write_text(entry.read_text(encoding="utf-8").replace("| ported |", "| ported [x](daemon/spec.md) |"),
                     encoding="utf-8", newline="\n")
    with pytest.raises(sp.LedgerError, match=r"relative to this file \(\.\./\.\./daemon/spec\.md\)"):
        sp.load_dir(root)


def test_a_forward_merge_of_a_linked_row_keeps_its_links(tmp_path):
    root = converted(tmp_path, LINKED)
    ours = LINKED.replace("| PG-HYDRATE | 3 | ported ([spec]", "| PG-HYDRATE | 4 | ported ([spec]")
    assert sp.convert(sp.parse_ledger(ours), sp.parse_ledger(LINKED), root) == []
    text = (root / "parity-rows/pg-hydrate.md").read_text(encoding="utf-8")
    assert text.endswith("| PG-HYDRATE | 4 | ported ([spec](../../daemon/spec.md#x)) |\n")


def test_a_name_is_the_key_cells_opening_words():
    assert sp.row_key("| Method | Path |", "| GET | /api/maintainer/sent | 3 | deferred |") == "GET /api/maintainer/sent"
    assert sp.row_key("| Row | Phase |", "| **CLI-HOOK** closed | 4 |") == "CLI-HOOK"
    assert sp.slug("`PSEUDOLIFE_SUITE_PRUNE_DAYS`") == "pseudolife-suite-prune-days"
