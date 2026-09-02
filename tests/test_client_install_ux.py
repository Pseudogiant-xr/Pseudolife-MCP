"""Claude and Codex installer UX guards."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _marker_block(text: str, name: str) -> list[str]:
    """Lines strictly between ``# >>> <name> >>>`` and ``# <<< <name> <<<``."""
    lines = text.splitlines()
    return lines[lines.index(f"# >>> {name} >>>") + 1:
                 lines.index(f"# <<< {name} <<<")]


def _heredoc_payload(block: list[str]) -> list[str]:
    """The literal payload inside a marker block: a bash quoted heredoc
    (``cat <<'TAG'`` ... ``TAG``) or a PowerShell literal here-string
    (``@'`` ... ``'@``). Both terminators must sit at column 0."""
    for i, line in enumerate(block):
        stripped = line.strip()
        if stripped.endswith("@'"):
            term = "'@"
        elif "<<'" in stripped:
            term = stripped.split("<<'", 1)[1].split("'", 1)[0]
        else:
            continue
        for j in range(i + 1, len(block)):
            if block[j].rstrip() == term:
                return block[i + 1:j]
    raise AssertionError("no heredoc/here-string payload in marker block")


def test_one_shot_installers_support_both_clients() -> None:
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        assert "claude" in text
        assert "codex" in text
        assert "both" in text
        assert "gemini" in text
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
    shadow the real bank with a stale one. Every provider, both platforms:
    fresh registrations carry the guard through the probed env flag, the
    generic snippet embeds it, and pre-existing registrations get an
    upgrade warning with paste-ready commands."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        # Upgrade commands for pre-existing registrations (paste-ready).
        # NB flag order: claude's --env is variadic — placed before the
        # server name it swallows it and the whole registration fails
        # (verified against the live CLI 2026-08-29). Name first, then
        # --env, then the `--` separator.
        assert ("claude mcp add --scope user pseudolife-memory "
                "--env PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp") in text
        assert ("codex mcp add pseudolife-memory "
                "--env PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp") in text
        # Gemini registrations and the generic snippet carry the guard too.
        assert ("gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini "
                "-e PSEUDOLIFE_MCP_NO_SPAWN=1 pseudolife-memory pseudolife-mcp") in text
        assert '"PSEUDOLIFE_MCP_NO_SPAWN": "1"' in text
    # Fresh registrations set the guard through the probed env flag — the
    # spellings differ per script (the .sh quotes its variable).
    assert ('claude mcp add --scope user pseudolife-memory "$env_flag" '
            "PSEUDOLIFE_WRITER_ID=claude-code PSEUDOLIFE_MCP_NO_SPAWN=1 "
            "-- pseudolife-mcp") in sh
    assert ('codex mcp add pseudolife-memory "$env_flag" '
            'PSEUDOLIFE_WRITER_ID=codex "$env_flag" '
            "PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp") in sh
    assert ("claude mcp add --scope user pseudolife-memory $envFlag "
            "PSEUDOLIFE_WRITER_ID=claude-code PSEUDOLIFE_MCP_NO_SPAWN=1 "
            "-- pseudolife-mcp") in ps
    assert ("codex mcp add pseudolife-memory $envFlag "
            "PSEUDOLIFE_WRITER_ID=codex $envFlag "
            "PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp") in ps


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


