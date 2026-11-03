### Fixed (2026-11-03 — extractor rollback after a late launcher child)

- Windows extractor replacement and removal collect native children created while their waiting launcher stops, and wait for termination before restoring the previous task. Failed replacement diagnostics retain rollback warnings in the test output.
