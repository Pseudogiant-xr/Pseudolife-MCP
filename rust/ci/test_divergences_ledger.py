"""The declared-divergence ledger stays one file per entry (rust/daemon/divergences/)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rust/daemon"))

import split_divergences as sd  # noqa: E402

BASE = """\
# Declared divergences: Rust daemon vs the Python daemon

Preamble.

## Routes (answer 501)

| Surface | Python behaviour | Owner |
|---|---|---|
| `/api/stats` | the handler's answer | W2-D |
| `/api/config` | settings | W2-D |
| A body `{"error": "x|y"}` | parsed | W2-E |

## MCP surface

Intro prose.

**MCP-ONE:** a bold-labelled paragraph
over two lines.

| Item | Python | Rust | Owner |
|---|---|---|---|
| MCP-LIVE | kept | dropped | B6 |

### Binding

| Name | Python | Rust | Why |
|---|---|---|---|
| BIND-A | a | b | c |
"""


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(root.rglob("*")) if p.is_file()}


def converted(tmp_path: Path, text: str = BASE) -> Path:
    root = tmp_path / "divergences"
    assert sd.convert(sd.parse_ledger(text), None, root) == []
    return root


def test_the_committed_ledger_is_the_pointer_and_a_valid_directory():
    assert sd.check() == []


def test_the_committed_directory_renders_and_parses_back_to_itself():
    ledger = sd.load_dir()
    assert sd.parse_ledger(sd.render(ledger)) == ledger


def test_conversion_keeps_every_entry_and_rerunning_changes_nothing(tmp_path):
    root = converted(tmp_path)
    ledger = sd.parse_ledger(BASE)
    assert sd.load_dir(root) == ledger
    assert set(ledger[("Routes (answer 501)",)]["entries"]) == {"api-stats", "api-config", "a-body-error-x-y"}
    assert ledger[("MCP surface",)]["entries"]["mcp-one"].startswith("**MCP-ONE:**")
    assert ledger[("MCP surface",)]["prose"] == ["Intro prose."]
    files = snapshot(root)
    assert files["mcp-surface/binding/_section.md"] == "### Binding\n"
    assert files["routes-answer-501/api-stats.md"] == (
        "| Surface | Python behaviour | Owner |\n|---|---|---|\n| `/api/stats` | the handler's answer | W2-D |\n")
    assert sd.convert(sd.parse_ledger(BASE), sd.parse_ledger(BASE), root) == []
    assert snapshot(root) == files


def test_a_forward_merge_applies_only_the_branchs_own_changes(tmp_path):
    root = converted(tmp_path)
    base = sd.parse_ledger(BASE)
    routes = root / "routes-answer-501"
    # Master, after the split: edits one row and adds another.
    (routes / "api-config.md").write_text(
        "| Surface | Python behaviour | Owner |\n|---|---|---|\n| `/api/config` | settings, served | B2d |\n",
        encoding="utf-8", newline="\n")
    (routes / "api-pair.md").write_text(
        "| Surface | Python behaviour | Owner |\n|---|---|---|\n| `/api/pair` | redemption | W2-F |\n",
        encoding="utf-8", newline="\n")
    # The branch, on the old single file: closes /api/stats, edits MCP-LIVE,
    # adds a binding row, and leaves /api/config as it found it.
    ours = BASE.replace("| `/api/stats` | the handler's answer | W2-D |\n", "")
    ours = ours.replace("| MCP-LIVE | kept | dropped | B6 |", "| MCP-LIVE | kept | kept | closed |")
    ours = ours.replace("| BIND-A | a | b | c |\n", "| BIND-A | a | b | c |\n| BIND-B | d | e | f |\n")
    assert sd.convert(sd.parse_ledger(ours), base, root) == []
    files = snapshot(root)
    assert "routes-answer-501/api-stats.md" not in files
    assert "settings, served" in files["routes-answer-501/api-config.md"]
    assert "routes-answer-501/api-pair.md" in files
    assert files["mcp-surface/mcp-live.md"].endswith("| MCP-LIVE | kept | kept | closed |\n")
    assert files["mcp-surface/binding/bind-b.md"].endswith("| BIND-B | d | e | f |\n")
    sd.load_dir(root)


def test_an_entry_both_sides_changed_is_written_with_conflict_markers(tmp_path):
    root = converted(tmp_path)
    base = sd.parse_ledger(BASE)
    entry = root / "mcp-surface/mcp-live.md"
    entry.write_text(entry.read_text(encoding="utf-8").replace("B6", "B7"), encoding="utf-8", newline="\n")
    ours = BASE.replace("| MCP-LIVE | kept | dropped | B6 |", "| MCP-LIVE | kept | dropped | B8 |")
    conflicts = sd.convert(sd.parse_ledger(ours), base, root)
    assert conflicts == ["divergences/mcp-surface/mcp-live.md"]
    text = entry.read_text(encoding="utf-8")
    assert text.startswith("<<<<<<< this branch\n") and "B8" in text and "B7" in text
    with pytest.raises(sd.LedgerError, match="conflict markers"):
        sd.load_dir(root)


def test_converting_with_no_base_over_the_ledger_is_refused(tmp_path):
    # A rebase, a cherry-pick or a run before merging master has no merge
    # base: converting would put the branch's stale rows over master's.
    root = converted(tmp_path)
    files = snapshot(root)
    ours = BASE.replace("| `/api/config` | settings | W2-D |", "| `/api/config` | old text | W2-D |")
    with pytest.raises(sd.LedgerError, match="no merge base is known"):
        sd.convert(sd.parse_ledger(ours), None, root)
    assert snapshot(root) == files


def run_main(monkeypatch, tmp_path, ledger_text, argv, base=None):
    ledger = tmp_path / "divergences.md"
    ledger.write_text(ledger_text, encoding="utf-8", newline="\n")
    monkeypatch.setattr(sd, "LEDGER", ledger)
    monkeypatch.setattr(sd, "ROOT_DIR", tmp_path / "divergences")
    monkeypatch.setattr(sd, "merge_base", lambda: base)
    return sd.main(argv)


def test_the_script_outside_a_merge_refuses_and_names_the_merge(monkeypatch, tmp_path, capsys):
    assert run_main(monkeypatch, tmp_path, BASE, []) == 1
    assert "run this during `git merge`" in capsys.readouterr().err
    assert not (tmp_path / "divergences").exists()


def test_the_one_time_full_conversion_writes_the_pointer(monkeypatch, tmp_path):
    assert run_main(monkeypatch, tmp_path, BASE, ["--full"]) == 0
    assert (tmp_path / "divergences.md").read_text(encoding="utf-8") == sd.STUB
    assert sd.load_dir(tmp_path / "divergences") == sd.parse_ledger(BASE)
    assert run_main(monkeypatch, tmp_path, sd.STUB, []) == 0      # the pointer: nothing to do


def test_a_heading_reworded_past_its_name_is_an_edit_not_a_removal(tmp_path):
    long = "## Routes whose handlers belong to later slices (answer 501)"
    base_text = BASE.replace("## Routes (answer 501)", long)
    root = converted(tmp_path, base_text)
    ours = base_text.replace("(answer 501)", "(answer 501, narrowed)")
    ours = ours.replace("| settings | W2-D |", "| settings | B2d |")
    assert sd.convert(sd.parse_ledger(ours), sd.parse_ledger(base_text), root) == []
    section = root / "routes-whose-handlers-belong-to"
    assert sorted(p.name for p in section.iterdir()) == [
        "_section.md", "a-body-error-x-y.md", "api-config.md", "api-stats.md"]
    assert "narrowed" in (section / "_section.md").read_text(encoding="utf-8")
    assert "B2d" in (section / "api-config.md").read_text(encoding="utf-8")


def test_a_section_master_removed_and_the_branch_edited_is_a_conflict(tmp_path):
    root = converted(tmp_path)
    for path in sorted((root / "mcp-surface/binding").iterdir()):
        path.unlink()
    (root / "mcp-surface/binding").rmdir()
    ours = BASE.replace("| BIND-A | a | b | c |", "| BIND-A | a | b | changed |")
    conflicts = sd.convert(sd.parse_ledger(ours), sd.parse_ledger(BASE), root)
    assert "divergences/mcp-surface/binding/bind-a.md" in conflicts
    assert "divergences/mcp-surface/binding/_section.md" in conflicts


def test_a_ledger_with_conflict_markers_is_refused_with_the_fix():
    merged = "<<<<<<< HEAD\n" + BASE + "=======\n" + sd.STUB + ">>>>>>> origin/master\n"
    with pytest.raises(sd.LedgerError, match="git checkout --ours"):
        sd.parse_ledger(merged)


def test_conflict_markers_in_a_section_file_fail_the_check(tmp_path):
    root = converted(tmp_path)
    marker = root / "mcp-surface/_section.md"
    marker.write_text(marker.read_text(encoding="utf-8") + "\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> x\n",
                      encoding="utf-8", newline="\n")
    with pytest.raises(sd.LedgerError, match="conflict markers"):
        sd.load_dir(root)


@pytest.mark.parametrize("name, text, error", [
    ("api-stats.md", "| Surface | Python behaviour | Owner |\n|---|---|---|\n| `/api/stats` | x | y |\n"
                     "| `/api/more` | x | y |\n", "exactly one row"),
    ("wrong-name.md", "| Surface | Python behaviour | Owner |\n|---|---|---|\n| `/api/stats` | x | y |\n",
     "belongs in api-stats.md"),
    ("api-stats.md", "| `/api/stats` | x | y |\n", "malformed table"),
    ("note.md", "A paragraph with no label.\n", "exactly one row"),
])
def test_a_malformed_entry_file_is_refused_with_its_fix(tmp_path, name, text, error):
    root = converted(tmp_path)
    (root / "routes-answer-501" / name).write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(sd.LedgerError, match=error):
        sd.load_dir(root)


def test_a_name_is_the_opening_words_so_editing_the_rest_keeps_the_file():
    long = "Every Console route in `harness/goldens/routes.json` other than `GET /api/search`"
    name = sd.slug(long)
    assert name == "every-console-route-in-harness-goldens" and len(name) <= sd.ENTRY_LIMIT
    assert sd.slug(long + ", `GET /api/trace`") == name
    assert sd.slug("MCP-LIVE") == "mcp-live"


def test_editing_a_rows_later_words_on_a_branch_edits_its_file_in_place(tmp_path):
    # The hot row: each pull request that serves a route edits its first cell.
    row = "| Every Console route in `routes.json` other than `GET /api/search` | the answer | W2-D |"
    base = BASE.replace("| `/api/stats` | the handler's answer | W2-D |", row)
    root = converted(tmp_path, base)
    ours = base.replace("`GET /api/search` |", "`GET /api/search`, `GET /api/trace` |")
    assert ours != base
    conflicts = sd.convert(sd.parse_ledger(ours), sd.parse_ledger(base), root)
    assert conflicts == []
    assert sorted(p.name for p in (root / "routes-answer-501").iterdir()) == [
        "_section.md", "a-body-error-x-y.md", "api-config.md", "every-console-route-in-routes-json-other.md"]
    text = (root / "routes-answer-501/every-console-route-in-routes-json-other.md").read_text(encoding="utf-8")
    assert "`GET /api/trace`" in text


def test_two_entries_one_name_would_hold_are_refused_with_the_fix():
    twice = BASE.replace("| BIND-A | a | b | c |\n", "| BIND-A | a | b | c |\n| bind a | d | e | f |\n")
    with pytest.raises(sd.LedgerError, match="two entries would be bind-a.md"):
        sd.parse_ledger(twice)
