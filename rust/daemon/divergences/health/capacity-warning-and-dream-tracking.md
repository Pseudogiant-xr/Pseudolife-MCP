| Key | Python | Rust | Owner |
|---|---|---|---|
| `capacity_warning` and `dream_tracking_error` while the clock reseed keeps failing | read from the stores once they load, before the late reseed | absent until the reseed succeeds, because the ready service reaches `/health` only then | declared (B4): `/health` is `ready: false` in that window either way |
