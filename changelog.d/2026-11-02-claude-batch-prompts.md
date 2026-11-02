### Fixed (2026-11-02 — Preserve Windows Claude batch prompts)
- Claude extractor calls through Windows npm `.cmd` and `.bat` launchers now pass system prompts through a temporary UTF-8 file, preserving multiline text and its system role instead of silently truncating it. The file is removed after success or failure; native executable invocation stays unchanged.
