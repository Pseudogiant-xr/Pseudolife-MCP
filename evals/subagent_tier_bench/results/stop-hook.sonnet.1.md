All line references are to `plugin/hooks/stop-wake.sh` unless I name another file.

**Registration.** The Stop hook in `plugin/hooks/hooks.json` (lines 72-80) is `async: true` and `asyncRewake: true`, with `timeout: 1209600`. Line 75 runs `exec bash stop-wake.sh`, and `commandWindows` runs `lifecycle.ps1 -Event Stop`. It is skipped if `PSEUDOLIFE_AGENT_WAKE_HOOK` is 0/false/no/off, or if `PSEUDOLIFE_AGENT_COORDINATION` is set to anything but a yes.

**Intervals and maximum wait**
- **Fast poll:** `POLL=5` seconds (line 101). It applies while `SECONDS < FAST_WAIT`, and `FAST_WAIT=3540` (line 106) is about one hour.
- **Idle poll:** `IDLE_POLL=30` seconds (line 107), used after `FAST_WAIT`. The sleep choice is at line 644.
- **Maximum wait:** `MAX_WAIT=1209540` seconds (line 96), which is 14 days and sits just under the hooks.json timeout of 1209600. `PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT` can shorten the wait but never lengthen it (lines 470-474). On timeout `wait_for_mail` returns 3, so the hook exits 0 without waking.

**Wake cap.** `MAX_WAKES=20` within `WAKE_WINDOW=3600` seconds, a rolling hour (lines 115-116). `wake_budget_ok` (lines 579-602) counts the epoch times in `<key>.wakes` that fall inside the window. Mail over the cap waits and is not dropped. `fire` appends the new wake time to `<key>.wakes` (line 666).

**Files read besides the digest** (`<key>.txt`, line 212; all in `$DIGEST_DIR`)
- **Ring marker:** `<key>.ring` (line 216). Line 1 is the digest watermark the ring is for. Line 2 is the daemon's decision and reason, and the reason must start with "rung " (line 575). `ring_past_seen` requires the ring's watermark to be greater than the seen marker (line 577).
- **Shown-up-to marker:** `<key>.seen` (line 213), read by `read_seen`.
- **Wake condition:** the digest's watermark (line 1 of the digest) is a number, it is greater than the seen value, the body is non-empty, the ring is past seen, and the wake budget allows another wake (lines 633-636).
- **Other files it touches:** `<key>.wake` (the lease), `<key>.wakes`, and `<key>.<token>.wake-armed`. These last two are written, not read for mail.

**Wake signal** (`fire`, lines 648-672)
- It prints to fd 3, which is the original stderr: "Pseudolife board mail woke this session (… nothing failed):" followed by the digest body (line 656).
- It raises `.seen` to the watermark, only ever upward (lines 657-661), and appends to `ledger.log`.
- It then runs `exit 2` (line 671). Claude Code shows the stderr text to the model as a "Stop hook blocking error" reminder and starts a new turn.
- A separate park gate, which I did not read in detail, also uses `exit 2` (line 462).

**One watcher per session** (lines 80-83, 126-132, 614-622)
- Each firing writes a fresh `TOKEN="$$-$RANDOM$RANDOM"` into `<key>.wake` (the `LEASE` file) through a temp file and `mv -f`.
- Every loop iteration calls `still_owner`, which compares the file's contents to its own token. An older watcher that finds a different token returns 3 and exits quietly, so the newest turn end owns the wait.
- `renew_listener` rewrites the `.wake-armed` record with a 60-second expiry (`LISTENER_LEASE=60`, line 99), and an EXIT trap removes it.
