| Item | Python | Rust | Owner |
|---|---|---|---|
| A reconnect that fails after winning the lease (reading or bumping the epoch) | `_reconnect_error` already cleared: `verify_writer_session` retries the reconnect once at once, then tolerates an operational failure | nothing cached; an operational failure is tolerated and the next call reconnects, any other propagates | declared (B4): one immediate retry fewer |
