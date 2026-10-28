| Item | Python | Rust | Owner |
|---|---|---|---|
| The SessionEnd hook's close firing the session-end dream | the hook closes the session and fires the dream | the entry point is served (`Service::fire_session_dream`, as the reaper's idle close uses it); the hook's close answers 501 until the hook batch serves it | the hook batch (B3) |
