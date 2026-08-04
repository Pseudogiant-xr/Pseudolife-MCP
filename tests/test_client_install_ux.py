"""Claude and Codex installer UX guards."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_one_shot_installers_support_both_clients() -> None:
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        assert "claude" in text
        assert "codex" in text
        assert "both" in text
        assert "codex mcp add" in text
        assert "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json" in text
        assert "PSEUDOLIFE_WRITER_ID" in text


def test_installers_wire_codex_via_shim_by_default() -> None:
    """Shim transport applies to BOTH clients (2026-07-19): a Codex session
    spawns its own shim process and gets tier-1 per-session identity instead
    of inheriting the Claude hook's machine-scoped tier-3 pointer
    (configuration.md#session-identity, cross-client paragraph). The HTTP
    one-liner stays as the no-shim-tooling fallback."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        assert "codex mcp add pseudolife-memory -- pseudolife-mcp" in text
        assert "codex mcp add pseudolife-memory --url" in text


def test_compose_writer_default_is_client_neutral() -> None:
    compose = _read("ops/docker-compose.yml")
    assert "PSEUDOLIFE_WRITER_ID: ${PSEUDOLIFE_WRITER_ID:-mcp-client}" in compose


def test_hook_installers_support_codex_hook_store() -> None:
    ps = _read("ops/install-hook.ps1")
    sh = _read("ops/install-hook.sh")
    for text in (ps, sh):
        assert ".codex" in text
        assert "hooks.json" in text
        assert "AGENTS.md" in text


def test_installers_offer_dreamer_model_choice() -> None:
    """Claude-shim installs prompt for the dreamer model (2026-08-04): all
    four current Anthropic tiers are offered, Opus is the recommended
    default (dreamer-choice-verdict.json), and the choice reaches the
    autostart script instead of being hardcoded there."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        for model in ("claude-opus-5", "claude-sonnet-5",
                      "claude-haiku-4-5", "claude-fable-5"):
            assert model in text, f"missing model option: {model}"
    assert "-Model $Model" in ps          # choice forwarded to autostart
    assert '--model "$MODEL"' in sh


def test_shim_autostart_scripts_accept_model_and_run_live_shim() -> None:
    ps = _read("ops/install-shim-autostart.ps1")
    sh = _read("ops/install-shim-autostart.sh")
    # Opus stays the non-interactive default (measured winner).
    assert 'Model = "claude-opus-5"' in ps
    assert 'MODEL="claude-opus-5"' in sh
    assert "--model" in sh
    # The Linux unit must launch the shim that exists: evals/claude_shim.py
    # (sonnet_shim.py was renamed; a unit pointing at it fails at start).
    assert "claude_shim.py" in sh
    assert "sonnet_shim.py" not in sh
    assert "sonnet_shim.py" not in _read("ops/install.sh")


def test_preflight_checks_the_selected_client_only() -> None:
    ps = _read("ops/preflight.ps1")
    sh = _read("ops/preflight.sh")
    for text in (ps, sh):
        assert "claude" in text
        assert "codex" in text
        assert "both" in text
