# W3-J preparation contract

This slice prepares an opt-in Rust container and narrows the native sent YAML
scanner boundary. It does not change the default daemon, installed runtimes,
update paths or releases; the five Phase 5 rows remain deferred.

| Boundary | Exact contract / Python authority |
|---|---|
| Image | CPU only, offline runtime, root user (the existing image has no USER), port 8765, `/data` volume/config, unauthenticated health check and build stamps: `ops/Dockerfile.daemon`. Rust's embedder requires a tokenizer, fp32 ONNX graph/external data and dynamic CPU ORT: `src/embed.rs`, `src/service.rs::ensure_init_inner`. |
| Isolation | Explicit separate compose file/project; separate bank and loopback port; disposable data only. Existing compose bytes and bundled copies stay equal: `tests/test_update_cli.py::test_bundled_compose_files_match_the_checkouts`. Cleanup removes only this run's resources. |
| Runtime proof | Start against disposable pgvector Postgres. Require healthy CPU ONNX initialization, successful authenticated search and refused unauthenticated search. No runtime model download or image publication. This proves packaging/readiness, not full daemon parity. |
| YAML | Native sent's typed configuration boundary remains in `shim/src/sent_config.rs::parse`. Refuse tabs between tokens/in plain scalars and non-ASCII/punctuated anchor or alias names: PyYAML `scanner.py::scan_to_next_token`, `scan_anchor`; Python loads config through `utils/config.py::load_config`. Quoted/block string content and comment tabs remain admissible. Existing named resolver/tag/merge/type refusals remain. |
| Differential | The startup corpus calls PyYAML and the existing owned native sent process harness. Refusals require exit 1, empty stdout, a named `config-yaml-typed` diagnostic and unchanged disposable-home files. Accepted cases require affirmative native readiness and owned cleanup. No database connection/write is exercised by these YAML cells. |

Free items: YAML diagnostic wording after the named prefix; process PID, nonce
and ephemeral port (validated by the existing harness). Scalar values and
admission are exact. Container search score/ranking parity belongs to the read
path slice, not this preparation proof. The frozen public ONNX graph is a
packaging input; its different graph hash from the historical prerequisite
does not inherit that receipt's numerical evidence.

The graph, external tensors and tokenizer are the onnx-community export's
files. The module list, last-token Pooling configuration, model configuration
and tokenizer configuration come from `Qwen/Qwen3-Embedding-0.6B` at
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, with hashes equal to the recorded
Qwen metadata. That module list requires normalization. Its tokenizer config
omits `padding_side`, so the loader retains right padding; the image readiness
check requires the policy actually reported by the loaded daemon. The synthetic
last/left/normalize fixture is separate from the identified real-model receipt.

Named YAML presentation deferral `sent-quoted-continuation-tab`: a quoted continuation starting with a literal
tab at indentation column zero (for example `ignored: 'a` followed by a line
starting with tab then `b'`, with either quote style) is accepted by PyYAML and rejected by yaml-rust2.
The sent boundary retains its named invalid-YAML refusal; shipped configuration
producers use space indentation and escaped tabs. This case is counted separately
from actual PyYAML refusals, not reported as differential agreement.
`sent-anchor-colon-terminator` (`a: &x:y b`) is also deferred: PyYAML ends
the anchor name at the colon and loads the remaining scalar; yaml-rust2
includes the colon in the anchor name and the sent boundary refuses it.

Evidence needed before cutover is enumerated in [CUTOVER.md](../CUTOVER.md).
The image requires an explicit external Postgres DSN; embedded/file modes,
remaining daemon routes and the other slices' divergences remain deferred.
