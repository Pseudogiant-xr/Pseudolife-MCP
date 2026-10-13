### Added (2026-10-13 — Native store and shared episode ownership)

- The experimental Rust daemon implements memory storage, surprise admission,
label normalization, slot extraction and contradiction detection. Store-driven
session creation and handle resume share the background reaper's episode,
activity and tombstone state, so an idle session created by a store can close
without a stale cache later undoing that close.

- Admitted stores finish before graceful shutdown even when their callers
disconnect. Store session durations accept underscore-separated numbers and
preserve invalid-setting errors instead of silently reopening old sessions.

- Canonical fact writes, outcomes, standalone episode/title APIs and tensor
persistence remain deferred. This does not enable the Rust daemon for cutover.
