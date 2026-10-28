| Item | Python | Rust | Owner |
|---|---|---|---|
| Writer-session probe per call (`verify_writer_session`) | probes at most once a second, skipped while a transaction pins the session (`_pinned`) | the same interval; skipped while a transaction or statement group holds the writer seam (`txn::try_hold`) or a dropped transaction is still open (`txn::recovery_pending`), and the seam's guard keeps one from starting mid-probe. A session already closed heals without the seam | declared (B4): the skip covers more than Python's pin, so a probe waits for the next call |
