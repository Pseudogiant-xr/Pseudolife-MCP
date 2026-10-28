| Item | Python | Rust | Owner |
|---|---|---|---|
| Storage calls while a reconnect cannot reach the server | refused for 5 s with the cached `reconnect failed: <error>` | each fails with the dead client's own error (`connection closed`) | declared (B4): same retry window and outcome, different error text |
