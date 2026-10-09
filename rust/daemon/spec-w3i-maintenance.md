# Background maintenance contract

Canonical producer: shipped daemon configuration, PyPI's `info.version`
string, and PostgreSQL rows written by the Python service.

| Exact behavior | Python source |
|---|---|
| Release check starts once only when configured and environment switch is not `0`; fetch occurs outside requests | `release_check.py:124-137` |
| Check immediately, retry failed first answer at min(interval, 900); retain the last good version | `release_check.py:80-95,117-121` |
| Accept only 1–32 ASCII version characters and a leading dotted integer version | `release_check.py:39,63-77` |
| Health exposes checker enablement/latest/checked time and unattended knobs | `daemon.py:275-287` |
| Offer only a newer version; one line naming the update command, optional differing plugin | `web/session_hook.py:150-177` |
| Heap trim sleeps first, runs every tick on glibc; nonpositive interval disables it | `utils/heap_trim.py:98-163` |
| Headroom retains the existing native cgroup/process/unavailable reader | `utils/memory_headroom.py:95-149`, `health.rs:57-132` |
| Sweep starts once when dream OR retrieval log is enabled; maintenance runs before the dream gate | `mcp_server.py:3243-3271`, `memory/dream.py:2383-2446` |
| Dream journal marks stale running rows failed then retains newest configured count, atomically | `storage/postgres.py:3282-3297` |
| Retrieval and lesson-search pruning use the same strict older-than cutoff, separate transactions under the service lock | `service.py:6041-6055`, `storage/postgres.py:2390-2399,2419-2426` |
| Compaction keeps newest non-live rows per slot, tie-breaking by asserted time and resident ordinal; current/contested survive | `memory/compaction.py:27-70` |

Clocks and durations are semantic within each arm's captured window; log
wording and phase durations are free. Version values, enabled state, retention
boundaries, row identities, cascade effects and event order are exact.
W2-E owns mutable canonical stores and durability; W3-H owns automatic dream
execution. Their integration gaps remain declared until those APIs land.
