| Item | Remaining behavior | Owner / reason |
|---|---|---|
| Episode titles on idle close | `memory_episode_start`, `memory_episode_end`, `memory_session_title` and `memory_episode_summary` are served (spec EP1-EP12) on the state store-driven open/resume and empty-root reaping share; automatic content titles on idle close now share explicit close's title derivation and resident/durable entry stamps | closed (B4): live restart and malformed-resume scenarios |
