**Exit codes** (`lease_cli.py`)
- Free: exit **0** (`_check_lease`, line 1501, and the `--json` path at 1482).
- Held: exit **1** (same lines).
- Check crashed: exit **70** (`EXIT_SOFTWARE`, line 100, returned from `_check` at 1431). This is not held or free.

**`--json` top-level keys** (line 1480): `name`, `held`, `local`, `board`.

**Board unreachable, local lock free:** `held` is computed at line 1478 as `local state == "held" or (holder is not None and not board_stale)`. With the board down, `board["holder"]` stays `None` (line 1436), so `held` is false. The command exits **0** and prints "lease X: free", plus "board unavailable: <reason>" (line 1499). So an outage with a free local lock reads as free. The board's state is never checked in that case.

**Delegate lease prefix:** `delegate:` (`storage/coordination.py:210`). The `<project>` suffix follows it, e.g. `delegate:<project>`. Agents cannot acquire it; it is reserved (line 211).

**Grant duration:** the CLI's `--for` defaults to 86400 s and accepts 60 s to 7 days (`lease_cli.py:1838`). The store enforces `DELEGATION_MIN = 60` and `DELEGATION_MAX = 7 * 86400` (`coordination.py:224-225`, checked at 2359).

**Audit event proving a grant:** `lease_delegate`, appended in `grant_delegate` at `coordination.py:2384`. Its payload has `name`, `fence`, `hold`, and `replaced`. The actor is `operator` (or `maintainer` via the Console). The event goes into the hash-chained `coordination_events` log.
