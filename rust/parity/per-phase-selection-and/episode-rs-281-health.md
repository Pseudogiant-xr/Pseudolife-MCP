| Episode site | Producer/input class | Substitution or retained reason |
|---|---|---|
| `episode.rs:281` health | Daemon `_build_health_payload`, `web/api.py:418` JSON response | Narrow to UTF-8 ordinary serde_json parsing/non-null health gate, with no Python non-finite, integer-env or UTF16/32 byte-sniffing emulation. Configured remote malformed responses still refuse. |
