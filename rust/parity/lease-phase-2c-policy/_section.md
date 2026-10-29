## Lease phase 2c policy preparation

CLI-LEASE and the lease mode row remain deferred. The named decisions in
PORTING.md supersede historical references to unapproved groups 1, 3, 4 and 13;
they do not promote any historical receipt to current-head acceptance. The
23 retained instances below are preparation only: 14 traceback matches under
the approved contract (12 newly admitted and two earlier approved), eight
candidate refusals still requiring implementation, and one clock comparison
incomplete without captured invocation windows and a second oracle run.

| Case ID | Named policy | Governed fields | Retained disposition |
| --- | --- | --- | --- |

The JSON ledger retains each source receipt basename and SHA256, platform,
terminal/diagnostic bytes, named field and exact shape/bound. The raw receipts
remain unchanged; missing metadata is explicitly null. No outside-rule retained
case was found in this scoped inventory. This is not a claim about unexamined
inputs, current CI, full mode coverage or final-head executable behavior.

Implementation after the version gate:

- Add a fatal forbidden-header failure path in `shim/src/cli/lease/board.rs`,
  inspecting the approved fields before constructing headers. Propagate it
  through `run.rs` and `view.rs` as the named diagnostic/exit 1, before local
  fallback, child launch or lock-file creation. Preserve the exact ordinary
  non-ASCII and surrogate behavior, and use checked header constructors.
- Add candidate-specific corpus inputs and expected refusal outputs in a new
  additive module; keep the oracle raw and label the candidate substitution.
  The retained DEL/CR/LF/NUL cases need both-platform final-head proofs.
  New required capture IDs: `D3-DEL-bearer`, `D3-CR-bearer`, `D3-LF-bearer`,
  `D3-NUL-bearer`, `D3-FOLDED-agent_id`, `D3-FOLDED-credential`,
  `D3-FOLDED-bearer`. Cover remaining C0 byte values with additive controls.
- Wire the named policies at the comparison boundary without rewriting raw
  observations. The existing corpus rejects normalization; changing that
  instrument is a later, explicit integration step, not done by this preparation.
- Retain start/end Unix windows around every arm invocation of
  `lease-run-existing-successful-child`, plus two oracle repetitions proving
  `/t` differs and all other bytes stay exact. Validate the strict decimal shape
  and own-window bound before any comparison; add successful-run timing only
  after this proof. Existing child source remains unchanged.
- Rebase only after the version branch is ported, then freeze and recapture
  each affected case at the final head on Windows and Linux, bind the resulting
  receipts and measurements, and verify routed/additive nodes in hosted CI.
  Broader host/storage/concurrency/recovery/signal gates remain deferred.

Static preparation checks do not run a daemon, child command or native binary.
Existing tests and the existing eval harness remain byte-identical.
