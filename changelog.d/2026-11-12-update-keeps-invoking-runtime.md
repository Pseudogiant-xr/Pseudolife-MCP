### Fixed (2026-11-12 — a release update no longer deletes the shim runtime it is still running from)
- A release-mode `pseudolife-mcp update` that installed a new shim runtime
  could finish the whole update and then exit 1 with `ImportError: cannot
  import name 'console_cli'` (seen on Linux, 0.22.1 to 0.23.0). The new
  runtime runs the client steps after the shim as a child of the updater, and
  that child's runtime prune left out its own parent process as "this
  helper", so it removed the older runtime the waiting updater still ran from.
  The prune now holds any runtime a live process runs from, except the pruning
  process itself. A handed-off run also holds the runtime of the updater that
  started it: from this release the updater passes its runtime explicitly,
  and when an older updater passes only its pid and cannot be placed in a
  runtime, no runtime is removed while it lives.
- The closing notes after a successful update (the schema v59 owner note and
  the Console sign-in hint) and the unattended run's board notice can no
  longer fail the update: an error there is a warning or a step line, and the
  update's own result stands.