def test_installers_offer_codex_extractor_modes() -> None:
    """A ChatGPT-plan adopter gets the same one-shot path a Max-plan user has
    (2026-08-31): codex-only / codex-fallback extractor modes with a GPT-5.6
    dreamer-model prompt. Terra is the non-interactive default (the shim's
    own default; nothing is quality-measured yet, and the menus must say so
    rather than borrow the Claude modes' 'recommended')."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        for needle in ("codex-only", "codex-fallback", "gpt-5.6-sol",
                       "gpt-5.6-terra", "gpt-5.6-luna", "unmeasured"):
            assert needle in text, f"missing: {needle}"
    # A model from the wrong family must be rejected up front, not passed
    # through to a shim that would silently serve its launch default.
    assert "does not match extractor mode" in ps
    assert "does not match extractor mode" in sh


def test_codex_shim_autostart_scripts_mirror_the_claude_pair() -> None:
    """The Codex autostart twins launch codex_shim.py on :8086 with the
    gpt-5.6-terra default and NO prompt-file override (the v2 extraction
    prompt is Sonnet-tuned; the codex shim runs the production prompt until
    a --rung terra ladder run measures a variant), and they raise the
    health-probe TTL — each /health refresh is a real CLI call, which is
    metered spend on a free ChatGPT tier."""
    ps = _read("ops/install-codex-shim-autostart.ps1")
    sh = _read("ops/install-codex-shim-autostart.sh")
    assert 'Model = "gpt-5.6-terra"' in ps
    assert 'MODEL="gpt-5.6-terra"' in sh
    assert "8086" in ps and "8086" in sh
    for text in (ps, sh):
        assert "codex_shim.py" in text
        assert "system-prompt-file" not in text
        assert "health-ttl" in text.lower().replace("healthttl", "health-ttl")
    # Windows: the official installer keeps codex.exe in a rotating
    # %LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\ dir off PATH — the script must
    # know that layout to fail fast with a useful message, and must NOT bake
    # the hash path into the task (the shim re-resolves at every start).
    assert "OpenAI\\Codex\\bin" in ps
    # Linux: systemd user units get a minimal PATH — pin the CLI via --cli.
    assert "command -v codex" in sh
    assert "--cli" in sh


def test_installer_env_block_covers_codex_modes() -> None:
    """codex-fallback / codex-only write the same env-triple shapes as the
    sonnet pair (fallback => auto + sidecar pair; only => primary), and the
    installer-managed override marker keeps recognizing files written by
    pre-codex installs (legacy '(sonnet-only)' text) while writing the
    generalized marker."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        assert "managed override (shim-only extractor)" in text
        assert "managed override (sonnet-only)" in text     # legacy accepted
    # codex modes reach the autostart stage with the codex script, not the
    # claude one.
    assert "install-codex-shim-autostart.ps1" in ps
    assert "install-codex-shim-autostart.sh" in sh


def test_mode_switch_tears_down_the_sibling_shim_autostart() -> None:
    """Re-running with a different -Extractor is the documented way to switch
    modes, so a cross-family switch (codex -> sonnet, any -> sidecar) must
    remove the other family's autostart — an abandoned codex task keeps
    burning real ChatGPT-tier CLI calls at every /health refresh, forever,
    on a machine whose owner believes it is turned off (2026-08-31 review
    finding)."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    assert '"Pseudolife Codex Shim"' in ps
    assert '"Pseudolife Claude Shim"' in ps
    assert '"Pseudolife Sonnet Shim"' in ps       # pre-rename installs too
    assert "pseudolife-codex-shim.service" in sh
    assert "pseudolife-sonnet-shim.service" in sh


def test_shim_modes_fail_fast_on_a_missing_cli() -> None:
    """The shim family's CLI is checked right after the extractor choice,
    BEFORE volumes/env/compose — preflight only knows -Client, so
    `-Extractor codex-fallback -Client claude` used to sail through
    preflight and die at stage 8 with the stack already up (2026-08-31
    review finding; symmetric fix for the claude modes)."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    for text in (ps, sh):
        assert "needed by extractor mode" in text


def test_preflight_codex_check_knows_the_official_installer_layout() -> None:
    """Get-Command codex misses the official Windows installer entirely
    (codex.exe lives in %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\, off
    PATH — verified live 2026-08-31), so preflight would FAIL a machine with
    a working Codex. The check must accept that layout too."""
    ps = _read("ops/preflight.ps1")
    assert "OpenAI\\Codex\\bin" in ps


def test_preflight_checks_the_selected_client_only() -> None:
    ps = _read("ops/preflight.ps1")
    sh = _read("ops/preflight.sh")
    for text in (ps, sh):
        assert "claude" in text
        assert "codex" in text
        assert "both" in text
        assert "gemini" in text
        assert "generic" in text


def test_installers_accept_multi_provider_client_lists() -> None:
    """--client / -Client take a comma- or space-separated provider list
    (claude codex gemini generic), with `both` and `all` kept as aliases so
    every documented invocation keeps working."""
    sh = _read("ops/install.sh")
    ps = _read("ops/install.ps1")
    for text in (sh, ps):
        assert "gemini" in text
        assert "generic" in text
    assert 'both) expanded="$expanded claude codex"' in sh
    assert "tr ',' ' '" in sh
    assert '"both" { $expanded += @("claude", "codex") }' in ps
    assert "-split '[,\\s]+'" in ps


