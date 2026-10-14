Here are the answers. All paths are under `<repo>\pseudolife_memory\`.

**Exit codes**
- **Free is 0 and held is 1:** see `lease_cli.py:1482` (`return 1 if held else 0`), the docstring at `lease_cli.py:1424`, and the help text at `lease_cli.py:1798`.
- **The check itself fails:** it returns `EXIT_SOFTWARE` = 70 (`lease_cli.py:100`, `1428-1431`). The message says "neither held nor free is known", so a gate that treats 1 as "held" never refuses because of a crash.

**--json top-level keys**
- The keys are `name`, `held`, `local` and `board` (`lease_cli.py:1480`).
- `board` contains `available`, `reason`, `holder`, `expected_end`, `stale`, `queued` and `queue` (`lease_cli.py:1436-1437`).

**Board unreachable and local lock free**
- The answer is **free, exit 0**.
- If `_connect` fails or the call raises, `board["available"]` stays False, `holder` stays None, and the failure text goes into `board["reason"]` (`lease_cli.py:1438-1459`).
- `held = local["state"] == "held" or (holder is not None and not board_stale)` (`lease_cli.py:1478`), so a free local lock with no board holder comes out as free.

**Delegate lease prefix and grant length**
- The prefix is `delegate:`, which gives the lease name `delegate:<project>` (`storage/coordination.py:210`).
- It is reserved together with `designated:` (`storage/coordination.py:211`), so agents can't acquire it. Only the operator or maintainer grants it.
- A grant lasts at least 60 seconds (`DELEGATION_MIN`) and at most 7 days, 604800 seconds (`DELEGATION_MAX`) (`storage/coordination.py:221-225`). The CLI default is a day.
- The limits are enforced in `grant_delegate` at `storage/coordination.py:2359-2360`; anything outside them raises `invalid_ttl`.

**Audit event that proves a grant**
- The event is `lease_delegate`. It is appended at `storage/coordination.py:2384-2388` with payload `name`, `fence`, `hold` and `replaced`, and with the actor (operator or maintainer).
- The authority check counts the live `delegate:<project>` hold only if a matching `lease_delegate` audit row exists (`storage/coordination.py:2794-2819`, query at 2812). A plain lease with that name proves nothing.
- If `audit_retention_days` is under 7, the audit window can end the authority before the grant expires (`storage/coordination.py:2801-2802`).
