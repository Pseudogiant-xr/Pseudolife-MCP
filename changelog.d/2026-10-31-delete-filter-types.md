### Fixed (2026-10-31 — Reject malformed deletion filters)

- Reject non-string text, substring, source, episode and tag filters before matching or deleting memories in Python and Rust, including confirmed bulk deletion. An invalid tag can no longer silently widen a deletion to the whole bank; the error names the filter and asks for a string or its omission.