def test_preflight_accepts_provider_lists_and_checks_gemini() -> None:
    """Preflight takes the same provider-list grammar as the installers and
    probes the gemini CLI (with its install hint) when selected. The .ps1
    must not keep a 3-value ValidateSet on -Client — install.ps1 passing
    "claude,gemini" would die at the parameter binder."""
    sh = _read("ops/preflight.sh")
    ps = _read("ops/preflight.ps1")
    for text in (sh, ps):
        assert "gemini" in text
        assert "npm install -g @google/gemini-cli" in text
    assert 'both) CHECKS="$CHECKS claude codex"' in sh
    assert '[ValidateSet("claude", "codex", "both")]' not in ps


def test_preflight_checks_pipx_on_both_platforms() -> None:
    """pipx is the preferred shim installer on every platform; only the .sh
    preflight used to mention it (the PEP 668 rationale is Linux-specific but
    the recommendation is not)."""
    assert "pipx" in _read("ops/preflight.sh")
    assert "pipx" in _read("ops/preflight.ps1")


def test_installers_wire_gemini_via_shim_and_http() -> None:
    """Gemini CLI is first-class: stdio shim with a per-provider writer id
    (user scope — gemini defaults to project scope) and an HTTP fallback.
    Flag spellings verified live against gemini CLI 0.57.0 (2026-08-29)."""
    sh = _read("ops/install.sh")
    ps = _read("ops/install.ps1")
    for text in (sh, ps):
        assert "gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 pseudolife-memory pseudolife-mcp" in text
        assert "gemini mcp add -s user -t http pseudolife-memory http://127.0.0.1:8765/mcp" in text
        assert "gemini mcp list" in text  # idempotency: there is no `gemini mcp get`


def test_installers_pass_writer_id_on_registration_with_a_flagless_fallback() -> None:
    """Per-provider writer ids ride each shim registration's env (the shim
    forwards PSEUDOLIFE_WRITER_ID as X-PL-Writer). Env-flag support is
    probed, never assumed: the flagless forms must survive verbatim as the
    fallback, or a CLI without the flag turns into a failed install."""
    sh = _read("ops/install.sh")
    ps = _read("ops/install.ps1")
    for text in (sh, ps):
        for writer in ("PSEUDOLIFE_WRITER_ID=claude-code",
                       "PSEUDOLIFE_WRITER_ID=codex",
                       "PSEUDOLIFE_WRITER_ID=gemini"):
            assert writer in text, writer
        # The probed-flag pattern and its flagless fallbacks.
        assert "mcp add --help" in text
        assert "claude mcp add --scope user pseudolife-memory -- pseudolife-mcp" in text
        assert "codex mcp add pseudolife-memory -- pseudolife-mcp" in text


def test_installers_skip_codex_hook_on_windows() -> None:
    """Codex hooks are not available on Windows, so install.ps1 must gate the
    Codex hook install on the OS and say what replaces it (the standing
    AGENTS.md block). The .sh installer never runs on Windows and carries no
    such gate."""
    ps = _read("ops/install.ps1")
    sh = _read("ops/install.sh")
    assert "$IsWindows" in ps
    assert "the standing AGENTS.md block is the briefing there" in ps
    assert "$IsWindows" not in sh


def test_hook_installer_explains_experimental_codex_opt_in() -> None:
    """Codex hooks are off by default: writing hooks.json is not enough, the
    user must also opt in via [features] codex_hooks = true in config.toml
    (and then trust the hook — test_codex_hook_install_explains_required_
    trust_review pins that part)."""
    for rel in ("ops/install-hook.sh", "ops/install-hook.ps1", "README.md"):
        text = _read(rel)
        assert "codex_hooks = true" in text, rel
        assert "[features]" in text, rel


