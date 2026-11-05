### Fixed (2026-11-04 — Finish unattended installs without waiting for prompts)

- Both installers accept `--yes` / `-Yes`, or `PSEUDOLIFE_UNATTENDED=1` under
  an agent's pseudo-terminal, with the same conservative defaults as redirected
  stdin. Omitted clients are detected from installed CLIs, and a fresh local
  install defaults to the CPU sidecar. Guided setup that needs a person names
  its follow-up command instead of waiting for input.
