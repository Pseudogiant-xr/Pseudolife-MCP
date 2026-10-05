"""The session names a harness already shows, read from its own files.

Claude Code writes a session's shown title into its transcript as
``custom-title`` lines (the Desktop app's titles and renames) and
``ai-title`` lines (the CLI's generated title), repeated through the
session, latest wins. Codex appends ``{"id", "thread_name"}`` lines to
``session_index.jsonl``, latest line per id wins. Measured 2026-10-02 on
the maintainer's host: transcripts of 23-53 MB with a ``custom-title``
roughly every 30 lines, the last within ~30 lines of the end; one 45 MB
transcript with no title at all. The readers must stay cheap on such files:
the tail first, then bounded incremental reads, never the whole file per
heartbeat.
"""
from __future__ import annotations

import json
import uuid

from pseudolife_memory.harness_names import (
    ClaudeSessionTitle, CodexThreadNames, clean_name,
)

SESSION = "8d1a64d9-1cdc-4a1f-9bee-9f175d2d5a64"


def _line(**fields) -> str:
    return json.dumps(fields) + "\n"


def custom(title, session=SESSION):
    return _line(type="custom-title", customTitle=title, sessionId=session)


def ai(title, session=SESSION):
    return _line(type="ai-title", aiTitle=title, sessionId=session)


def agent_name(title, session=SESSION):
    return _line(type="agent-name", agentName=title, sessionId=session)


def chatter(n=1, size=200):
    return "".join(_line(type="user", message={"content": "x" * size}, uuid=str(i))
                   for i in range(n))


def transcript(config, text, *, project="C--work-repo", session=SESSION, mode="w"):
    path = config / "projects" / project / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return path


def append(path, text):
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def reader(config, **kwargs):
    kwargs.setdefault("glob_interval", 0)
    return ClaudeSessionTitle(SESSION, config_dir=config, **kwargs)


def test_latest_custom_title_wins_and_a_rename_is_seen(tmp_path):
    path = transcript(tmp_path, chatter(3) + custom("first") + chatter(2) + custom("second"))
    titles = reader(tmp_path)
    assert titles.poll() == "second"
    append(path, chatter(1) + custom("renamed"))
    assert titles.poll() == "renamed"


def test_ai_title_is_the_fallback_and_a_custom_title_beats_it(tmp_path):
    path = transcript(tmp_path, chatter(2) + ai("generated") + chatter(1))
    titles = reader(tmp_path)
    assert titles.poll() == "generated"
    append(path, custom("named") + ai("regenerated"))
    assert titles.poll() == "named"


def test_agent_name_ranks_between_a_custom_title_and_a_generated_one(tmp_path):
    # Claude Code 2.1.287 (checked 2026-10-05) also writes ``agent-name``
    # lines, the name a session goes by in the Desktop app's session list.
    path = transcript(tmp_path, ai("generated") + chatter(1) + agent_name("named agent"))
    titles = reader(tmp_path)
    assert titles.poll() == "named agent"
    append(path, ai("regenerated"))
    assert titles.poll() == "named agent"
    append(path, custom("custom") + agent_name("later agent name"))
    assert titles.poll() == "custom"


def test_an_agent_name_in_the_older_part_is_found_by_backfill(tmp_path):
    transcript(tmp_path, agent_name("old name") + chatter(40, size=400))
    titles = reader(tmp_path, max_read=8192, tail=4096)
    seen = None
    for _ in range(20):
        seen = titles.poll()
        if seen is not None:
            break
    assert seen == "old name"


def test_no_title_and_no_transcript_read_as_none(tmp_path):
    assert reader(tmp_path).poll() is None
    transcript(tmp_path, chatter(5))
    assert reader(tmp_path).poll() is None


def test_a_transcript_that_appears_later_is_found(tmp_path):
    titles = reader(tmp_path)
    assert titles.poll() is None
    transcript(tmp_path, custom("late"))
    assert titles.poll() == "late"


def test_an_unchanged_file_is_not_read_again(tmp_path):
    transcript(tmp_path, chatter(5) + custom("steady"))
    titles = reader(tmp_path)
    assert titles.poll() == "steady"
    assert titles.bytes_read > 0
    assert titles.poll() == "steady"
    assert titles.bytes_read == 0


def test_a_title_at_the_end_of_a_huge_file_is_found_from_the_tail(tmp_path):
    """The Desktop case: the newest title sits near the end, so the first
    poll reads only the tail and never backfills the rest."""
    transcript(tmp_path, chatter(2000, size=500) + custom("found fast") + chatter(3))
    titles = reader(tmp_path, max_read=64 * 1024, tail=16 * 1024)
    assert titles.poll() == "found fast"
    assert titles.bytes_read <= 16 * 1024
    assert titles.poll() == "found fast"
    assert titles.bytes_read == 0


