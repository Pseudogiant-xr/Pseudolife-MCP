| Boundary | Candidate coverage | Status | Notes |
|---|---|---|---|
| JSONB container depth | `bounded-jsonb-depth`: 2048 containers per column with protected decode/traversal/encoding/drop; deeper values return private 503 | deferred | candidate; named read domain; depth 200 exact, boundary policy controls separate |
