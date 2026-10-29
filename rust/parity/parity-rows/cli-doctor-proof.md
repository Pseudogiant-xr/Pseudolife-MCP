| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| CLI-DOCTOR-PROOF | 2/4 | `doctor --disposable-proof` with a disposable database: builds a fixture bank and drives the daemon in-process. `coordination_proof.py` | Native returns the canonical refusal without a database and defers with one. Blocker: needs the native daemon (W1-A/W2) | deferred |
