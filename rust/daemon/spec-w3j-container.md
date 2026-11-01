# W3-J preparation contract

This slice prepares an opt-in Rust container and narrows the native sent YAML
scanner boundary. It does not change the default daemon, installed runtimes,
update paths or releases; the five Phase 5 rows remain deferred.

| Boundary | Exact contract / Python authority |
|---|---|
| Image | CPU only, offline after model provisioning, root user (the existing Python image has no USER), port 8765, `/data` volume/config, unauthenticated health check and build stamps: `ops/Dockerfile.daemon`. Rust's embedder requires a tokenizer, fp32 ONNX graph/external data and dynamic CPU ORT: `src/embed.rs`, `src/service.rs::ensure_init_inner`. |
| Isolation | Explicit separate compose file/project; separate bank and loopback port; disposable data only. Existing compose bytes and bundled copies stay equal: `tests/test_update_cli.py::test_bundled_compose_files_match_the_checkouts`. Cleanup removes only this run's resources. |
| Runtime proof | Provision the persistent model cache, then cold-start against disposable pgvector Postgres with the container network disabled. Require CPU ONNX initialization, successful authenticated `memory_search`, refused unauthenticated access, idempotent offline provisioning, and refused tampered artifacts. No image publication. This proves packaging/readiness, not full daemon parity. |
| YAML | Native sent's typed configuration boundary remains in `shim/src/sent_config.rs::parse`. Refuse tabs between tokens/in plain scalars and non-ASCII/punctuated anchor or alias names: PyYAML `scanner.py::scan_to_next_token`, `scan_anchor`; Python loads config through `utils/config.py::load_config`. Quoted/block string content and comment tabs remain admissible. Existing named resolver/tag/merge/type refusals remain. |
| Differential | The startup corpus calls PyYAML and the existing owned native sent process harness. Refusals require exit 1, empty stdout, a named `config-yaml-typed` diagnostic and unchanged disposable-home files. Accepted cases require affirmative native readiness and owned cleanup. No database connection/write is exercised by these YAML cells. |

Free items: YAML diagnostic wording after the named prefix; process PID, nonce
and ephemeral port (validated by the existing harness). Scalar values and
admission are exact. Container search score/ranking parity belongs to the read
path slice, not this preparation proof. B8 measures the public ONNX graph
independently using `harness/embedding.py`; it does not inherit the historical
export's numerical evidence.

The graph, external tensors and tokenizer are the onnx-community export's
files. The module list, last-token Pooling configuration, model configuration
and tokenizer configuration come from `Qwen/Qwen3-Embedding-0.6B` at
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, with hashes equal to the recorded
Qwen metadata. That module list requires normalization. Its tokenizer config
omits `padding_side`, so the loader retains right padding; the image readiness
check requires the policy actually reported by the loaded daemon. The synthetic
last/left/normalize fixture is separate from the identified real-model receipt.

## B8 model provisioning

`pseudolife-daemon provision models` installs both models named by blocker 6:
Qwen3-Embedding-0.6B and the ms-marco MiniLM cross-encoder. It needs no bank,
configuration file or bearer. Exit 0 means installed or already provisioned;
exit 1 names the download, checksum or filesystem cause and the repair;
unsupported command arguments exit 2. A successful rerun checks the bytes,
performs no download or rewrite, and prints one `models already provisioned`
line. A failed download never publishes a partial bundle. Installation uses
the shared native provisioner's lock, private staging tree and atomic rename.

The cache base is `PSEUDOLIFE_DAEMON_MODELS_DIR`, otherwise
`$PSEUDOLIFE_MCP_DATA_DIR/models`, otherwise the platform's per-user data
directory plus `models`. This pin set installs under
`qwen3-msmarco-fp32-v1-1ca6e542b73a/`, with `embedding/` and `reranker/`
subdirectories. The suffix binds the complete compiled manifest; changed pins
install alongside the old bundle. `pseudolife-runtime.json` records provenance,
but a recorded checksum is never trusted instead of hashing the actual bytes.

