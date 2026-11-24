### Fixed (2026-11-24 — Codex hook launchers name a step an installed client has) [#1609]
- The Codex hook launchers (`run.sh`, `run.ps1`) now say "rerun the installer and approve the Codex hooks" when their bundle pointer is broken, instead of the checkout-only `python ops/setup-codex-hooks.py`.
- No hook re-approval is expected: Codex approves the command that names a launcher, and that command and every hook definition (`plugin/hooks/hooks.json`, the manual hook commands) are unchanged; Codex does not hash the launcher's bytes.
- An installer rerun and `pseudolife-mcp update` replace the launchers every earlier release shipped (their sha256 is in `PREVIOUS_LAUNCHERS`); `update` without a refresh reports them as behind, and a launcher no release shipped is still refused as modified.
