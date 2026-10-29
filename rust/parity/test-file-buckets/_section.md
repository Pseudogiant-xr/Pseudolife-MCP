## Test-file buckets

The real-bank stdio judge declares `completed-readiness-wait-notice` as a
semantic normalization at `/stderr`. The producer is the shim's existing
no-spawn path: Python and Rust can print a five-second readiness wait notice
after an unsuccessful initial probe, then complete the same exchange.
Normalization requires matching stdout under the existing byte comparator,
successful integer exit 0 in both arms, at least one stdout frame, genuine
process evidence, and stderr consisting only of the exact notice or empty bytes.
The notice is limited to HTTP loopback `127.0.0.1`, a decimal port 1–65535, the
existing five-second text, and one LF or CRLF terminator. It never suppresses
extra diagnostics, timeout/boundary errors, or output/exit mismatches. Startup,
fault, EOF and concurrent-call policies remain strict; transport headers are
outside this rule. Raw streams remain in private captures and both arms' raw
stderr is also retained in each applied normalization event in public receipts.
The regression test retains the exact 172-byte Windows notice, with synthetic
matched frames; it is not a replay of historical stdout. The original cause of
the initial probe miss remains unproven, and this rule changes no production
lifecycle, timeout or diagnostic behavior.

The Phase 0b snapshot retains 406 `tests/test_*.py` files: 68 oracle, 1 candidate and 337 internal. Its historical manifests remain `test-buckets.json` and `contract-inventory.json`; validate them with `python rust/contract_inventory.py` and the unchanged `python -m pytest rust/test_contract_inventory.py -q`.

The selected Phase 1 manifest at `1d444aa1bc432431e5fcbc07a55e1fe661c0f8e4` contains 426 `tests/test_*.py` files: 68 oracle, 2 candidate and 356 internal; the one file added since `686b3f95c4a4d4e6c2d81e1b76be9901945a8da1` (425 files), `tests/test_changelog_fragments.py`, is internal. Historical manifests at `f709abb54f7912ae9cd767998d0926ca33df4bcd` retain 419 files: 68 oracle, 2 candidate and 349 internal. Its manifests are `phase1-test-buckets.json` and `phase1-contract-inventory.json`. Candidate routing maps exactly 18 concrete nodes: 8 from `test_cli_dispatch.py` and 10 public stdio cases from `test_shim.py`; unlisted nodes stay oracle-only. The phase 1 function inventory separately classifies all 191 current-pin functions in its nine scoped files as 10 candidates, 1 public oracle CLI case and 180 internal cases. The two current-pin additions check tool refusals and unknown-parameter forwarding through the public default stdio launch; their source identity and adapter routing are covered. Both executed once/non-skipped per arm in the historical native Linux capture at `74b5998c`: tool refusal failed in both arms, while unknown-parameter refusal passed in both. At published `95d5402d`, both additions passed once/non-skipped in each OS candidate lane; no Python ten-stdio original lane was reported. The later console/warm successor remains pending its combined-head proof. Historical targeted-equivalence evidence retains its original candidate trees and pins. Historically, at `2e628b27`, all 125 scoped internal cases named Rust equivalents with targeted Windows and Linux evidence. The current mapping has 121 completed equivalents, exactly 3 SDK cases retired-by-decision, and 1 pending postframe update-scheduling substitution with targeted evidence on both platforms at frozen tree `a4ff3236eb349aaed427d80129513fe22cf0183f`; 55 internal cases remain outside this phase's scope. Windows retains the recorded transient ConnectionReset followed by successful exact and full-file reruns; Linux check/clippy, 66 integration and 5 wire cases passed with four saved exits 0. The mapping's pending postframe row remains subject to overall acceptance g; the register separately records its authorized substitution and current runtime evidence. Four historical 690bb8ac schema-2 receipts, both 3x10 paired measurements and executing CI are recorded below. Phase 1 later completed its own final integrated-head suites/review/CI before #560 merged; version-branch stdio acceptance remains separate. Run `python rust/contract_inventory.py --phase1` and `python -m pytest evals/rust_port/test_phase1_inventory.py -q`.

The final Phase 1 judge always adds seven startup and six concurrent non-EOF candidate cells through `evals/rust_port/stdio_scenarios.py`. The pinned byte templates and platform-specific common-release order evidence are committed in `stdio_startup_contract.json` and `stdio_concurrent_orders.json`; each cell must bind the actual candidate executable. Separate response releases retain exact AB or BA order, while a common release permits only the observed final call pair orders. These additive checks do not establish final-head acceptance or replace the existing EOF corpus.

Frozen candidate `2e628b27` passes 280 Windows and 262 Linux Rust tests,
214 combined harness/audit tests, and strict eight-node stdio judges on both
platforms with zero differences. Historical receipts are
`evals/results/rust-port-phase1-rust-windows-2e628b27.json` and
`evals/results/rust-port-phase1-rust-linux-2e628b27.json`; historical receipts
retain their recorded identities. The independent code verdict is conditional
approval after four original blockers were fixed. The first hosted attempt
failed in new fixtures on both platforms and skipped parity. That attempt
remains historical evidence; executing green CI at 690bb8ac is recorded below.
Required committed full suites, final integrated-head CI and fresh review
remain gates. PR #546 may remain open and stacked under the close-out brief.