def test_a_title_far_from_the_tail_is_found_by_bounded_backfill(tmp_path):
    budget = 64 * 1024
    path = transcript(tmp_path, chatter(1000, size=500) + custom("old name")
                      + chatter(1000, size=500))
    assert path.stat().st_size > 10 * budget
    titles = reader(tmp_path, max_read=budget, tail=16 * 1024)
    seen = []
    for _ in range(100):
        seen.append(titles.poll())
        assert titles.bytes_read <= budget
        if seen[-1] is not None:
            break
    assert seen[-1] == "old name"
    assert len(seen) > 5          # several bounded polls, not one whole read


def test_a_backfilled_custom_title_beats_an_ai_title_from_the_tail(tmp_path):
    transcript(tmp_path, custom("chosen") + chatter(400, size=500) + ai("generated"))
    titles = reader(tmp_path, max_read=64 * 1024, tail=16 * 1024)
    results = [titles.poll() for _ in range(40)]
    assert results[0] == "generated"
    assert results[-1] == "chosen"


def test_backfill_finds_the_newest_older_title_first_without_churn(tmp_path):
    """Read backwards, the first custom title met is the newest one before
    the tail, so an older one never shows on the board on the way."""
    transcript(tmp_path, custom("oldest") + chatter(300, size=500) + custom("older")
               + chatter(300, size=500) + ai("generated"))
    titles = reader(tmp_path, max_read=64 * 1024, tail=16 * 1024)
    results = [titles.poll() for _ in range(40)]
    assert "oldest" not in results
    assert results[-1] == "older"


def test_a_line_longer_than_the_budget_is_skipped(tmp_path):
    giant = _line(type="assistant", message={"content": "y" * 300_000})
    transcript(tmp_path, giant + custom("after the giant"))
    titles = reader(tmp_path, max_read=64 * 1024, tail=16 * 1024)
    results = [titles.poll() for _ in range(20)]
    assert results[-1] == "after the giant"


def test_backfill_steps_over_a_line_longer_than_the_budget(tmp_path):
    giant = _line(type="assistant", message={"content": "y" * 300_000})
    transcript(tmp_path, custom("before the giant") + giant + chatter(100, size=500)
               + ai("generated"))
    titles = reader(tmp_path, max_read=64 * 1024, tail=16 * 1024)
    results = [titles.poll() for _ in range(30)]
    assert all(titles is not None for titles in results)
    assert results[-1] == "before the giant"


def test_backfill_keeps_a_title_that_straddles_a_chunk_after_a_giant_line(tmp_path):
    # Review of 2026-10-05: stepping back over a line longer than the budget,
    # a short title line that crossed the next chunk's start was taken for
    # part of the giant line and lost. Chunks are counted back from the
    # end, so sweep the giant's length: some layout puts a chunk boundary
    # inside the title line.
    lost = []
    for pad in range(0, 8192, 31):
        root = tmp_path / str(pad)
        giant = _line(type="assistant", message={"content": "y" * (40_000 + pad)})
        transcript(root, chatter(3) + custom("straddling") + giant + chatter(12, size=500)
                   + ai("generated"))
        titles = reader(root, max_read=8192, tail=4096)
        seen = [titles.poll() for _ in range(40)][-1]
        if seen != "straddling":
            lost.append((pad, seen))
    assert lost == []


def test_the_off_switch_reads_the_environment():
    from pseudolife_memory.harness_names import harness_names_enabled
    assert harness_names_enabled({}) is True
    for on in ("1", "true", "yes", "on", ""):
        assert harness_names_enabled({"PSEUDOLIFE_BOARD_HARNESS_NAMES": on}) is True, on
    for off in ("0", "false", "No", " OFF "):
        assert harness_names_enabled({"PSEUDOLIFE_BOARD_HARNESS_NAMES": off}) is False, off


def test_malformed_lines_are_ignored(tmp_path):
    text = ('{"type":"custom-title","customTitle":\n'           # cut short
            + _line(type="custom-title", customTitle=42)          # not a string
            + '[1, "custom-title"]\n'                             # not an object
            + _line(type="user", text='{"type":"custom-title","customTitle":"fake"}')
            + custom("real") + "not json at all custom-title\n")
    transcript(tmp_path, text)
    assert reader(tmp_path).poll() == "real"


