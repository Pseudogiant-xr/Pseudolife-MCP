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


def test_fetch_markdown_parses_api_response(monkeypatch):
    from pseudolife_memory import briefing_cli as bc

    class _Resp:
        def __init__(self, body): self._b = body.encode("utf-8")
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=5: _Resp('{"markdown": "## hi\\n- x", "available": true}'))
    assert bc._fetch_markdown("http://x", None, 3, 3) == "## hi\n- x"


def test_briefing_no_daemon_prints_nothing(monkeypatch, capsys):
    import sys
    from pseudolife_memory import briefing_cli as bc
    monkeypatch.setattr("pseudolife_memory.shim.probe_health",
                        lambda url, timeout=0.25: None)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "briefing"])
    bc.run_briefing()                       # must not raise, must not print
    assert capsys.readouterr().out == ""


def test_hook_json_wraps_markdown_as_sessionstart_context():
    import json
    from pseudolife_memory import briefing_cli as bc
    d = json.loads(bc._as_hook_json("## hi\n- x"))
    assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert d["hookSpecificOutput"]["additionalContext"] == "## hi\n- x"


def test_hook_json_empty_is_empty_string():
    from pseudolife_memory import briefing_cli as bc
    assert bc._as_hook_json("") == ""
    assert bc._as_hook_json("   \n  ") == ""
