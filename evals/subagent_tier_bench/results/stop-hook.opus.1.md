Paths are relative to `<repo>\plugin\hooks\`.

**How the hook is registered.** `hooks.json:70-79` registers it as the `Stop` hook. It runs in the background after each turn (`"async": true`, `"asyncRewake": true`) with `"timeout": 1209600` (14 days). A `bash -n` syntax check runs first, so a copy that does not parse is skipped. On Windows, `commandWindows` runs `lifecycle.ps1 -Event Stop` instead (line 76). The wake only happens in Claude Code. In Codex the script runs just the park gate and stops (`stop-wake.sh:468`).

**Poll intervals**
- `POLL=5` seconds (`stop-wake.sh:102`). The shim rewrites the digest on its 20 s heartbeat, so 5 s adds little delay.
- After `FAST_WAIT=3540` seconds (59 min), it slows to `IDLE_POLL=30` seconds (`stop-wake.sh:108-109`). The switch is at line 644.

**Maximum wait**
- `MAX_WAIT=1209540` seconds (`stop-wake.sh:96`), which is 60 s under the hooks.json timeout, so the wait ends before Claude Code kills the hook.
- `PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT` can only shorten it (lines 470-474).
- If there is no Claude Code process to watch, the wait is capped at `FAST_WAIT` (line 538).
- The loop's deadline check is at line 643.

**Wake cap**
- At most `MAX_WAKES=20` wakes in any `WAKE_WINDOW=3600` seconds, a rolling hour (`stop-wake.sh:115-116`).
- The check is `wake_budget_ok` (lines 581-602), which counts timestamps in `<key>.wakes`. Mail over the cap is delayed until the window allows it, not dropped.

**Files read beside the digest (`<key>.txt`, line 212)**
- **`<key>.seen`** (line 213) is the shown-up-to marker: one watermark number, read by `read_seen` (lines 553-558).
- **`<key>.ring`** (line 216) is the ring marker the shim writes. Line 1 is the watermark the ring is for; line 2 is the daemon's decision and reason, which must start with `rung ` (`ring_past_seen`, lines 564-577).
- The hook fires only when all of these hold (lines 637-638):
  - the digest body is non-empty;
  - the digest watermark is greater than `.seen`;
  - the ring watermark is greater than `.seen`;
  - the wake cap allows another wake.
- Other files: `.wakes` (cap timestamps), `.wake` (the lease), `.agent` and `.turn` (park gate) (lines 214-218).

**What wakes the session.** `fire()` (lines 649-672) does the following in order:
1. Prints `Pseudolife board mail woke this session (Claude Code labels this delivery a Stop hook error; nothing failed):` followed by the digest body. This goes to fd 3, which holds the original stderr (`exec 3>&2 2>/dev/null`, line 150).
2. Raises `.seen` to the digest watermark. If the marker did not advance, it returns without waking.
3. Records the wake in `.wakes` and appends a `wait` line to `ledger.log`.
4. Posts the woke marker.
5. Exits with `exit 2` (line 671).

Claude Code shows that stderr to the model as a "Stop hook blocking error" and starts a new turn. Every other path ends in `exit 0` (line 677).

**One watcher per session** (comment at lines 80-82)
- Each run writes a fresh token (`$$-$RANDOM$RANDOM`) to `<key>.wake` by writing a temp file and renaming it (lines 476-479).
- `still_owner` (lines 480-484) is checked on every poll (line 619) and again in `fire()`.
- An older watcher that finds a different token there exits quietly, so the newest turn end always owns the wait.
