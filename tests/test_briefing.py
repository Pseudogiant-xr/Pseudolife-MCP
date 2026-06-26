from pseudolife_memory.memory.briefing import select_lessons, format_briefing


def test_select_lessons_prioritizes_avoid_then_recent():
    entries = [
        {"lesson": "use offline env", "polarity": "+", "outcome": "success"},
        {"lesson": "do not down -v", "polarity": "-", "outcome": "failure"},
        {"lesson": "correction here", "polarity": "+", "outcome": "correction"},
        {"lesson": "prefer X", "polarity": "+", "outcome": "success"},
    ]
    picked = select_lessons(entries, max_lessons=3)
    # the two avoid/correction lessons come first
    assert picked[0]["lesson"] == "do not down -v"
    assert picked[1]["lesson"] == "correction here"
    assert len(picked) == 3


def test_format_briefing_renders_both_sections_ascii():
    md = format_briefing(
        surprises=[{"src": "a", "dst": "b", "relation": "uses", "why": "bridge"}],
        questions=[{"question": "what runs where?"}],
        lessons=[{"lesson": "do not down -v", "polarity": "-", "outcome": "failure"},
                 {"lesson": "prefer offline", "polarity": "+", "outcome": "success"}],
    )
    assert "## What your memory is unsure about" in md
    assert "`a` uses `b`" in md and "what runs where?" in md
    assert "## Lessons from past work" in md
    assert "avoid: do not down -v" in md and "prefer: prefer offline" in md
    assert md.isascii()


def test_format_briefing_empty_when_nothing():
    assert format_briefing([], [], []) == ""


def test_session_briefing_cold_bank_is_unavailable(tmp_path):
    from pseudolife_memory.service import MemoryService
    svc = MemoryService(data_dir=str(tmp_path))   # file mode, no graph/lessons
    out = svc.session_briefing()
    assert out["available"] is False
    assert out["markdown"] == ""
    assert out["unsure"] == {"surprises": [], "questions": []}
    assert out["lessons"] == []
