| Key | Python | Rust | Owner |
|---|---|---|---|
| `dream_tracking_error` before the first dream call | set at startup, when the dream's acknowledgement state is read | set at the first dream call, which is when Rust reads that state (the lazy tracking init above) | declared (B4): follows the tracking-init divergence (W3-H) |
