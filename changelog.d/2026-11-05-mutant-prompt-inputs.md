### Fixed (2026-11-05 — Build source mutants with their embedded prompt inputs)
- Mutant source copies include the extractor shim's external prompt files, refresh their build timestamps and remove inputs no longer in the checkout. The same source controls and cases can compile instead of failing before execution.
