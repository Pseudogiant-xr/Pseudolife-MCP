The rules live in `<repo>\tests\suite_lock.py`, and `tests\conftest.py` calls them.

**Lock file path.** The lock directory is `~/.pseudolife-mcp/locks`, or whatever `PSEUDOLIFE_SUITE_LOCK_DIR` points to (`lock_dir`, suite_lock.py:270-274; constants at 151, 121-122). The lock file is `full-suite.lock` (suite_lock.py:158). If slots are raised, slot 0 keeps that name and slot k uses `full-suite.k.lock` (suite_lock.py:234-241). On this host the file is `<home>\.pseudolife-mcp\locks\full-suite.lock`.

**Queue.** The directory is `full-suite.queue/`, next to the lock (suite_lock.py:164, 26). Each waiter creates a ticket named `{time.time_ns():020d}-{pid}`, from wall-clock arrival time and pid, and holds an OS lock on it while it waits (suite_lock.py:449-460). Tickets sort by the key (arrival ns, pid) (suite_lock.py:436-446, 513). Only a waiter with no live earlier ticket may try the lock (suite_lock.py:26-28, 508-516). Free slots go out in arrival order (suite_lock.py:98, 591). Arrivals within the same clock tick go in pid order (suite_lock.py:452-455).

**Fail or skip.** The variable is `PSEUDOLIFE_SUITE_LOCK` (suite_lock.py:150, 258-267):
- `wait` is the default.
- `fail` exits with a usage error instead of queueing, even when the lock is free but an earlier waiter is still pending (suite_lock.py:84-86).
- `off` skips the lock (suite_lock.py:87). If the variable is unset and `GITHUB_ACTIONS=true`, the mode is `off` (suite_lock.py:263).

**Slot count.** `PSEUDOLIFE_SUITE_SLOTS` sets it; otherwise the `full-suite.slots` file in the lock directory; otherwise 1. The value must be 1 to 8 (suite_lock.py:92-101, 152, 160, 163, 277-295). That file doesn't exist on this host, so the count is 1.

**Windows refusal.** The file is `full-suite.windows` in the lock directory, and `PSEUDOLIFE_SUITE_WINDOWS` overrides it. Values are `allow` (default) or `refuse` (suite_lock.py:102-113, 154, 161, 324-345). On this host the file contains `refuse`. conftest.py:409-420 calls `native_windows_refusal`. The reason given: Git Bash process bursts stalled mouse input, so this host runs full suites in WSL (`ops/wsl-suite.ps1`). A WSL run can't see a Windows run's lock, so Windows full runs are refused outright.

**CUDA_VISIBLE_DEVICES.** conftest.py:40 calls `suite_lock.hide_cuda(os.environ)` before anything imports torch. That sets `CUDA_VISIBLE_DEVICES="-1"` unless `PSEUDOLIFE_TEST_CUDA=1` (suite_lock.py:244-255, 123-124, 156). It uses `-1` rather than an empty string because of a test on 2026-09-23 with torch 2.10+cu126. An empty value left `torch.cuda.is_available()` True, since the driver treats empty as unset, while `device_count()` reported 0 (suite_lock.py:247-250). The GPU is hidden because every pytest loads the embedder on CUDA when it can see one. A GPU run alongside two suites had hit 143 CUDA OOMs (suite_lock.py:11-12, 17).
