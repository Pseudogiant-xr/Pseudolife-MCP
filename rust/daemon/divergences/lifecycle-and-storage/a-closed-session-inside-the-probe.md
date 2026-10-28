| Item | Python | Rust | Owner |
|---|---|---|---|
| A closed session inside the probe interval | psycopg learns the session died only from a failed call, so for up to 1 s after the last good probe calls skip the probe and fail on the dead session | tokio-postgres marks the client closed when its connection ends, so the next `_ensure_init` reconnects at once | declared (B4): Rust reconnects earlier; the harness waits past the interval so both arms probe |
