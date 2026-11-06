# G2 installed-execution proofs (2026-11-07)

Three proofs of the Rust daemon reached through the commands a user runs:
a fresh install switched to Rust, an update from Python 0.22.1 to Rust that
keeps the bank, and backup, restore and rollback under Rust. They ran on
throwaway hosts with throwaway banks, never the live bank, and all three
passed. They also found one real gap: on a fresh host, the default
`--engine rust` activation always fails its health gate, because the
updater stops waiting long before the Rust daemon has downloaded its
models. With a longer wait it succeeds.

## What ran

- **Tested head:** `5274b0b9269305f304f2b41ec756e408b60cac42` (master, 2026-11-06),
  plus the `v0.22.1` release tag for proof 2's starting point.
- **Venue:** the maintainer's Windows host, Docker Desktop 29.9.2. Each
  proof ran in its own disposable Docker-in-Docker host
  (`harness/Dockerfile.host`: Debian bookworm, docker-ce 29.9.3, nested
  cgroup v2), removed with its volume afterwards. Each host had its own
  dockerd, so the fixed `pseudolife-mcp-*` container names never met the
  outer host's containers. Every proof cloned the public repository fresh
  and built every image from source.
- **Driver:** `harness/run_proofs.sh`, run under
  `pseudolife-mcp lease run full-suite`, executed `harness/proof.sh
  upgrade` (proofs 2 and 3) and then `harness/proof.sh fresh` (proof 1).
  Run window: 2026-11-06 16:53Z to 17:49Z.
- **Client entry point:** `ops/install.sh --client claude` registers the
  stdio shim with `claude mcp add`. The host's `claude` is a stand-in
  (`harness/claude-stub`) that records that registration.
  `harness/mcp_call.py` then runs exactly the registered command, with the
  registered environment, for every read and write below. No real client
  or credential was involved.
- **Checkout mode:** no installer engine option has merged (G2-B), so
  "select Rust" here means the checkout updater, `python3 ops/update.py
  --engine rust`, after a normal install. Release-mode Rust selection is
  #1355 and is not exercised here.

## Results

`upgrade-ledger.jsonl` and `fresh-ledger.jsonl` record every step: its
command, exit code and start and end times. `summary.json` holds what each
check read back. The raw per-step logs stay private, since they may carry
generated credentials.

| Proof | Result | Evidence |
|---|---|---|
| 1. Fresh install, then select Rust (checkout mode) | Pass | `ops/install.sh --yes` exit 0, health `ok` / schema 59. Rust selected (`org.pseudolife.engine=rust`, health `build.git_sha` = the tested head). A write through the registered shim survived `docker restart`: the search and fact read back after the restart. Rerunning `--engine rust` exits 0 (it redeploys with a new backup and rollback tag; checkout mode is exempt from the quiet-rerun rule). |
| 2. Update Python 0.22.1 to Rust, keeping the bank | Pass | v0.22.1 install exit 0. Bank fingerprints before and after the switch are identical (entries 2 / facts 1, md5 per row). Rollback tag `pseudolife-daemon-python:0.22.1-pre-update-*` kept at the original image ID. The registered shim reads 0.22.1's notes and fact through Rust. |
| 3. Backup and restore under Rust, then roll back to Python | Pass | `pseudolife-mcp backup` (dump plus state archive) exit 0. A marker written after the backup is gone after `pseudolife-mcp restore --backup-file … --state-archive … --yes`: the bank equals the backup-time fingerprint, a safety dump was taken first, and Rust serves again. Running the update's printed rollback verbatim brings back the original v0.22.1 image (`sha256:4d43da62ac0c`) on the same bank (`313d9ec766827a8b`): it reads the note and fact Rust wrote, and it writes. Master's own `--engine python` then switches back too. |

## The gap this found

On a fresh host the default `python3 ops/update.py --engine rust` failed
with "the engine failed its version, schema or authenticated-read gate". It
failed 3 times out of 3: both hosts of this run, and the fresh host of the
run before it. The daemon's own log 43 s after the recreate read "model
provisioning phase: installing pinned artifacts … downloading
model.onnx_data (2 of 12)". The gate waits 30 × 1.5 s
(`update_cli` `health_retries`); `ops/Dockerfile.daemon-rust` budgets up to
30 minutes for first-start provisioning; the read-only shadow already waits
1,920 s. The ledgers keep each failure (`*-default`, exit 1). After it, the
printed rollback restored Python cleanly, and the retry
`--health-retries 1300` passed. The failed gate does not say which check
failed. A fix (provision the models before the recreate, and name the
failing check) is in progress, stacked on #1355.

## What this does not show

- Schema: v0.22.1 and the tested head are both schema 59, so "the older side
  reads what the newer side wrote" is shown at the same schema version. The
  newer-bank refusal is not exercised here. `rust/daemon/harness/rehearse_activation.py`
  covers it, and #1356 closes its reconnect gap.
- Scope: Linux Docker tier only. No lite tier, no native client runtime, no
  release-mode selection, no published image, and no Windows-native daemon.
- Cleanup: PostgreSQL and extractor container identity across the
  daemon-only recreate was not recorded; the bank fingerprints were.
- G2 bar: this is one of the three parts of D6's evidence bar for G2. The
  forced real-data run and the 24–48 h live window are separate.
