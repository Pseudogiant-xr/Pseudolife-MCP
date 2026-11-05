### Fixed (2026-11-04 — Reuse completed installer setup on reruns)

- Both installers retain recorded extractor, model, client and setup choices,
  leave unchanged managed files untouched, and skip Compose startup when the
  checkout, configuration and running images match the last healthy deployment.
  Stopped containers restart with existing images. Checkout shim runtimes are
  reused only when source, interpreter, package version and dependencies match;
  source edits, missing dependencies and incomplete runtimes still rebuild.
