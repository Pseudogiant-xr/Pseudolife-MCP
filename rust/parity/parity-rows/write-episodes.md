| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| WRITE-EPISODES | 4 | Daemon-owned session handles, pointers/tombstones, nested episodes, reap/resume/title stamps, isolated caller identity and inferred outcomes. `episodes.py`, `writer_context.py`, `service.py`, `web/session_hook.py` | `test_session_identity.py`, `test_episodes.py`, `test_episode_service.py`, `test_session_title.py`, `test_outcome_inference.py` I/mixed; add two-client restart/reap corpus | deferred |
