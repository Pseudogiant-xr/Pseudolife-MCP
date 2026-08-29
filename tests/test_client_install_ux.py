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
        assert ("codex mcp add pseudolife-memory "
                "--env PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp") in text
        assert "codex mcp add pseudolife-memory --url" in text


def test_docker_tier_shim_registrations_disable_the_spawn_fallback() -> None:
    """The Docker-tier installers register every shim with
    ``PSEUDOLIFE_MCP_NO_SPAWN=1`` (2026-08-29 incident): there the real
    daemon is the compose container, and a shim-spawned host fallback can
    win the port-bind race against a still-booting Docker Desktop and
    shadow the real bank with a stale one. Both clients, both platforms."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        # NB flag order: claude's --env is variadic — placed before the
        # server name it swallows it and the whole registration fails
        # (verified against the live CLI 2026-08-29). Name first, then
        # --env, then the `--` separator.
        assert ("claude mcp add --scope user pseudolife-memory "
                "--env PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp") in text
        assert ("codex mcp add pseudolife-memory "
                "--env PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp") in text


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


def test_codex_hook_install_explains_required_trust_review() -> None:
    """Codex skips new or changed hooks until the user reviews and trusts
    their exact definition. A successful file write must not imply that the
    briefing is already active (Codex hooks security model, 2026-08-28)."""
    readme = " ".join(_read("README.md").lower().split())
    assert "review and trust" in readme
    assert "open `/hooks`" in readme
    for rel in ("ops/install-hook.ps1", "ops/install-hook.sh"):
        text = " ".join(_read(rel).lower().split())
        assert "review and trust" in text, rel
        assert "open /hooks" in text, rel


def test_codex_http_auth_uses_supported_token_configuration() -> None:
    readme = _read("README.md")
    assert 'bearer_token_env_var = "PSEUDOLIFE_MCP_TOKEN"' in readme


def test_hook_installers_wire_user_prompt_submit_for_claude() -> None:
    """Non-plugin Claude installs get the every-turn mid-session discipline
    line too (UserPromptSubmit), including the recall-before-review clause —
    the one-shot session-start briefing loses salience over a long session
    (2026-08-25 finding). Claude client only: Codex per-prompt hook support
    is unverified, and every new Codex hook needs a manual trust review
    (2026-08-28), so the installer must not silently write one there."""
    ps = _read("ops/install-hook.ps1")
    sh = _read("ops/install-hook.sh")
    for text in (ps, sh):
        assert "UserPromptSubmit" in text
        assert "reviewing code, docs, or a PR" in text
    # Pin the client gating itself — a refactor that hoists the wiring out of
    # the guard would silently write an untrusted per-prompt hook into every
    # Codex install. (Line identity + idempotency needle are pinned by
    # test_plugin_packaging.py::test_discipline_line_synced_across_plugin_and_installers.)
    assert 'if [ "$CLIENT" = claude ]' in sh
    assert 'if ($Client -eq "claude")' in ps


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


def test_install_sh_shim_failure_falls_back_instead_of_aborting() -> None:
    """A failed shim install must leave SHIM_OK unset so the HTTP fallback
    fires — not kill the run via errexit (issue #176). On PEP 668 distros
    (Ubuntu 24.04, Debian 12, Fedora 40, Arch) ``pip install --user`` exits 1
    with externally-managed-environment; a bare call under ``set -e`` aborted
    the installer after the multi-GB image build with no remediation text.
    install.ps1 already exit-checks both paths; this pins install.sh to the
    same contract."""
    sh = _read("ops/install.sh")
    # Every install command is the condition of an `if`, so errexit is
    # suspended and failure reaches the fallback branch instead of aborting.
    assert "if pipx install pseudolife-mcp; then" in sh
    assert "if pipx upgrade pseudolife-mcp; then" in sh
    assert "if python3 -m pip install --user pseudolife-mcp; then" in sh
    assert "if python -m pip install --user pseudolife-mcp; then" in sh
    # The failure-mode hint names the PEP 668 cause and the recovery paths.
    assert "externally-managed" in sh
    assert "pipx" in _read("ops/preflight.sh")
