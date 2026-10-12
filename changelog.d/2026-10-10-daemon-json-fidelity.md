### Fixed (2026-10-10 — daemon JSON number parity)
- Match Python's shortest decimal spelling at binary64 ties by sharing the checked float writer with the CLI audit codec.
- Preserve exact number tokens in portable daemon golden recordings so replay detects spelling changes even when decoded float values agree.
