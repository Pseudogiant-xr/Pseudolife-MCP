The lock lives in `~/.pseudolife-mcp/locks` by default, and a full run takes it through `tests/suite_lock.py` and `tests/conftest.py`. Here is what each part does:

**Lock file.** `full-suite.lock` (`tests/suite_lock.py:158`), in the lock directory. The default directory is `~/.pseudolife-mcp/locks` (`:270-274`), and `PSEUDOLIFE_SUITE_LOCK_DIR` overrides it (`:151`). The lock is an OS lock, so no pid file can go stale.

**Queue.** `full-suite.queue/` beside the lock (`:164`). Each waiter creates a ticket named `{arrival_ns:020d}-{pid}.ticket` (`:458`) and holds an OS lock on it. Order is arrival time, then pid (`:436`, `:513`). A waiter tries the lock only when no live ticket is ahead of it (`:617-623`). Dead tickets are passed over, and deleted after a 30 s grace window (`:189`, `:488-506`).

**Exit instead of waiting.** `PSEUDOLIFE_SUITE_LOCK=fail` raises a usage error instead of queueing (`:84-87`, `:626-629`). Setting `PSEUDOLIFE_SUITE_LOCK=off` skips the lock entirely (`:87-88`, `:856-857`, `:889`). Unset on GitHub Actions, the default is `off`.

**Slot count.** `PSEUDOLIFE_SUITE_SLOTS` if set, otherwise the `full-suite.slots` file in the lock directory, otherwise 1 (`:92-93`, `:277-296`). Valid values are whole numbers 1 to 8 (`:163`, `:293`). That file does not exist on this host.

**Windows refusal.** The file is `full-suite.windows` in the lock directory, and it contains `refuse`. The default is `allow` (`:102-113`, `:324-342`, `:345-358`). The override is `PSEUDOLIFE_SUITE_WINDOWS=allow` (`:102-103`, `:154`). The check runs in `conftest.py:417-426` before the run queues. I confirmed the file exists here with the value `refuse`, so native full runs are refused on this machine unless the variable overrides it.

**CUDA.** `hide_cuda()` (`suite_lock.py:244-255`) sets `CUDA_VISIBLE_DEVICES=-1`, and `conftest.py:40` calls it before anything imports torch. `PSEUDOLIFE_TEST_CUDA=1` leaves the variable alone (`:123-124`, `:252-253`). The value is `-1` and not an empty string because of a measurement on 2026-09-23 (`:246-251`). With torch 2.10+cu126 on the Windows host, an empty value left `torch.cuda.is_available()` True, while `device_count()` reported 0. The docstring says the driver reads an empty value as unset, so the two checks disagreed.
