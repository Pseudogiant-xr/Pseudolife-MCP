"""PSEUDOLIFE_HOOK_PROBE_MS: the test-facing budget of the /health probe.

Hooks probe the daemon before doing anything and stay silent when the probe
misses, so the default budget stays 250 ms for users. The CLI parity harness
sets the variable to the same larger value in both arms, because a cold
hosted runner can stall one arm's first connection past 250 ms. The Rust
port parses it with the same rules (``rust/shim/src/lifecycle.rs``).
"""

from __future__ import annotations

import pytest

import pseudolife_memory.briefing_cli as briefing_cli
import pseudolife_memory.episode_cli as episode_cli
import pseudolife_memory.shim as shim

VAR = "PSEUDOLIFE_HOOK_PROBE_MS"


class _Reply:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"status": "ok"}'


@pytest.fixture
def seen(monkeypatch):
    """The timeouts ``probe_health`` handed to urlopen."""
    timeouts: list[float] = []

    def fake_urlopen(url, timeout=None):
        assert url.endswith("/health")
        timeouts.append(timeout)
        return _Reply()

    monkeypatch.setattr(shim.urllib.request, "urlopen", fake_urlopen)
    return timeouts


def test_unset_keeps_the_250_ms_default(monkeypatch, seen):
    monkeypatch.delenv(VAR, raising=False)
    assert shim.probe_health("http://127.0.0.1:9") == {"status": "ok"}
    assert seen == [0.25]
    assert shim.hook_probe_timeout() == 0.25


def test_the_variable_widens_the_default_probe(monkeypatch, seen):
    monkeypatch.setenv(VAR, "1500")
    shim.probe_health("http://127.0.0.1:9")
    assert seen == [1.5]


@pytest.mark.parametrize("raw, expected", [
    (" 1500\t", 1.5),       # surrounding ASCII whitespace is ignored
    ("\r\n800\n", 0.8),
    ("0001500", 1.5),       # leading zeros are digits like any other
    ("1", 0.001),
    ("60000", 60.0),
    ("60001", 60.0),        # clamped to 60 s
    pytest.param("9" * 5000, 60.0, id="5000-digits"),  # however long
])
def test_accepted_values(monkeypatch, raw, expected):
    monkeypatch.setenv(VAR, raw)
    assert shim.hook_probe_timeout() == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "0", "000", "-5", "+5", "1.5", "1e3", "1_500", "abc", "1500ms",
    "١٥٠٠",  # Arabic-Indic digits: int() takes them, the rule does not
    " 1500",                 # non-ASCII whitespace is not stripped
    "\x0b1500",                   # nor vertical tab (outside both ports' strip set)
])
def test_invalid_zero_or_negative_falls_back_to_250_ms(monkeypatch, raw):
    monkeypatch.setenv(VAR, raw)
    assert shim.hook_probe_timeout() == 0.25


def test_an_explicit_timeout_wins_over_the_variable(monkeypatch, seen):
    monkeypatch.setenv(VAR, "1500")
    shim.probe_health("http://127.0.0.1:9", timeout=2.0)
    shim.probe_health("http://127.0.0.1:9", 0.5)
    assert seen == [2.0, 0.5]


def test_the_briefing_hook_probe_follows_the_variable(monkeypatch, seen):
    monkeypatch.setenv(VAR, "4000")
    monkeypatch.setattr(shim, "_daemon_url", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(briefing_cli.sys, "argv", ["pseudolife-mcp", "briefing"])
    monkeypatch.setattr(briefing_cli, "_fetch_markdown", lambda *a, **kw: "")
    briefing_cli.run_briefing()
    assert seen == [4.0]


def test_the_episode_hook_probe_follows_the_variable(monkeypatch, seen):
    monkeypatch.setenv(VAR, "4000")
    monkeypatch.setattr(episode_cli, "_daemon_url", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(episode_cli, "_post", lambda *a, **kw: None)
    episode_cli.run_episode("episode-start", stdin_text='{"session_id":"abc","cwd":"/x"}')
    assert seen == [4.0]
