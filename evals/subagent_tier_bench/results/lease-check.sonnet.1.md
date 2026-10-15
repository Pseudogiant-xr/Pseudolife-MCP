All references are in `pseudolife_memory/`.

- **Exit codes** (`lease_cli.py`): 0 means free and 1 means held (`_check`, 1423-1425; returns at 1482 and 1501). If the check itself crashes, it exits `EXIT_SOFTWARE` = 70 (1431, constant at 100). The point is that a gate reading 1 as "held" must not refuse on a crash.

- **`--json` keys** (`lease_cli.py:1480`): the top-level keys are `name`, `held`, `local` and `board`. The `board` object has `available`, `reason`, `holder`, `expected_end`, `stale`, `queued` and `queue` (1436-1437).

- **Board unreachable, local lock free** (`lease_cli.py:1434-1501`):
  - `board["available"]` stays False and `board["reason"]` holds the failure text (1459).
  - `holder` stays None, so `held` is `local["state"] == "held" or (holder is not None and not board_stale)` (1478), which is False.
  - The command therefore answers "free" with exit 0, and the text output adds `board unavailable: <reason>` (1499).

- **Delegate lease name and duration**:
  - **Prefix:** the lease is named `delegate:<project>`. `DELEGATE_PREFIX = "delegate:"` is at `storage/coordination.py:210`, and `grant_delegate` builds the name at 2358.
  - **Reserved prefixes:** `delegate:` and the older `designated:` are reserved, so agents cannot acquire them (211, 2019, 2111).
  - **Duration:** `DELEGATION_MIN = 60` and `DELEGATION_MAX = 7 * 86400` (224-225), so a grant lasts 60 seconds to 7 days. `grant_delegate` rejects anything else with `invalid_ttl` (2359).
  - **CLI option:** the `--for` option accepts 60 seconds to 7 days, with a default of 1 day (`lease_cli.py:1838-1840`).

- **Audit event that proves a grant**: `lease_delegate`, appended in `grant_delegate` (`coordination.py:2384-2388`). Its payload has `name`, `fence`, `hold` and `replaced`, and its actor is the operator (or the maintainer, via the Console route). The delegate-authority check relies on it: it queries `coordination_events` for `event='lease_delegate'` (around 2812). A plain `delegate:<project>` lease without that event proves nothing (2794-2797).