def test_generic_provider_prints_pasteable_mcp_config() -> None:
    """The generic path prints ready-to-paste mcpServers config (stdio shim
    and HTTP shapes), synced across both installers, and only ever appends
    the standing block with consent (a prompted or flag-passed path)."""
    sh_block = "\n".join(_heredoc_payload(
        _marker_block(_read("ops/install.sh"), "generic-snippets")))
    ps_block = "\n".join(_heredoc_payload(
        _marker_block(_read("ops/install.ps1"), "generic-snippets")))
    assert sh_block == ps_block
    assert '"mcpServers"' in sh_block
    assert '"pseudolife-memory"' in sh_block
    assert "http://127.0.0.1:8765/mcp" in sh_block
    for rel in ("ops/install.sh", "ops/install.ps1"):
        assert "Append the standing memory block to which file?" in _read(rel), rel
    # Idempotency: the presence check lives inside the single append choke
    # point — the generic prompt resolves its target path only after the
    # loop-top check already ran against an empty --agents-file, so a
    # re-run would otherwise double-append (review finding, 2026-08-29).
    sh_append = _read("ops/install.sh").split("append_block() {", 1)[1].split("\n}", 1)[0]
    ps_append = _read("ops/install.ps1").split("function Add-MemoryBlock(", 1)[1].split("\n}", 1)[0]
    assert "pseudolife-memory" in sh_append
    assert "pseudolife-memory" in ps_append


def test_installers_report_a_per_provider_wiring_ladder() -> None:
    """The run ends with a per-provider ladder: what got wired ([x]), what
    was deliberately skipped ([-]), what is unavailable ([!]) — including
    the universal MCP instructions field, so hook-less providers see what
    they still get."""
    for rel in ("ops/install.sh", "ops/install.ps1"):
        text = _read(rel)
        assert "MCP transport" in text, rel
        assert "[x] Server instructions" in text, rel
        assert "Session briefing" in text, rel
        assert "Per-turn discipline" in text, rel
        assert "[!]" in text, rel
    # A failed registration must not render as wired (the .ps1 computes the
    # transport marker from the exit-checked state; the .sh aborts loudly).
    assert "Get-McpMarker" in _read("ops/install.ps1")
    assert '"failed"' in _read("ops/install.ps1")


def test_providers_guide_matches_installer_matrix() -> None:
    """docs/guide/providers.md must agree with the installer's capability
    matrix on the provider set and the writer ids (loose agreement — the
    markdown table is formatted differently on purpose)."""
    guide = _read("docs/guide/providers.md")
    for label in ("Claude Code", "OpenAI Codex", "Gemini CLI"):
        assert label in guide, f"providers guide missing: {label}"
    for writer in ("claude-code", "codex", "gemini", "mcp-client"):
        assert writer in guide, f"providers guide missing writer id: {writer}"
    assert "codex_hooks = true" in guide
    assert "@AGENTS.md" in guide


def test_update_scripts_carry_the_shared_header_style() -> None:
    """The deploy scripts get the installers' colored step styling (gated on
    NO_COLOR / TTY, literal `==>` prefix kept for log greps) but no banner —
    their output is tee'd into deploy logs."""
    sh = _read("ops/update.sh")
    ps = _read("ops/update.ps1")
    for text in (sh, ps):
        assert "NO_COLOR" in text
        assert "==>" in text
        assert "\x1b" not in text
        assert "# >>> banner >>>" not in text
    assert "step()" in sh
    assert "function Step" in ps


