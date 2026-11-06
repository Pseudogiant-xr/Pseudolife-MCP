# G2 installed-execution proofs (2026-11-07)

Three proofs of the Rust daemon reached through the commands a user runs,
on throwaway hosts and banks (never the live bank), all passed:

1. a fresh install switched to Rust;
2. an update from Python 0.22.1 to Rust that keeps the bank;
3. backup, restore and rollback under Rust.

They also found a real gap. On a fresh host the default `--engine rust`
activation always fails its health gate, because the updater stops waiting
long before the Rust daemon has downloaded its models. Proofs 1 and 2
passed only with a longer wait (`--health-retries 1300`).

## What ran

- **Tested head:** `5274b0b9269305f304f2b41ec756e408b60cac42` (master,
  2026-11-06). The `v0.22.1` tag is proof 2's starting point.
- **Venue:** the maintainer's Windows host, Docker Desktop 29.9.2.
  - Each proof ran in its own disposable Docker-in-Docker host
    (`harness/Dockerfile.host`: Debian bookworm, docker-ce 29.9.3, nested
    cgroup v2), removed with its volume afterwards.
  - Each host had its own dockerd, so the fixed `pseudolife-mcp-*` container
    names never met the outer host's.
  - Every proof cloned the public repository fresh and built its images
    from source.
- **Driver:** `harness/run_proofs.sh` under
  `pseudolife-mcp lease run full-suite`. It ran `harness/proof.sh upgrade`
  (proofs 2 and 3), then `harness/proof.sh fresh` (proof 1). Run window:
  2026-11-06 16:53Z to 17:49Z.
- **Client entry point:** `ops/install.sh --client claude` registers the
  stdio shim with `claude mcp add`.
  - The host's `claude` is a stand-in (`harness/claude-stub`) that records
    that registration.
  - `harness/mcp_call.py` runs exactly the registered command and
    environment for every read and write below.
  - No real client or credential was involved.
- **Checkout mode:** no installer engine option has merged (G2-B). Here,
  "select Rust" means a normal install, then the checkout updater
  `python3 ops/update.py --engine rust`. Release-mode Rust selection is
  #1355 (open) and is not exercised here.

## Results

- **Ledgers:** `upgrade-ledger.jsonl` and `fresh-ledger.jsonl` record every
  step's command, exit code and times.
- **`summary.json`:** what each check read back, plus short excerpts of the
  log lines cited below. `harness/extract_summary.py` builds it from the raw
  logs, which stay private because they may carry generated credentials.
- **`harness/verify.py summary.json`:** asserts the claims below (26
  checks, exit 0). On a copy tampered to keep the post-backup marker and
  change a fingerprint, it fails both claims and exits 1.
- **Harness timing:** the recorded run used the harness before
  `proof.sh`'s assertion steps and `run_proofs.sh`'s exit propagation were
  added (both after review). For that run the contract is checked by
  `verify.py`, not by the run itself.

Bank fingerprints cover two tables and the schema stamp:
- entries: a count and an md5 over each row's `id` and `md5(text)`;
- facts: a count and one md5 over every row's
  `entity|attribute|value|status`;
- `meta.schema_version`.

Embeddings, tags, episodes, the graph, the counters and the `/data` state
volume are not fingerprinted.

| Proof | Result | Evidence |
|---|---|---|
| 1. Fresh install, then select Rust (checkout mode) | Pass, with `--health-retries 1300` | `ops/install.sh --yes` exit 0, health `ok` at schema 59. The default `--engine rust` failed its gate (the gap below); after its printed rollback, the longer wait selected Rust: engine label `rust`, health `build.git_sha` equal to the tested head. A note and a fact written through the registered shim read back after `docker restart`. A rerun of `--engine rust` exits 0 and redeploys with a new backup and rollback tag (the `rerun_excerpt`); checkout mode is exempt from the quiet-rerun rule. |
| 2. Update Python 0.22.1 to Rust, keeping the bank | Pass, with `--health-retries 1300` | v0.22.1 install exit 0. The bank fingerprint is the same before and after the switch. The update backed up the bank and `/data` first and recorded it in the daemon (`update_backup_excerpt`). The rollback tag `pseudolife-daemon-python:0.22.1-pre-update-*` names the original image ID. The registered shim reads v0.22.1's notes and fact through Rust. |
| 3. Backup and restore under Rust, then roll back to Python | Pass | `pseudolife-mcp backup` wrote a dump and a state archive under Rust. A marker written after the backup is gone after `pseudolife-mcp restore --backup-file … --state-archive … --yes`, which first took a safety dump (`restore_excerpt`). The bank equals the backup-time fingerprint and Rust serves again. The update's printed rollback, run verbatim, brings back the original v0.22.1 image (`sha256:4d43da62ac0c`) on the same bank id. That image reads the note and fact Rust wrote, and writes. Master's own `--engine python` then switches back too. |

## The gap this found

On a fresh host the default `python3 ops/update.py --engine rust` failed
with "the engine failed its version, schema or authenticated-read gate".

- **Frequency:** 3 times out of 3 (`default_activation_gate_failures`):
  both hosts of this run, and the fresh host of the run before it.
- **What the daemon was doing:** at the failure the container read
  `Up 43 seconds (health: starting)`, its log "model provisioning phase …
  downloading model.onnx_data (2 of 12)".
- **Why:** the gate waits 30 × 1.5 s (`update_cli` `health_retries`), while
  `ops/Dockerfile.daemon-rust` budgets up to 30 minutes for first-start
  provisioning. The read-only shadow already waits 1,920 s.
- **Recovery:** each time, the printed rollback restored a healthy Python
  daemon, and `--health-retries 1300` then passed.
- **Diagnosis:** the failed gate does not say which check failed.
- **Fix:** in progress, stacked on #1355. It provisions the models before
  the recreate and names the failing check.

## What this does not show

- **Schema:** v0.22.1 and the tested head are both schema 59, so "the older
  side reads what the newer side wrote" is shown at the same schema
  version. The newer-bank refusal is not exercised here; it is covered by
  `rust/daemon/harness/rehearse_activation.py`, and #1356 (open) addresses
  its reconnect gap.
- **Scope:** the Linux Docker tier only. Not covered: the lite tier, the
  native client runtime, release-mode selection, a published image, and the
  Windows-native daemon.
- **Not recorded:** the PostgreSQL and extractor container identity across
  the daemon-only recreate, and the restored `/data` contents.
- **G2 bar:** this is one part of the maintainer's 2026-11-07 G2 bar. The
  forced real-data run and the 24–48 h live window are separate.

## Rerunning

```sh
G2PROOF_OUT=<a directory outside any checkout> \
  pseudolife-mcp lease run full-suite --expect 4h -- bash harness/run_proofs.sh <master sha>
```

It needs Docker Desktop (or any Docker host that allows `--privileged`) with
room for two builds of the Python and Rust images (disk use was not
measured), and took about an hour here. It exits non-zero when either proof
fails or `verify.py` refuses the summary.
