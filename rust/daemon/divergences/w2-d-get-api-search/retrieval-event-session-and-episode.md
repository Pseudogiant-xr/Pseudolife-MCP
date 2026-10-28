| Item | Python | Rust | Why / owner |
|---|---|---|---|
| Retrieval-event session and episode | resolved from the resident active-session pointer and episode tree | read from `meta.active_session_pointer` and `episodes` per search (same values) | W2-E owns the resident resolver; the call sites switch when it lands |