def test_installer_ps1_parses() -> None:
    """The literal here-strings require their closing '@ at column 0 — a
    parse failure there is invisible to text-only guards. Skipped where
    pwsh is unavailable."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not available")
    for rel in ("ops/install.ps1", "ops/preflight.ps1", "ops/install-hook.ps1"):
        path = str(ROOT / rel).replace("'", "''")
        script = ("$e=$null;"
                  "[System.Management.Automation.Language.Parser]::ParseFile("
                  f"'{path}',[ref]$null,[ref]$e)|Out-Null;"
                  "if($e){exit 1}")
        proc = subprocess.run([pwsh, "-NoProfile", "-Command", script],
                              capture_output=True)
        assert proc.returncode == 0, f"{rel} failed to parse"


def test_installer_banner_is_synced_and_pure_ascii() -> None:
    """The ASCII banner is duplicated across install.sh and install.ps1 and
    must stay byte-identical (marker-block sync, like the discipline line).
    Pure printable ASCII only: the tracked-tree control-byte guard bans raw
    ESC, and Unicode box drawing renders as mojibake on legacy consoles."""
    sh = _heredoc_payload(_marker_block(_read("ops/install.sh"), "banner"))
    ps = _heredoc_payload(_marker_block(_read("ops/install.ps1"), "banner"))
    assert sh == ps
    assert len(sh) >= 8
    assert "P S E U D O" in "\n".join(sh)
    for line in sh:
        assert len(line) <= 78, f"banner line over 78 cols: {line!r}"
        assert all(0x20 <= ord(c) <= 0x7E for c in line), \
            f"non-ASCII in banner line: {line!r}"


def test_installers_suppress_art_when_not_a_tty_or_no_color() -> None:
    """Art and color are interactive sugar only: both scripts honour
    NO_COLOR and an opt-out flag, the bash side gates on a real TTY, and
    color arrives via escape sequences built at runtime — never a literal
    ESC byte in the file (tracked-tree control-byte guard)."""
    sh = _read("ops/install.sh")
    ps = _read("ops/install.ps1")
    for text in (sh, ps):
        assert "NO_COLOR" in text
        assert "\x1b" not in text
    assert "[ -t 1 ]" in sh
    assert "--no-art" in sh
    assert "$NoArt" in ps


def test_capability_matrix_is_synced_across_installers() -> None:
    """The provider capability matrix shown at selection time is duplicated
    across install.sh and install.ps1 and must stay byte-identical."""
    sh = _heredoc_payload(
        _marker_block(_read("ops/install.sh"), "capability-matrix"))
    ps = _heredoc_payload(
        _marker_block(_read("ops/install.ps1"), "capability-matrix"))
    assert sh == ps
    joined = "\n".join(sh)
    for label in ("Claude Code", "OpenAI Codex", "Gemini CLI", "Other agent"):
        assert label in joined, f"matrix missing row: {label}"
    assert "instructions" in joined  # the universal MCP-field lever
    for line in sh:
        assert len(line) <= 78, f"matrix line over 78 cols: {line!r}"
        assert all(0x20 <= ord(c) <= 0x7E for c in line), \
            f"non-ASCII in matrix line: {line!r}"


def test_capability_matrix_states_codex_hook_limits() -> None:
    """The matrix must be honest about Codex hooks: experimental opt-in via
    config.toml, and unavailable on Windows (there the standing AGENTS.md
    block IS the briefing — which is why append is recommended)."""
    joined = "\n".join(_heredoc_payload(
        _marker_block(_read("ops/install.sh"), "capability-matrix")))
    assert "codex_hooks = true" in joined
    assert "NOT available on Windows" in joined


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


def test_elevated_autostart_steps_warn_against_elevating_inside_claude_desktop() -> None:
    """Task Scheduler refuses per-user logon-task registration from an
    unelevated administrator account (probed 2026-09-02: fresh task, Limited
    principal, root folder and a subfolder all return Access is denied), so
    the Windows autostart installers legitimately need an elevated
    PowerShell. WHERE that elevation is requested matters: a UAC prompt
    raised from a shell running inside Claude Desktop (or any Store-packaged
    app such as Store-installed pwsh 7) leaves Windows' Application
    Information service holding a handle to that app's container job, and
    the app's next update then fails to launch ("Another program is
    currently using this file") until a reboot — anthropics/claude-code
    #61635, reproduced live on the maintainer's box 2026-09-02, most likely
    triggered by the codex-shim autostart installer having been elevated
    from a Desktop session on 2026-08-31 (inferred from timing).
    Every place that tells the user to elevate must say to open the elevated
    PowerShell fresh from the Start menu instead."""
    for rel in ("ops/install-shim-autostart.ps1",
                "ops/install-codex-shim-autostart.ps1",
                "docs/guide/dreaming.md"):
        text = _read(rel)
        assert "inside Claude Desktop" in text, f"{rel}: missing the caveat"
        assert "claude-code#61635" in text, f"{rel}: missing the issue reference"
    # The one-shot installer only points at the autostart scripts, but its
    # own retry hint is where a user actually copies the command from.
    assert "inside Claude Desktop" in _read("ops/install.ps1")
