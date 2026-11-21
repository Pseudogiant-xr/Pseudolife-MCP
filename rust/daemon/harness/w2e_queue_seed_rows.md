# Queue judge seed fixtures

`w2e_queue_seed_rows.json` captures the template immediately after the frozen
W2-E seeder at `88787b0e2` ran these scenarios on disposable PostgreSQL.

| Capture | Scenario | Consumers |
| --- | --- | --- |
| `judge` | `link-auto` | Link and junk judges |
| `deep` | `candidate-auto` | Candidate and curation judges |
| `fold` | `curation-auto-fold` | Curation fold, with the two additional lesson rows |
| `junk` | `junk-auto` | Junk auto, with the two current facts that protect Delta Node |

Use a disposable home, a restricted PostgreSQL login, and the cached
`Qwen/Qwen3-Embedding-0.6B` revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` in offline Torch CPU mode with
FP32 and max sequence length 512. Disable reranking. Start each scenario's seed stub and
call the frozen `w2e.prepare_template` with that scenario and stub. Pin the
child's `time.time` to `1795000000.0`, `uuid.uuid4` to successive integer
UUIDs, and `secrets.token_hex(n)` to `"01" * n`. Stop the child before reading
the template.

Capture all public table rows as `to_jsonb(table_row)` and each sequence's
`(last_value, is_called)`. Compare the complete `dbstate.dump` catalog as
well. Replay uses the shared initializer for the schema, built-in relations
and writer epoch. Keep each other populated table's full row descriptors.
Allocate the captured count of sequence positions on replay, including any
gaps. Identical vector values share a name in the fixture. The fold capture
adds two lessons and raises the lessons sequence position from five to seven.

Equivalence uses a fresh disposable bank for each capture and replay, pins
the shared initializer to the same clock, and compares every captured table,
sequence position and catalog section.
