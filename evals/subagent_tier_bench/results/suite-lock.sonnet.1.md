All references are to `tests/suite_lock.py` unless noted.

- **Lock file:** `full-suite.lock` (`LOCK_FILE`, line 158) sits in `~/.pseudolife-mcp/locks` (`lock_dir`, lines 270-274). `PSEUDOLIFE_SUITE_LOCK_DIR` overrides the directory (line 151). Slot k above 0 uses `full-suite.k.lock` (`_slot_file`, lines 234-241). The holder record is `full-suite.holder.json`.

- **Queue:** `full-suite.queue/` (`QUEUE_DIR`, line 164) is created beside the lock (`_enqueue`, line 449). Each waiter writes a `.ticket` file named `{arrival_ns:020d}-{pid}.ticket` (line 458). Order is arrival time, with pid breaking ties (`key`, line 436). `_queued_ahead` (line 509) lists the earlier tickets. The docstring (lines 26-35) says a stopped first-in-line run holds up the queue and that liveness is judged by the ticket's lock, not the pid.

- **Wait or exit:** `PSEUDOLIFE_SUITE_LOCK` (`LOCK_ENV`, line 150; `lock_mode`, line 258).
  - `wait` is the default. It queues and names the holders about once a minute.
  - `fail` exits with a usage error instead of waiting, even if the lock is free but an earlier waiter hasn't taken it (lines 85-87, 628).
  - `off` skips the lock. It is also the default when `GITHUB_ACTIONS=true`.

- **Slot count:** `PSEUDOLIFE_SUITE_SLOTS`, else the `full-suite.slots` file in the lock directory, else 1 (`slot_count`, lines 277-296). The value must be a whole number from 1 to 8 (`MAX_SLOTS`, line 163). Queued runs re-read it on every poll. No slots file exists on this host, so it is 1.

- **Windows refusal:** `full-suite.windows` in the lock directory (`WINDOWS_FILE`, line 161). Its content is `refuse`, and I confirmed that file exists in `~/.pseudolife-mcp/locks`. `PSEUDOLIFE_SUITE_WINDOWS` (line 154) overrides it, with values `allow` or `refuse` (`windows_policy`, lines 324-342). `native_windows_refusal` (line 345) returns the refusal only for a full run on `os.name == "nt"`. It tells you to run `pwsh ops/wsl-suite.ps1`. `tests/conftest.py:407-422` calls it first in the session setup. Setting `PSEUDOLIFE_SUITE_WINDOWS=allow` overrides the file.

- **CUDA:** `tests/conftest.py:40` calls `suite_lock.hide_cuda(os.environ)`. That function (lines 244-255) sets `CUDA_VISIBLE_DEVICES` to `"-1"` unless `PSEUDOLIFE_TEST_CUDA=1`, in which case it leaves the variable alone. It never uses an empty string. On the Windows host with torch 2.10+cu126, an empty value left `torch.cuda.is_available()` True, because the driver reads it as unset. `device_count()` still reported 0.
