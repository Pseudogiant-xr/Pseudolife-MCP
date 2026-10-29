| Item | Phase | Status | Rust | Evidence |
|---|---|---|---|---|
| POST /api/hook/session-end | 4 | deferred | partly | The gates, the 16 KiB cap and the no-session `{"ok": true}` are served and compared in hosted CI (HTTP-BODY-LIMITS, security cases); a truthy `session_id` gets 501 (the close, W2-E). Malformed bodies answer 400 `invalid_json` (declared, "Request parsing edges" in `daemon/divergences.md`) |
