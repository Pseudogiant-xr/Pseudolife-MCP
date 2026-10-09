# Background session contract

Oracle: the PR merge-base Python source. Canonical inputs are daemon
configuration and session episodes written by the shipped hook/API producers.

| Contract | Python source |
|---|---|
| Durability starts once: changed-state exit save, autosave, warmup | `mcp_server.py:3165-3219` |
| Reaper starts once; sleeps before each tick; errors do not stop later ticks | `mcp_server.py:3274-3301` |
| Open keyed roots expire on the newest subtree entry or root touch | `service.py:6561-6610` |
| Close cascades, records client-session idle reason, triggers one dream per tick with nonempty closes | `service.py:6469-6530,6599-6610` |
| Empty idle closes remain resumable; only deferred roots may be swept | `service.py:6612-6671` |
| Sweep records tombstones, expires them by handle window, retains newest 200 | `service.py:6673-6702` |
| Deferred roots and tombstones hydrate across restart | `service.py:1315-1330` |

Intervals and clock-dependent decisions are injectable. Lifecycle evidence
compares ordered events and database state after each milestone; close clocks
must lie in each arm's captured window. Source-derived values, episode IDs,
descendant membership, metadata and client-session records remain exact.
Log wording and scheduling latency are free; neither is a performance claim.

Integration boundaries: W2-E owns episode opening/resume and all entry/fact
writes. Background scheduling calls a durability interface; it does not
implement those writes. W3-H owns dream execution; the session-end trigger
uses an interface with a declared no-op until that implementation lands.
File-mode tensors and embedded Postgres remain deferred by name.

The session daemon fixture replaces Python's `_fire_and_forget_dream` with
a counted no-op, matching the declared native trigger stub. It does not
normalize away dream cursor or acknowledgement writes. Its controlled
normal-return shutdown installs a returning prior signal handler because
uvicorn rethrows captured signals after stopping; this exercises atexit
and daemon restart, not the ordinary CLI's uninstrumented signal exit code.
Cascade children must share the root's close time before clock projection.
