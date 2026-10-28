### W2-E first increment boundaries

Stores and background reaping now use one episode log, touch map, deferred-root
map and tombstone map under `Ready.write`. Startup constructs that owner from
the validated startup candidate; no second episode cache is published. This
closes the preparation's cache-ownership gap without adding another database
connection or changing the leased writer boundary.

Admitted store tasks are tracked independently of disconnected callers and
drained before the background exit callback. This completes their write-through
database operations; it does not implement tensor-state persistence.

The remaining write work is deliberately split into subsequent increments:
