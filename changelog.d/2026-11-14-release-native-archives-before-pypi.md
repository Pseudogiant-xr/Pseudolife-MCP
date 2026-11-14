### Fixed (2026-11-14 — a release reaches PyPI only once its native archives are on GitHub)
- The release workflow now builds, verifies and attaches the five native
  client archives and `SHA256SUMS` to the GitHub release before it
  publishes to PyPI. `pseudolife-mcp update`, including the 0.23.x update
  bridge, learns that a release exists from PyPI. Before this change, an
  update run between the PyPI upload and the archive upload (the whole
  native build, often more than an hour) found no native archive and
  stayed on the Python client until the next release. A failed native build now stops the release before PyPI, which
  cannot be unpublished. The images and the MCP registry still follow PyPI.
