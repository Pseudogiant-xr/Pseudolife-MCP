# Commit-loss seed fixtures

`commit_loss_seed_rows.json` captures the frozen
`commit_loss.prepare_template` at `94336fee3` on disposable PostgreSQL. It
also holds the three shutdown templates from the frozen
`w2e.prepare_template`: the standalone shutdown config with retrieval
logging disabled, `counters-restore`, and `search-after-restart`.

The recipe runs the original seeding tool calls, stops the child, and applies
the original post-stop SQL. It uses the offline 1,024-dimensional embedding
fixture. The child clock is fixed at `1795000000.0`, UUIDs are successive
integer UUIDs and synthetic token hex is repeating `01`.

Capture the populated public tables using `to_jsonb(table_row)`. Record
the last value and called flag for each sequence, and compare the full
catalog using `dbstate.dump`. The shared initializer supplies the schema,
built-in relations and writer epoch. Other rows retain their complete
column sets. Identical vector values share a name in the fixture.

The fault runner clones each replayed seed before injecting a COMMIT fault.
It reads the durable bank directly at each milestone to check what the
native daemon serves, including after a clean stop and restart.
