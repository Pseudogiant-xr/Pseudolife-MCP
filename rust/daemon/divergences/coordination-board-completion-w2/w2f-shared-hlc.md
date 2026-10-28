| Name | Disposition and evidence | Remaining gate |
|---|---|---|
| W2F-SHARED-HLC | Python board and memory writes use the service clock. Rust currently owns a separate board clock; the service clock now exists for fact writes (`hlc.rs`, `Ready::tick_hlc`, spec FS6) but the board has not adopted it, so board-only and mixed Python/Rust board audit checks do not establish ordering across Rust board and memory writes. | **Cutover blocker:** move the board onto `Ready::tick_hlc`, then test interleaved board/memory writes, restart reseeding and lease-epoch changes. |
