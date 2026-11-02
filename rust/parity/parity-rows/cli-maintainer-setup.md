| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-MAINTAINER-SETUP | 4 | Guided host configuration, read-only check, Tailscale HTTPS serve ownership/undo, host consent, exact original backup, saved-name/restart recovery, pending-key identity check and fresh-key revocation; `maintainer_setup.py`, `expose_cli.py` | `cli_harness/rows/maintainer_setup.py` and its row specification; bound oracle goldens; `shim/tests/cli_maintainer_setup_contract.rs`; Python transport regression tests; declared config-value/backup-name projection and bounded YAML rewrite domain; Linux native build and all 30 live cases passed (Rust, Python and unlabelled image helpers); Linux golden replay and both-platform terminal regression passed; all 30 Windows live cases and goldens passed; hosted stress pending | ported-with-substitution |

The updater integration adds explicit `--container` scoping in both CLI arms.
Inspection, host helpers, key operations and restart retain that target; a
missing explicit target refuses instead of selecting a local or default bank.
Existing no-flag cases remain the default-output regression. The scoped row,
recreated-checkout isolation tests and both-platform validation are pending.
