## Background-duty preparation

The experimental daemon now owns cancellable, idempotent durability/reaper/sweep
scheduling; an initialized session log supports cascade idle close, deferred
empty-root retention, and persisted tombstones. Release checks and glibc trim
run outside requests and service locks. Sweep pruning keeps the newest dream
journal rows, marks stale running rows failed, and deletes expired retrieval
and lesson-search rows even with dreams disabled. The native compaction
selection and update-offer formatter use live Python policy goldens.

`BACKGROUND-SESSIONS`, `BACKGROUND`, and `BACKGROUND-SWEEP` remain deferred.
Writer durability and canonical-store slot synchronization require W2-E;
session write/resume routes and generic close-time titles are not integrated;
automatic dreams and the graph sweep ticks are served (`rust/daemon/spec.md`
SW1-SW6), with the merge, link, junk, curation and candidate judges
(LJ1-LJ6, JJ1-JJ5, CU1-CU6, CJ1-CJ5), and the reaper's
session trigger fires the automatic dream. File-mode tensors and embedded bank lifecycle
remain separately deferred. Source preparation and unit tests do not establish
those missing process-boundary contracts.

The enabled release loop under `/health` remains unproved: the health normalizer
equates an unchecked `latest_release: null` / `checked_at: 0` snapshot with a
retained release and check clock.

The pruning scenario uses the existing daemon differential harness. It checks
whole-bank state after the observed pruning milestone. For portable goldens,
catalog fields must first match the arm's seeded baseline exactly, then become
an unchanged-baseline marker; sequence values remain exact except for one
identified new warmup probe and its matching single sequence allocation.
Seeded primary-key identities survive the init scrub even when rewritten;
unexplained sequence changes and other new retrieval rows remain differences.
Seeded relation
clocks must remain unchanged before their declared clock normalization. The
session scenarios validate new close clocks against each arm's own start/end
window and check equality of episode, client-session, deferred-map and
tombstone clocks before normalizing them by root identity.
