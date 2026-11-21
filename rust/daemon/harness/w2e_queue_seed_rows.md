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

`w2e_queue_contract.json` holds the corresponding step contracts. For
each registered queue scenario, clone the checked seed into the two banks
used by the frozen W2-E runner at `88787b0e2`. Use the harness's offline
1,024-dimensional ONNX fixture in both daemons. Collect the Python arm's
normalized rows, sequences, numeric values, answers and drained stub
requests after each step. Require the old two-arm comparator to report
zero differences. Copy only the frozen arm's observations into the
contract. Deduplicate identical table bodies and numeric vectors without
changing their content. Preserve request order and endpoint, include the
body digest, and enumerate the verdict-table keys asked on that request.

The live check uses the established clock and numeric normalization. The
fixture supplies complete row sets and exact sequence positions. Other
catalog properties are compared with the prepared bank. No generator
runs during native verification, and the runner never records this data
from the daemon under test.

Some frozen signatures hash the dynamically allocated stub URL. During
capture, record the frozen arm's SHA-256 preimages containing the URL or
a hash already depending on it. Replace the origin with `@QUEUE_STUB@`
in the recorded preimage and resolve hash references in dependency order.
The verifier reconstructs the expected hashes from these preimages using
its own stub's address, independently of the candidate's stored hashes.