`PSEUDOLIFE_DAEMON_ONNX_DIR` and `PSEUDOLIFE_DAEMON_RERANK_DIR` remain complete
model-root overrides. They do not override checksums. Qwen's graph path is
`embedding/onnx/model.onnx`, its external tensors are beside it, and the
reranker's graph is `reranker/model.onnx`. Both roots contain `tokenizer.json`
and pinned metadata. The default Qwen loader verifies both models before
becoming ready; the reranker independently verifies its inputs before loading.
Ordinary loading never downloads. Missing or changed inputs therefore refuse
readiness with their cause and provisioning instruction. Instrumented harness
builds also admit the existing tiny synthetic BERT fixtures; production builds
have no fixture bypass flag. Other explicitly configured embedding models keep
their existing local/cached loading contract and have no new parity claim.

| Artifact | Frozen source | SHA256 |
|---|---|---|
| Qwen fp32 graph | `onnx-community/ONNX_Qwen3-Embedding-0.6B`, revision `462e5a71e724575c710975d9b79309b690fd22ce`, `model.onnx` | `cec22565ec783289a5e51bd94950f70b8cb7ca6c0b7ced255b5e8dbf3c60536b` |
| Qwen fp32 external tensors | Same revision, `model.onnx_data` | `a585477de21c0a89e021dd64f4d3be34483eb4aaed7ae93fc047e3edf74545da` |
| MiniLM reranker fp32 graph | `cross-encoder/ms-marco-MiniLM-L6-v2`, revision `c5ee24cb16019beea0893ab7796b1df96625c6b8`, `onnx/model.onnx` | `5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a` |

Every download uses `https://huggingface.co/<repository>/resolve/<revision>/<file>`.
The complete URL, tokenizer and metadata pins are compiled in
`src/model_provision.rs`. Only one fp32 export per model ships. Historical
embedding identities remain in the harness for old-result comparisons, not
as alternative daemon pins. bf16 ONNX remains out of scope.

The image entrypoint runs `provision models` before its default daemon command,
then `exec`s the daemon. An explicit `pseudolife-daemon provision models` image
command provisions once and exits. `/data/models` persists across container
replacement; a separate writable model volume can be selected with
`PSEUDOLIFE_DAEMON_MODELS_DIR` when `/data` is read-only. An already provisioned
model cache can itself be mounted read-only. No model files are baked into an
image layer. First-start provisioning keeps downloads out of image builds and
lets reinstall/update reuse the same verified cache. Native installers invoke
the same command; installer and updater wiring belongs to B11.

The disposable preparation Compose stack keeps `/data` on tmpfs but mounts a
disk-backed named volume at `/models`. CI restores a model cache keyed on the
compiled pin source and binds that cache to the same named volume. It runs
`compose run --rm --no-deps rust-daemon pseudolife-daemon provision models`
before `compose up --wait`, so download time is outside the service readiness
deadline. Direct first start still provisions automatically; a health-timed
launcher should likewise provision before starting its readiness timer.

The explicit native provision command uses HTTPS directly, independent of
the Python-library flags `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE`. Those
flags can remain set in the image. Network isolation is tested with Docker
`--network none`, not inferred from environment flags.

### Correctness evidence and limits

Run `harness/embedding.py --accepted-fp32 --qwen-position-ids-deferred` with
the provisioned embedding directory and a Python checkpoint from the pinned
Qwen revision. The torch checkpoint is test-only and is not installed by the
Rust provisioner. Tokenization, cache behavior and ranking remain exact;
vector bounds remain `FP32_MAX_ABS` and `FP32_MIN_COSINE`. The Python ONNX
wrapper still fails to supply `position_ids`; the harness records that exact
failure and compares against the shipped Python torch fp32 pipeline instead.

The extra `harness/w2d_rerank_check.py` control has a pre-existing exact-order
failure in case 20 on the tested Python stack: two different full passages
truncate to identical token inputs; Python produces slightly different logits
while Rust ties. The historical local reranker graph and the newly provisioned
public graph produce identical Rust logits and sigmoid outputs over all 40
cases. Both agree with 39 of Python's 40 orders and satisfy the harness's
sigmoid tolerance. This is an explicit baseline limitation, not a passing
reranker differential or an acceptance of unspecified caller-visible order;
this change does not relax the harness or alter scoring.

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
