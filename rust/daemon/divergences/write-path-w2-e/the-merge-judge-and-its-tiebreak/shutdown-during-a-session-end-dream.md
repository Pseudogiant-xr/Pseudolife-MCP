| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Shutdown during a session-end dream | its daemon thread dies with the process | it runs as an admitted write task, like a manual `run`, which shutdown drains: shutdown waits for it until the end of the 9 s exit budget (the shutdown deadline row) | a dream that began finishes within the exit budget, else it is cut at the exit as Python's thread is; the same holds for a manual `run` | no committed state differs from a Python process killed at the same point (an open transaction rolls back with its connection) |