def test_a_truncated_or_replaced_transcript_is_read_afresh(tmp_path):
    path = transcript(tmp_path, chatter(50) + custom("before"))
    titles = reader(tmp_path)
    assert titles.poll() == "before"
    transcript(tmp_path, custom("after"))
    assert path.stat().st_size < 1000
    assert titles.poll() == "after"


def test_a_partial_last_line_waits_for_its_newline(tmp_path):
    path = transcript(tmp_path, chatter(2))
    titles = reader(tmp_path)
    assert titles.poll() is None
    line = custom("complete")
    append(path, line[:20])
    assert titles.poll() is None
    append(path, line[20:])
    assert titles.poll() == "complete"


def test_claude_config_dir_overrides_the_home_directory(tmp_path, monkeypatch):
    home, config = tmp_path / "home", tmp_path / "config"
    transcript(home / ".claude", custom("from home"))
    transcript(config, custom("from config"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    assert ClaudeSessionTitle(SESSION, glob_interval=0).poll() == "from config"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert ClaudeSessionTitle(SESSION, glob_interval=0).poll() == "from home"


def test_a_session_id_that_is_not_an_id_reads_nothing(tmp_path):
    transcript(tmp_path, custom("x"), session="star")
    assert ClaudeSessionTitle("*", config_dir=tmp_path, glob_interval=0).poll() is None
    assert ClaudeSessionTitle("", config_dir=tmp_path, glob_interval=0).poll() is None
    assert ClaudeSessionTitle(None, config_dir=tmp_path, glob_interval=0).poll() is None


def test_an_io_error_fails_closed(tmp_path, monkeypatch):
    transcript(tmp_path, custom("x"))
    titles = reader(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr("builtins.open", broken)
    assert titles.poll() is None


def test_names_are_cleaned_and_bounded():
    assert clean_name("  two\twords\n ") == "two words"
    assert clean_name("a‮b") == "a b"
    assert clean_name("x" * 500) == "x" * 120
    assert clean_name("   ") is None
    assert clean_name(None) is None
    assert clean_name(7) is None


# --- Codex --------------------------------------------------------------------

def index(home, text, mode="w"):
    path = home / "session_index.jsonl"
    home.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return path


def entry(thread, name):
    return _line(id=thread, thread_name=name, updated_at="2026-10-02T00:00:00Z")


def test_codex_latest_line_per_thread_wins(tmp_path):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    index(tmp_path, entry(a, "first") + entry(b, "other") + entry(a, "renamed"))
    names = CodexThreadNames(codex_home=tmp_path)
    assert names.name(a) == "renamed"
    assert names.name(b) == "other"
    assert names.name(str(uuid.uuid4())) is None
    index(tmp_path, entry(b, "other renamed"), mode="a")
    assert names.name(b) == "other renamed"
    assert names.name(a) == "renamed"


def test_codex_unchanged_index_is_not_read_again(tmp_path):
    a = str(uuid.uuid4())
    index(tmp_path, entry(a, "steady"))
    names = CodexThreadNames(codex_home=tmp_path)
    assert names.name(a) == "steady"
    assert names.name(a) == "steady"
    assert names.bytes_read == 0


def test_codex_malformed_and_truncated_index(tmp_path):
    a = str(uuid.uuid4())
    index(tmp_path, '{"id": 1, "thread_name": "x"}\n{"id":"' + a + '","thread_name":\n'
          + entry(a, "good") + "garbage thread_name\n")
    names = CodexThreadNames(codex_home=tmp_path)
    assert names.name(a) == "good"
    index(tmp_path, entry(a, "new"))
    assert names.name(a) == "new"


def test_codex_index_is_read_in_bounded_steps(tmp_path):
    threads = [str(uuid.uuid4()) for _ in range(3000)]
    index(tmp_path, "".join(entry(t, f"thread {i}") for i, t in enumerate(threads)))
    names = CodexThreadNames(codex_home=tmp_path, max_read=32 * 1024)
    results = []
    for _ in range(50):
        results.append(names.name(threads[-1]))
        assert names.bytes_read <= 32 * 1024
        if results[-1] is not None:
            break
    assert results[-1] == "thread 2999"
    assert len(results) > 3


def test_codex_home_overrides_the_home_directory(tmp_path, monkeypatch):
    a = str(uuid.uuid4())
    home = tmp_path / "home"
    index(home / ".codex", entry(a, "from home"))
    index(tmp_path / "codex", entry(a, "from codex home"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert CodexThreadNames().name(a) == "from codex home"
    monkeypatch.delenv("CODEX_HOME")
    assert CodexThreadNames().name(a) == "from home"


def test_codex_missing_index_reads_none(tmp_path):
    assert CodexThreadNames(codex_home=tmp_path).name(str(uuid.uuid4())) is None
