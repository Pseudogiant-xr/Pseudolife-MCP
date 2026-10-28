| Item | Python | Rust | Owner |
|---|---|---|---|
| Future or malformed schema version | refuses future versions and non-positive/non-integer version metadata before DDL, preserving all durable state (since 2026-10-23; it used to overwrite the stored version with the build's) | the same, with the same message | closed: no divergence; `run.py`'s `stamped` scenario requires both refusals and an unchanged bank on each side |
