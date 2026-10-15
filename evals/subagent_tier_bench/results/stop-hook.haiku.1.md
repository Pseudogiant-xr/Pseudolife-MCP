**Stop hook wake: how it waits and wakes the session**

Registration is in `plugin/hooks/hooks.json:70-80`. The Stop entry runs `stop-wake.sh` with `async: true`, `asyncRewake: true` and `timeout: 1209600`.

- **Poll interval:** 5 s (`stop-wake.sh:102`, `POLL=5`).
- **Slower idle interval:** 30 s after `FAST_WAIT=3540` s (`stop-wake.sh:108-109`). The loop switches at line 644.
- **Maximum wait:** `MAX_WAIT=1209540` s, about 14 days (`stop-wake.sh:96`), just under the hooks.json timeout. `PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT` can shorten it but not lengthen it (lines 470-474). If no Claude Code PID is known, the wait falls back to `FAST_WAIT` (line 538).
- **Wake cap:** at most `MAX_WAKES=20` wakes in any `WAKE_WINDOW=3600` s (lines 115-116). `wake_budget_ok` (lines 581-602) counts entries in `$KEY.wakes` inside the window. It is checked at line 638. Mail over the cap waits; it is not dropped.
- **Digest and markers** (paths set at lines 212-218, all under `~/.pseudolife-mcp/digests` unless `PSEUDOLIFE_DIGEST_DIR` is set):
  - `$KEY.txt` is the digest. Line 1 is its watermark, the rest is the body.
  - `$KEY.seen` is the shown-up-to marker. The watcher only fires when the watermark is greater than it (line 637), and `fire()` raises it (lines 659-662).
  - `$KEY.ring` is the ring marker. Line 1 is the watermark the ring is for, line 2 the daemon's reason. `ring_past_seen` (lines 564-577) requires the reason to start with `rung ` and the ring to be newer than `.seen`.
  - `$KEY.wake` is the lease. `$KEY.wakes` is the wake-time ledger. `$KEY.agent` and `$KEY.turn` feed the park gate.
- **What wakes the session:** `fire()` (lines 649-672) prints "Pseudolife board mail woke this session…" plus the digest body to fd 3, which Claude Code shows to the model as a stop-hook message. It then marks `.seen`, records the wake, appends to the ledger, posts the woke marker, and exits **2** (line 671). Exit 2 on an async rewake starts a new turn even when idle. Any other return exits 0 (line 677). The park gate also exits 2 with the daemon's block message (line 462), but only when the turn is not itself a stop-hook continuation and the daemon answers `block`.
- **One watcher per session:** each firing writes a fresh random TOKEN into `$KEY.wake` (lines 476-479). `still_owner` compares it on every poll (lines 480-484, 619). An older watcher finds a different token and returns 3, which exits quietly at line 677. The wait also ends on HUP/INT/TERM, when the Claude Code parent PID disappears (lines 620, 540-551), or when the lease is lost. A companion `$KEY.<token>.wake-armed` file, renewed every 60 s (`LISTENER_LEASE`, lines 492-502), advertises the listener as armed.
- **Session key:** `KEY` is the SHA-256 of the session id (line 188). After `/clear`, a `claude-<pid>.host` record can redirect it (lines 198-210).
